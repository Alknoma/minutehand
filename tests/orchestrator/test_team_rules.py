"""A team's own policy, written as YAML rules, judges an agent that chases people for their budgets: the policy of a
real agent, which follows up at 24 and 48 hours, escalates at 72, never acknowledges an answer, and stops once it has
escalated. Nothing of Minutehand's judges how it behaves: the built-in checks that once did failed the correct agent
(for never acknowledging, for not sending a third reminder), and could not be told otherwise."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import yaml

from minutehand.application.files import load_scenario
from minutehand.checks.runner import RunResult, evaluate_run
from minutehand.domain.agent import AgentUnderTest, Command
from minutehand.domain.checks import FindingKind
from minutehand.domain.people import InboundTarget
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import Scenario
from minutehand.session import rules_for
from tests.orchestrator.rig import AGENTS, Rig, rigged
from tests.orchestrator.world import CHAT

HERE = Path(__file__).parent / "budget"


def _scenario() -> Scenario:
    written = load_scenario(HERE / "scenario.yaml")
    assert written.starts_at is not None
    return written.starting(written.starts_at)


def _agent(rig: Rig, behaviour: str, policy: str | None) -> AgentUnderTest:
    rules = yaml.safe_load((HERE / policy).read_text())["assess"] if policy is not None else []
    return AgentUnderTest.model_validate(
        {
            "name": "budget_chaser",
            "wakes": [Command(argv=[sys.executable, str(AGENTS / "chaser.py"), behaviour]).model_dump()],
            "inbound": [InboundTarget(provider=CHAT, url=f"{rig.base}/{CHAT}/pushed").model_dump()],
            "tick": timedelta(hours=1),
            "assess": rules,
        }
    )


async def _judged(tmp_path: Path, behaviour: str, policy: str | None) -> RunResult:
    played = _scenario()
    async with rigged(tmp_path) as rig:
        agent = _agent(rig, behaviour, policy)
        record, store, _ = await rig.run(played, agent, env=rig.env(LEADS="ana,ben"))
    assert record.stop is not StopReason.WAKE_LIMIT, record.wake_limit
    return evaluate_run(
        played, store.events(), record.wakes, store.replies(), stop=record.stop, rules=rules_for(agent, played)
    )


def _failed(result: RunResult) -> list[tuple[str, str]]:
    return [(f.check, f.message) for f in result.findings if f.kind is FindingKind.FAIL]


async def test_the_correct_agent_passes_its_teams_policy(tmp_path: Path) -> None:
    result = await _judged(tmp_path, "correct", "policy.yaml")

    assert _failed(result) == []
    assert result.verdict.kind is VerdictKind.PASSED, result.verdict.words
    assert result.assessed_by == [
        "follows_up_at_a_day_and_two",
        "follows_up_twice_at_most",
        "escalates_at_three_days",
        "tells_the_owner_every_answer",
    ]
    assert result.effectiveness.follow_ups_made == 2


async def test_an_agent_that_nags_every_hour_fails_its_teams_policy(tmp_path: Path) -> None:
    result = await _judged(tmp_path, "nags", "policy.yaml")

    assert _failed(result) == [("follows_up_twice_at_most", "ben was followed up 48 times; we follow up twice")]
    assert result.verdict.kind is VerdictKind.FAILED


async def test_an_agent_that_never_escalates_fails_its_teams_policy(tmp_path: Path) -> None:
    result = await _judged(tmp_path, "never_escalates", "policy.yaml")

    assert _failed(result) == [
        ("escalates_at_three_days", "ben had not answered at three days and the owner heard 0 times within the hour"),
        ("tells_the_owner_every_answer", "the owner was never told what ana answered"),  # it waits on ben forever
    ]


async def test_a_team_that_does_not_mind_nagging_passes_the_same_agent(tmp_path: Path) -> None:
    result = await _judged(tmp_path, "nags", "lenient.yaml")

    assert _failed(result) == []
    assert result.verdict.kind is VerdictKind.PASSED, result.verdict.words


async def test_with_no_assessment_the_run_reports_facts_and_says_nothing_was_assessed(tmp_path: Path) -> None:
    result = await _judged(tmp_path, "nags", None)

    assert result.findings == []
    assert result.assessed_by == []
    assert result.verdict.kind is VerdictKind.NOT_JUDGED
    assert result.verdict.words.startswith("Not assessed: nothing judged this run")
    assert result.effectiveness.follow_ups_made == 48
    assert result.effectiveness.waits_opened == 2
