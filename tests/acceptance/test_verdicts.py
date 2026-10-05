"""A verdict never lies.

Each documented verdict (`docs/design.md`, "The verdict") from the smallest run that must produce it, its wording
and its exit code, read back by `minutehand findings` the same; and the cases that have happened: nothing judged
read "Passed", a missed follow-up passed with no finding, a broken fake or a dead emulator scored as the agent's
failure, and an agent's DONE trusted over an unanswered question nobody chased.
"""

from __future__ import annotations

from pathlib import Path

from minutehand.adapters.control.wire import CreateWorld
from minutehand.domain.run import VerdictKind
from tests.acceptance.support import (
    EMULATOR,
    EXIT,
    OWEN,
    PYTHON,
    ROSA,
    SILENT,
    TOLD_ONLY,
    WORDS,
    Finished,
    command_agent,
    installed_ledger_fake,
    minutehand,
    person,
    run_standin,
    scenario,
    scripted,
    seen,
    served,
    standin,
    through_proxy,
    write_yaml,
)

ANSWER = "The lakeside hall is booked."
RELAYED = [
    {"kind": "person_asked", "person": "rosa"},
    {"kind": "relayed", "said_by": "rosa", "to": "owen", "tell": "lakeside hall"},
]


def assert_verdict(finished: Finished, kind: str, state: Path) -> None:
    """The verdict is `kind`, its sentence begins as documented, the command exits by it, and `findings` reads the
    same verdict back with the same exit."""
    assert finished.verdict["kind"] == kind, finished.explain()
    assert str(finished.verdict["words"]).startswith(WORDS[kind]), finished.verdict["words"]
    assert finished.exit == EXIT[kind], finished.explain()
    again = minutehand("findings", finished.run_id, "--state", str(state))
    assert again.exit == EXIT[kind], again.explain()
    assert str(finished.verdict["words"]) in again.stdout, again.explain()


def test_an_answer_relayed_and_reported_done_is_passed_and_exits_0(tmp_path: Path) -> None:
    rosa = person("rosa", ROSA, scripted({"to_ask": 1, "text": ANSWER}, hours=5))
    story = scenario("passes", person("owen", OWEN, TOLD_ONLY), rosa, expect=RELAYED)
    finished = run_standin(tmp_path, story, standin(tmp_path, ask=ROSA, relay_to=OWEN))
    assert_verdict(finished, "passed", tmp_path / "state")
    assert finished.findings("fail") == []


def test_a_question_left_unanswered_and_never_chased_is_failed_with_no_follow_up_and_exits_1(tmp_path: Path) -> None:
    story = scenario("abandoned", person("owen", OWEN, TOLD_ONLY), person("rosa", ROSA, SILENT), expect=RELAYED[:1])
    finished = run_standin(tmp_path, story, standin(tmp_path, ask=ROSA))
    assert_verdict(finished, "failed", tmp_path / "state")
    assert [f["check"] for f in finished.findings("fail")] == ["no_follow_up"], finished.explain()


def test_an_agent_that_says_done_with_its_question_unanswered_and_unchased_is_not_finished_and_exits_3(
    tmp_path: Path,
) -> None:
    """Every expectation is met (Rosa was asked), the agent says DONE, and Rosa has not answered: not "Passed"."""
    rosa = person("rosa", ROSA, scripted({"to_ask": 1, "text": ANSWER}, hours=5))
    story = scenario("says_done", person("owen", OWEN, TOLD_ONLY), rosa, expect=RELAYED[:1])
    finished = run_standin(tmp_path, story, standin(tmp_path, ask=ROSA, done_at_start="1"))
    assert_verdict(finished, "unfinished", tmp_path / "state")
    assert "rosa" in str(finished.verdict["words"]), "the verdict names whose answer is still owed"


def test_a_fake_that_breaks_is_minutehands_failure_not_the_agents_and_exits_4(tmp_path: Path) -> None:
    """The agent also fails an expectation; the run still says nothing about the agent."""
    agent, env = command_agent(tmp_path, "POST https://ledger.example/entries")
    story = write_yaml(
        tmp_path / "scenario.yaml",
        scenario("broken_fake", person("owen", OWEN, TOLD_ONLY), expect=[{"kind": "person_asked", "person": "owen"}]),
    )
    state = tmp_path / "state"
    finished = minutehand(
        "run", str(story), "--agent", str(agent), "--state", str(state), "--json",
        env={**env, **installed_ledger_fake(tmp_path)}, cwd=tmp_path,
    )  # fmt: skip
    assert_verdict(finished, "tool_failed", state)
    assert "POST ledger.example/entries" in str(finished.verdict["words"]), "the verdict names the call that broke"
    assert seen(tmp_path)[0]["status"] == 500


def test_an_emulator_that_dies_is_the_environments_failure_not_the_agents_and_exits_2(tmp_path: Path) -> None:
    """The emulator answers one call and dies on the next; the agent also fails an expectation."""
    agent, env = command_agent(
        tmp_path,
        "POST https://api.payments.example/v1/customers",
        "GET https://api.payments.example/v1/customers",
        outbound=[{"host": "api.payments.example", "kind": "forward", "emulator": "payments"}],
        emulators=[
            {
                "name": "payments",
                "upstream": {"url": "http://127.0.0.1:{port}"},
                "command": [PYTHON, str(EMULATOR), "{port}", "--limit", "1"],
                "ready": {"kind": "http", "path": "/health", "status": 200},
                "ready_within": "PT20S",
                "health": {"every": "PT0.2S", "fails": 1, "path": "/health"},
            }
        ],
    )
    story = write_yaml(
        tmp_path / "scenario.yaml",
        scenario("dead_emulator", person("owen", OWEN, TOLD_ONLY), expect=[{"kind": "person_asked", "person": "owen"}]),
    )
    state = tmp_path / "state"
    finished = minutehand(
        "run", str(story), "--agent", str(agent), "--state", str(state), "--json", env=env, cwd=tmp_path
    )
    assert_verdict(finished, "environment_failed", state)
    assert [c["status"] for c in seen(tmp_path)] == [200, 502]


def test_a_standing_world_nobody_stepped_is_not_judged_and_findings_exits_5(tmp_path: Path) -> None:
    """A call reached the world, nothing was expected, no step was marked and the clock never moved."""
    with served(tmp_path) as server:
        spec = CreateWorld.model_validate(
            {"seed": {"people": [person("owen", OWEN, TOLD_ONLY)]}, "claims": {"tokens": ["xoxb-unjudged"]}}
        )
        world = server.client.create_world(spec)
        with through_proxy(server, tmp_path / "ca.pem") as http:
            answered = http.post("https://slack.com/api/auth.test", headers={"Authorization": "Bearer xoxb-unjudged"})
            assert answered.json()["ok"] is True
        checked = server.client.close_world(world.world_id)
        assert checked.result.verdict.kind is VerdictKind.NOT_JUDGED, checked.result.verdict
        assert checked.result.verdict.unjudged, "a verdict of not judged lists each reason"
        again = minutehand("findings", world.world_id, "--state", str(server.state))
        assert again.exit == EXIT["not_judged"], again.explain()
        assert "Passed" not in again.stdout


def test_a_run_with_nothing_expected_and_nothing_done_is_never_passed(tmp_path: Path) -> None:
    """No expectation, no question, no message: the agent woke once, did nothing, and said DONE. Nothing was judged."""
    story = scenario("nothing", person("owen", OWEN, TOLD_ONLY))
    finished = run_standin(tmp_path, story, standin(tmp_path, done_at_start="1"))
    assert finished.verdict["kind"] != "passed", finished.verdict["words"]
    assert finished.exit != EXIT["passed"]


def test_a_late_follow_up_on_a_question_answered_only_when_chased_is_scored_and_failed(tmp_path: Path) -> None:
    """Rosa takes a day to answer and answers only the follow-up; the agent follows up after 60 hours, 36 hours
    after her answer fell due. That follow-up is made, it is late, and a late follow-up is never a pass."""
    rosa = person("rosa", ROSA, scripted({"to_ask": 2, "text": ANSWER}, hours=24))
    story = scenario("late", person("owen", OWEN, TOLD_ONLY), rosa, expect=RELAYED)
    finished = run_standin(
        tmp_path, story, standin(tmp_path, ask=ROSA, relay_to=OWEN, follow_ups="1", every_hours="60")
    )
    scorecard = finished.result["effectiveness"]
    assert isinstance(scorecard, dict)
    assert (scorecard["follow_ups_made"], scorecard["follow_ups_late"]) == (1, 1), scorecard
    assert finished.verdict["kind"] == "failed", finished.verdict["words"]
    assert "late_follow_up" in [f["check"] for f in finished.findings("fail")]
