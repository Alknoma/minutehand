"""`Reported` and `Polled` agents: real HTTP agents on an ephemeral port, driven by the real drivers."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters.agent.reach import reach_for
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.domain.agent import AgentReport, AgentStatus, AgentUnderTest, Polled, Reported, WakeReason, WakeRequest
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Scenario
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
        scenario=scn, agent=agent, reach=reach_for(agent), store=rig.open(run_id, clock), clock=clock,
        services=Services(providers=[]), replier=ScriptedReplier(scn), poll_interval=0.001,
    )


async def test_the_reported_driver_polls_through_working_and_takes_the_next_wake(rig: Rig, tmp_path: Path) -> None:
    agent = ReportingAgent()
    async with serving(agent.app()) as base:
        under_test = AgentUnderTest(name="reporter", wakes=[Reported(wake_url=f"{base}/wake",
                                                                      report_url=f"{base}/report")])
        record = await _run(rig, scenario(ticket_fates=[]), under_test, "reported")

    assert record.stop is StopReason.AGENT_DONE
    assert [(w.reason, w.now) for w in agent.wakes] == [(WakeReason.START, T0), (WakeReason.DUE, T0 + timedelta(days=1))]
    assert agent.wakes[0].goal is not None and agent.wakes[1].goal is None
    assert agent.polls == 6


async def test_a_reported_agent_answering_500_ends_the_run_agent_failed(rig: Rig) -> None:
    agent = ReportingAgent()
    agent.fail_wakes = True
    async with serving(agent.app()) as base:
        under_test = AgentUnderTest(name="reporter", wakes=[Reported(wake_url=f"{base}/wake",
                                                                      report_url=f"{base}/report")])
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
        scn = scenario(ticket_fates=[], deadline_after=timedelta(hours=6),
                       directions=[{"text": "Prioritise the legal review.", "after": timedelta(hours=2)}])
        record = await _run(rig, scn, under_test, "directed")
        heard = [w.direction for w in agent.requests]

    assert record.stop is StopReason.DEADLINE_PASSED
    assert agent.ticks == [(WakeReason.START, T0), (WakeReason.DIRECTION, T0 + timedelta(hours=2)),
                           (WakeReason.TICK, T0 + timedelta(hours=5))]
    assert heard == [None, "Prioritise the legal review.", None]
