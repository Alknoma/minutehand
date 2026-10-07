"""The wake limit: the scenario's `max_wakes`, else sized from its deadline and the agent's own rhythm, else 20; and
a run that reaches it records how many and why."""

from __future__ import annotations

from datetime import timedelta

import pytest

from minutehand.domain.agent import AgentUnderTest, Polled
from minutehand.domain.run import DEFAULT_WAKES, StopReason, WakeLimit, wake_limit
from tests.orchestrator.rig import Rig, scenario


def _agent(*, tick: timedelta | None = None, polled: timedelta | None = None) -> AgentUnderTest:
    wakes = [Polled(wake_url="http://127.0.0.1:1/wake", every=polled)] if polled is not None else []
    return AgentUnderTest.model_validate(
        {"name": "a", "wakes": wakes or [{"kind": "command", "argv": ["x"]}], "tick": tick}
    )


@pytest.mark.parametrize(
    ("max_wakes", "deadline", "agent", "limit"),
    [
        (7, timedelta(days=5), _agent(tick=timedelta(hours=1)), WakeLimit(wakes=7, why="the scenario's max_wakes")),
        (
            None,
            timedelta(days=5),
            _agent(tick=timedelta(hours=1)),
            WakeLimit(
                wakes=120 + DEFAULT_WAKES,
                why="120 wakes of the agent's 1-hour rhythm before the scenario's deadline, and 20 more for what else "
                "wakes it",
            ),
        ),
        (
            None,
            timedelta(days=1),
            _agent(tick=timedelta(hours=2), polled=timedelta(minutes=30)),
            WakeLimit(
                wakes=48 + DEFAULT_WAKES,
                why="48 wakes of the agent's 30-minute rhythm before the scenario's deadline, and 20 more for what "
                "else wakes it",
            ),
        ),
        (
            None,
            None,
            _agent(tick=timedelta(hours=1)),
            WakeLimit(wakes=20, why="the default: the scenario sets no max_wakes and has no deadline to size one from"),
        ),
        (
            None,
            timedelta(days=5),
            _agent(),
            WakeLimit(
                wakes=20,
                why="the default: the scenario sets no max_wakes and has a deadline, but the agent file declares no "
                "tick to size one from",
            ),
        ),
    ],
    ids=["max_wakes", "deadline_and_tick", "the_shortest_rhythm", "no_deadline", "no_tick"],
)
def test_the_wake_limit_is_the_scenarios_or_sized_from_its_deadline_and_the_agents_rhythm(
    max_wakes: int | None, deadline: timedelta | None, agent: AgentUnderTest, limit: WakeLimit
) -> None:
    played = scenario(max_wakes=max_wakes, deadline_after=deadline, ticket_fates=[])
    assert wake_limit(played, agent) == limit


async def test_an_hourly_agent_with_a_tick_runs_to_its_deadline_and_one_without_stops_at_twenty(rig: Rig) -> None:
    played = scenario(ticket_fates=[], deadline_after=timedelta(days=1))
    ticking = rig.agent("keep_waking").model_copy(update={"tick": timedelta(hours=1)})

    record, _, _ = await rig.run(played, ticking)
    assert record.stop is StopReason.DEADLINE_PASSED and len(record.wakes) > DEFAULT_WAKES
    assert record.wake_limit is not None and record.wake_limit.wakes == 24 + DEFAULT_WAKES

    record, _, _ = await rig.run(played, rig.agent("keep_waking"), run_id="untimed")
    assert record.stop is StopReason.WAKE_LIMIT and len(record.wakes) == DEFAULT_WAKES
    assert record.wake_limit == WakeLimit(
        wakes=DEFAULT_WAKES,
        why="the default: the scenario sets no max_wakes and has a deadline, but the agent file declares no tick to "
        "size one from",
    )
