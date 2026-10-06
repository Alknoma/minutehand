"""Whoever drives the agent relays the agent's own report of its work (`AgentReport`, what `minutehand run` asks the
agent at the end of every wake): whether it is done, and what it still holds open. The world cannot say either.

Before, a standing world closed with every wait settled read Passed, "with nothing left open", while the service's
own record of the work was still open: the harness knew, and had no way to say."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.control.wire import CreateWorld
from minutehand.domain.agent import AgentReport, AgentStatus, Commitment, WaitingOn
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld, open_case
from tests.serve.support import Served, dm, event_receiver, spec


@pytest.fixture(scope="module")
def state(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("reported")


@pytest.fixture(scope="module")
def served(state: Path) -> Iterator[Served]:
    with serve_in_background(state) as url, MinutehandClient(url) as client:
        yield Served(url=url, client=client, environment=client.environment())


def _spec(token: str, inbound: str) -> CreateWorld:
    return spec(token, inbound=inbound, scripted={"sofia": "Yes, it is."})


def _asked_and_answered(served: Served, world: OpenWorld, token: str) -> None:
    """The agent asks Sofia, she answers an hour later, and the agent looks again: nothing is left open in the world."""
    with world.step(at=world.view.now, reason="the ask"):
        served.slack(token).chat_postMessage(channel=dm(served, token, "sofia@example.com"), text="Is the venue set?")
    world.advance(timedelta(hours=2))
    with world.step(reason="the look after her answer"):
        pass


def _still_open(world: OpenWorld) -> AgentReport:
    return AgentReport(
        status=AgentStatus.IDLE,
        commitments=[
            Commitment(
                key="venue",
                description="The venue is booked",
                waiting_on=WaitingOn.RESULT,
                opened_at=world.view.now,
            )
        ],
    )


def test_work_the_agent_still_holds_open_keeps_a_settled_world_from_passing(served: Served, state: Path) -> None:
    token = "xoxb-reported-open"
    with event_receiver() as receiver:
        world = OpenWorld(served.client, served.client.create_world(_spec(token, receiver.url)))
        _asked_and_answered(served, world, token)
        before = world.checks().result.verdict
        assert before.kind is VerdictKind.PASSED, before.words

        relayed = world.report(_still_open(world)).result.verdict
        closed = world.close().result.verdict

    assert relayed.kind is VerdictKind.UNFINISHED and relayed.open_commitments == 1
    assert closed.kind is VerdictKind.UNFINISHED and "1 commitment still open" in closed.words
    record = session.load(state, world.world_id).record
    assert record.stop is StopReason.CLOSED
    assert record.reported is not None and [c.key for c in record.reported.commitments or []] == ["venue"]


def test_the_agents_word_that_it_is_done_is_how_the_run_stopped(served: Served, state: Path) -> None:
    token = "xoxb-reported-done"
    with event_receiver() as receiver:
        world = OpenWorld(served.client, served.client.create_world(_spec(token, receiver.url)))
        _asked_and_answered(served, world, token)
        world.report(_still_open(world))
        world.report(AgentReport(status=AgentStatus.DONE, commitments=[]))  # the latest report stands

        closed = world.close().result.verdict

    assert closed.kind is VerdictKind.PASSED and closed.stop is StopReason.AGENT_DONE
    assert "the agent reported it was done" in closed.words
    assert session.load(state, world.world_id).record.stop is StopReason.AGENT_DONE


def test_a_report_through_any_world_of_a_case_is_the_cases(served: Served, state: Path) -> None:
    token = "xoxb-reported-case"
    with event_receiver() as receiver:
        case = open_case(served.client, "reported", [_spec(token, receiver.url)])
        [world] = case.worlds
        _asked_and_answered(served, world, token)

        case.report(_still_open(world))
        closed = case.close().result.verdict

    assert closed.kind is VerdictKind.UNFINISHED and closed.open_commitments == 1
    assert session.load(state, case.case_id).record.reported is not None
