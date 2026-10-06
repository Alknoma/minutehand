"""A whole run, end to end: a real agent process, the real proxy, the Slack provider, the clock, the checks."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.checks.runner import stability
from minutehand.domain.checks import FindingKind, Stability
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Silent
from minutehand.domain.world import Actor
from tests.e2e.support import (
    ANSWER,
    FOLLOW_UP,
    NEVER_EXPECTED_TO_ANSWER,
    OWNER,
    QUESTION,
    SECRET_VARIABLE,
    SOFIA,
    T0,
    THANKS,
    agent_under_test,
    answers,
    messages,
    scenario,
    texts,
    world,
)


async def test_a_diligent_agent_and_a_person_who_answers_in_36_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    scn = scenario(answers(after=timedelta(hours=36)))

    [outcome] = await session.play(scn, launched.agent, state=tmp_path / "state", command=launched.command)

    record, result = outcome.record, outcome.result
    assert record.stop is StopReason.AGENT_DONE
    assert [f for f in result.findings if f.kind is FindingKind.FAIL] == []
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (2, 2)
    # START at T0, then the one moment the scenario makes due: the answer, 36 hours later. Nothing else.
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=36)]
    assert record.ended_at == T0 + timedelta(hours=36)

    store = world(tmp_path / "state", record.run_id)
    events = store.events()
    assert texts(messages(events, Actor.AGENT, to=SOFIA)) == [QUESTION, THANKS]
    assert texts(messages(events, Actor.PERSON)) == [ANSWER]
    assert [m.sim_time for m in messages(events, Actor.PERSON)] == [T0 + timedelta(hours=36)]
    assert texts(messages(events, Actor.AGENT, to=OWNER)) == [f"Thanks: the pricing is confirmed ({ANSWER})."]
    # Every Slack call the agent made reached the Slack fake through the proxy, and is in the log as the agent's.
    calls = store.calls()
    assert calls and {(c.provider, c.exchange.host) for c in calls} == {("slack", "slack.com")}
    assert {c.exchange.path.split("?")[0] for c in calls} >= {
        "/api/users.lookupByEmail",
        "/api/conversations.open",
        "/api/chat.postMessage",
    }
    called = [e for e in events if e.exchange is not None]
    assert called and {e.actor for e in called} == {Actor.AGENT}
    assert [e for e in events if e.actor is Actor.PERSON and e.exchange is not None] == []
    # The answer reached the agent as an event it verified with Slack's own SignatureVerifier.
    assert launched.state()["verified"] == [ANSWER] and launched.state()["rejected"] == 0


async def test_a_diligent_agent_follows_up_when_the_answer_comes_after_its_follow_up_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    scn = scenario(answers(after=timedelta(hours=60)))

    [outcome] = await session.play(scn, launched.agent, state=tmp_path / "state", command=launched.command)

    assert outcome.record.stop is StopReason.AGENT_DONE
    assert [w.sim_time for w in outcome.record.wakes] == [T0, T0 + timedelta(days=2), T0 + timedelta(hours=60)]
    events = world(tmp_path / "state", outcome.record.run_id).events()
    asked = messages(events, Actor.AGENT, to=SOFIA)
    assert texts(asked) == [QUESTION, FOLLOW_UP, THANKS]
    assert asked[1].sim_time == T0 + timedelta(days=2)
    assert [f for f in outcome.result.findings if f.check == "late_follow_up"] == []
    assert [f for f in outcome.result.findings if f.kind is FindingKind.FAIL] == []


async def test_an_agent_that_takes_its_goal_from_a_slack_dm_needs_no_wake_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "slack_only", by_message=True)
    scn = scenario(answers(after=timedelta(hours=36)), owner=NEVER_EXPECTED_TO_ANSWER)

    [outcome] = await session.play(scn, launched.agent, state=tmp_path / "state", command=launched.command)

    record = outcome.record
    # Nothing can report DONE without a report endpoint: the run plays on until nothing is due.
    assert record.stop is StopReason.NOTHING_PENDING
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=36)]
    events = world(tmp_path / "state", record.run_id).events()
    said = messages(events, Actor.PERSON)
    assert texts(said) == [scn.goal, ANSWER] and said[0].sim_time == T0
    assert texts(messages(events, Actor.AGENT, to=SOFIA)) == [QUESTION, THANKS]
    assert launched.state()["goal"] == scn.goal
    assert launched.state()["verified"] == [scn.goal, ANSWER]
    assert (outcome.result.effectiveness.expectations_met, outcome.result.effectiveness.expectations_total) == (2, 2)
    assert [f for f in outcome.result.findings if f.kind is FindingKind.FAIL] == []


async def test_two_samples_are_two_runs_through_one_proxy_each_from_the_agents_first_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent", hooks=True)
    scn = scenario(answers(after=timedelta(hours=36)))

    outcomes = await session.play(scn, launched.agent, state=tmp_path / "state", samples=2, command=launched.command)

    assert [o.record.stop for o in outcomes] == [StopReason.AGENT_DONE, StopReason.AGENT_DONE]
    first, second = (world(tmp_path / "state", o.record.run_id) for o in outcomes)
    assert first.run_id != second.run_id
    for store in (first, second):
        events = store.events()
        assert texts(messages(events, Actor.AGENT, to=SOFIA)) == [QUESTION, THANKS]
        assert len(store.calls()) == len([c for c in store.calls() if c.provider == "slack"]) > 0
    assert stability([o.result for o in outcomes]) == Stability(samples=2, passed=2), [
        o.result.verdict.words for o in outcomes
    ]


async def test_a_provider_neither_file_names_is_seeded_on_its_first_call_and_listed_as_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    monkeypatch.setenv(SECRET_VARIABLE, "unused: nothing is pushed to this agent")
    agent = launched.agent.model_copy(update={"inbound": []})

    [outcome] = await session.play(scenario(Silent()), agent, state=tmp_path / "state", command=launched.command)

    record = outcome.record
    assert record.stop is StopReason.NOTHING_PENDING, record.failure
    assert record.providers == ["slack"]
    store = world(tmp_path / "state", record.run_id)
    assert texts(messages(store.events(), Actor.AGENT, to=SOFIA)) == [QUESTION]
    seeded = [e for e in store.events() if e.actor is Actor.SCENARIO and e.entity.provider == "slack"]
    assert seeded and all(e.wake == 1 for e in seeded), "seeded during the first wake, when the agent first called"
    # The seed is the scenario's, not the call's: no call is tied to a scenario event.
    assert all(e.exchange is None for e in seeded)
