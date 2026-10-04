"""`Reported` and `Polled` agents: real HTTP agents on an ephemeral port, driven by the real drivers."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters.agent.reach import reach_for
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.domain.agent import (
    AgentReport,
    AgentStatus,
    AgentUnderTest,
    Booked,
    Polled,
    Reported,
    WakeReason,
    WakeRequest,
)
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, EntityKind
from tests.orchestrator.rig import T0, Rig, scenario
from tests.orchestrator.world import RecordingClock, serving


class ReportingAgent:
    """Works for two polls after each wake. After START it asks to wake in a day; after that wake it is done."""

    def __init__(self) -> None:
        self.wakes: list[WakeRequest] = []
        self.polls = 0
        self._busy = 0
        self.fail_wakes = False

    def app(self) -> Starlette:
        async def wake(request: Request) -> Response:
            if self.fail_wakes:
                return JSONResponse({"error": "boom"}, status_code=500)
            self.wakes.append(WakeRequest.model_validate_json(await request.body()))
            self._busy = 2
            return JSONResponse({"ok": True})

        async def report(request: Request) -> Response:
            self.polls += 1
            if self._busy:
                self._busy -= 1
                return Response(AgentReport(status=AgentStatus.WORKING).model_dump_json())
            last = self.wakes[-1]
            if last.reason is WakeReason.START:
                answer = AgentReport(status=AgentStatus.IDLE, next_wake=last.now + timedelta(days=1))
            else:
                answer = AgentReport(status=AgentStatus.DONE)
            return Response(answer.model_dump_json(), media_type="application/json")

        return Starlette(routes=[Route("/wake", wake, methods=["POST"]), Route("/report", report, methods=["GET"])])


class TickingAgent:
    def __init__(self) -> None:
        self.ticks: list[tuple[WakeReason, datetime]] = []
        self.requests: list[WakeRequest] = []

    def app(self) -> Starlette:
        async def tick(request: Request) -> Response:
            got = WakeRequest.model_validate_json(await request.body())
            self.ticks.append((got.reason, got.now))
            self.requests.append(got)
            return JSONResponse({"ok": True})

        return Starlette(routes=[Route("/tick", tick, methods=["POST"])])


async def _run(rig: Rig, scn: Scenario, agent: AgentUnderTest, run_id: str) -> RunRecord:
    clock = RecordingClock(scn.starts_at)
    return await run_scenario(
        scenario=scn,
        agent=agent,
        reach=reach_for(agent),
        store=rig.open(run_id, clock),
        clock=clock,
        services=Services(providers=[]),
        replier=ScriptedReplier(scn),
        poll_interval=0.001,
    )


async def test_the_reported_driver_polls_through_working_and_takes_the_next_wake(rig: Rig, tmp_path: Path) -> None:
    agent = ReportingAgent()
    async with serving(agent.app()) as base:
        under_test = AgentUnderTest(
            name="reporter", wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report")]
        )
        record = await _run(rig, scenario(ticket_fates=[]), under_test, "reported")

    assert record.stop is StopReason.AGENT_DONE
    assert [(w.reason, w.now) for w in agent.wakes] == [
        (WakeReason.START, T0),
        (WakeReason.DUE, T0 + timedelta(days=1)),
    ]
    assert agent.wakes[0].goal is not None and agent.wakes[1].goal is None
    assert agent.polls == 6


async def test_a_reported_agent_answering_500_ends_the_run_agent_failed(rig: Rig) -> None:
    agent = ReportingAgent()
    agent.fail_wakes = True
    async with serving(agent.app()) as base:
        under_test = AgentUnderTest(
            name="reporter", wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report")]
        )
        record = await _run(rig, scenario(ticket_fates=[]), under_test, "reported-500")

    assert record.stop is StopReason.AGENT_FAILED


async def test_a_polled_agent_gets_one_wake_per_interval_and_none_skipped(rig: Rig) -> None:
    agent = TickingAgent()
    async with serving(agent.app()) as base:
        under_test = AgentUnderTest(name="ticker", wakes=[Polled(wake_url=f"{base}/tick", every=timedelta(hours=2))])
        record = await _run(rig, scenario(ticket_fates=[], deadline_after=timedelta(hours=9)), under_test, "polled")

    assert record.stop is StopReason.DEADLINE_PASSED
    assert agent.ticks == [(WakeReason.START, T0)] + [(WakeReason.TICK, T0 + timedelta(hours=h)) for h in (2, 4, 6, 8)]


async def test_a_scripted_direction_wakes_the_agent_with_the_owners_words(rig: Rig) -> None:
    agent = TickingAgent()
    heard: list[str | None] = []
    async with serving(agent.app()) as base:
        under_test = AgentUnderTest(name="ticker", wakes=[Polled(wake_url=f"{base}/tick", every=timedelta(hours=5))])
        scn = scenario(
            ticket_fates=[],
            deadline_after=timedelta(hours=6),
            directions=[{"text": "Prioritise the legal review.", "after": timedelta(hours=2)}],
        )
        record = await _run(rig, scn, under_test, "directed")
        heard = [w.direction for w in agent.requests]

    assert record.stop is StopReason.DEADLINE_PASSED
    assert agent.ticks == [
        (WakeReason.START, T0),
        (WakeReason.DIRECTION, T0 + timedelta(hours=2)),
        (WakeReason.TICK, T0 + timedelta(hours=5)),
    ]
    assert heard == [None, "Prioritise the legal review.", None]


class BookingAgent:
    """Books a wake five hours out on START. When the scheduler has delivered it, works for two polls, tells the
    owner, and is done: the delivery is its wake, and nothing else would make it act."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.polls_after_delivery = 0

    def app(self) -> Starlette:
        async def wake(request: Request) -> Response:
            got = WakeRequest.model_validate_json(await request.body())
            if got.reason is WakeReason.START:
                async with httpx.AsyncClient() as client:
                    booked = {"ref": "nudge", "at": (got.now + timedelta(hours=5)).isoformat()}
                    await client.post(f"{self.rig.base}/testsched/schedules", json=booked)
            return JSONResponse({"ok": True})

        async def report(request: Request) -> Response:
            if not self.rig.sched.fired:
                return Response(AgentReport(status=AgentStatus.IDLE).model_dump_json())
            self.polls_after_delivery += 1
            if self.polls_after_delivery <= 2:
                return Response(AgentReport(status=AgentStatus.WORKING).model_dump_json())
            async with httpx.AsyncClient() as client:
                said = {"to": "owner@example.com", "text": "The nudge came; acting on it."}
                await client.post(f"{self.rig.base}/testchat/messages", json=said)
            return Response(AgentReport(status=AgentStatus.DONE).model_dump_json())

        return Starlette(routes=[Route("/wake", wake, methods=["POST"]), Route("/report", report, methods=["GET"])])


async def test_a_wake_made_only_of_a_booking_waits_for_the_agent_to_act_on_the_delivery(rig: Rig) -> None:
    # Until a delivery was a wake the loop waited on: the booking fired, no report was asked for, and the clock
    # ran on to the deadline before the agent had done anything with what the scheduler delivered.
    agent = BookingAgent(rig)
    scn = scenario(ticket_fates=[])
    async with serving(agent.app()) as base:
        under_test = AgentUnderTest(
            name="booker", wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report"), Booked()]
        )
        clock = RecordingClock(scn.starts_at)
        store = rig.open("booked", clock)
        record = await run_scenario(
            scenario=scn,
            agent=under_test,
            reach=reach_for(under_test),
            store=store,
            clock=clock,
            services=rig.services(),
            replier=ScriptedReplier(scn),
            mounts=rig.board,
            poll_interval=0.001,
        )

    assert record.stop is StopReason.AGENT_DONE
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=5)]
    [acted] = [e for e in store.events() if e.actor is Actor.AGENT and e.entity.kind is EntityKind.MESSAGE]
    assert acted.wake == 2 and acted.sim_time == T0 + timedelta(hours=5)
    assert record.wakes[1].world_changes == 1
