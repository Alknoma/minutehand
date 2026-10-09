"""The scenario library: every entry loads, carries rules and names patterns that exist, and once filled with a team's values
is a scenario `minutehand validate` accepts; the team's values land where they are meant to and stay text; and the
`minutehand scenarios` command lists, shows and writes them out."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand import cli
from minutehand.application.files import problems
from minutehand.application.library import entries, entry, write
from minutehand.checks.patterns import PATTERNS
from minutehand.domain.library import LibraryScenario, TeamValues, Who, WhoRefused
from minutehand.domain.scenario import DispatchRule, PersonAsked, PersonPosts, PlannedBy, Relayed, Scripted, Silent

TEAM = TeamValues(
    goal="Get the quarterly figures signed off by Friday.",
    owner=Who.written("Priya Shah <priya@example.com>"),
    ask=Who.written("Tomas Berg <tomas@example.com>"),
    other=Who.written("Lena Fox <lena@example.com>"),
    answer="Signed off. The approval number is QF-7781.",
    tell="QF-7781",
    credential_env="FINANCE_APP_TOKEN",
    provider="microsoft",
    wakes=PlannedBy.BOOKED,
)
DEFAULTS = TeamValues(
    goal="Confirm the venue.",
    owner=Who.written("Owen Hart <owen@example.com>"),
    ask=Who.written("Rosa Lind <r@x.example>"),
)
NAMES = [e.name for e in entries()]


def test_the_library_holds_the_situations_it_names() -> None:
    assert NAMES == [
        "approval_rejected",
        "approver_never_decides",
        "deadline_moves_earlier",
        "person_answers_late",
        "person_answers_when_reminded",
        "person_away_with_delegate",
        "person_goes_quiet",
        "planned_wake_dropped",
        "planned_wake_late",
        "planned_wake_twice",
        "someone_else_writes_while_waiting",
    ]


@pytest.mark.parametrize("name", NAMES)
def test_each_entry_carries_rules_and_names_patterns_that_exist(name: str) -> None:
    found = entry(name)
    written = found.written(DEFAULTS)
    assert [r.id for r in written.assess] == found.rules and found.rules
    keys = {p.key for p in PATTERNS}
    assert set(found.patterns) <= keys, set(found.patterns) - keys
    assert {r.pattern for r in written.assess if r.pattern is not None} <= keys


@pytest.mark.parametrize("team", [TEAM, DEFAULTS], ids=["team", "defaults"])
@pytest.mark.parametrize("name", NAMES)
def test_each_entry_once_filled_passes_validate(name: str, team: TeamValues, tmp_path: Path) -> None:
    path = write(entry(name), team, tmp_path, replace=False)

    assert problems(path)[2] == []
    assert cli.main(["validate", str(path)]) == 0


def test_the_team_values_land_where_the_scenario_names_them() -> None:
    quiet = entry("person_goes_quiet").written(TEAM)
    assert (quiet.goal, quiet.owner) == ("Get the quarterly figures signed off by Friday.", "priya")
    [owner, asked] = quiet.people
    assert (owner.key, owner.name, owner.email) == ("priya", "Priya Shah", "priya@example.com")
    assert (asked.key, asked.email, asked.reply) == ("tomas", "tomas@example.com", Silent())
    assert quiet.expect[0] == PersonAsked(person="tomas", by=quiet.expect[0].by)

    late = entry("person_answers_late").written(TEAM)
    assert isinstance(late.people[1].reply, Scripted)
    assert late.people[1].reply.replies[0].facts == ["Signed off. The approval number is QF-7781."]
    assert late.expect[1] == Relayed(said_by="tomas", to="priya", holding=["QF-7781"])

    away = entry("person_away_with_delegate").written(TEAM)
    assert away.people[1].absences[0].delegate == "lena"
    assert away.people[1].absences[0].reason == "on leave"

    rejected = entry("approval_rejected").written(TEAM)
    assert rejected.people[2].credential is not None and rejected.people[2].credential.env == "FINANCE_APP_TOKEN"

    assert entry("planned_wake_late").written(TEAM).dispatch[0].wakes is PlannedBy.BOOKED
    [posted] = entry("someone_else_writes_while_waiting").written(TEAM).happenings
    assert isinstance(posted, PersonPosts) and (posted.provider, posted.person) == ("microsoft", "lena")


def test_a_goal_holding_yaml_and_braces_is_written_as_text(tmp_path: Path) -> None:
    goal = "Ship {the: thing}: [now], # not a comment, and {team.tell} stays as written"
    team = TEAM.model_copy(update={"goal": goal})
    path = write(entry("person_goes_quiet"), team, tmp_path, replace=False)

    assert problems(path)[1].goal == goal  # type: ignore[union-attr]


def test_the_written_file_says_what_it_is_for(tmp_path: Path) -> None:
    text = write(entry("planned_wake_dropped"), TEAM, tmp_path, replace=False).read_text()

    head = text.split("\nname:")[0]
    assert head.startswith("# planned_wake_dropped, from the Minutehand scenario library")
    assert "# Situation: The person asked never answers" in head
    flat = " ".join(line.removeprefix("# ") for line in head.splitlines())
    assert flat.index("Every run of it is assessed against what it declares below") < flat.index("Optional team policy")
    assert (
        "Optional team policy (in `assess` below, yours to edit or delete): follows_up_when_due, "
        "planned_to_be_back_when_due, not_done_while_waiting. Patterns: expiry_on_every_wait." in flat
    )
    assert all(line.startswith("#") or not line for line in head.splitlines())


def test_a_person_without_a_name_is_refused() -> None:
    with pytest.raises(WhoRefused, match="write it as 'Name <email>'"):
        Who.written("rosa@example.com")


def test_a_tell_the_answer_does_not_hold_is_refused() -> None:
    with pytest.raises(ValidationError, match="the tell 'XY-1' is not in the answer"):
        TeamValues.model_validate(TEAM.model_dump() | {"tell": "XY-1"})


def test_the_same_person_in_two_roles_is_refused() -> None:
    with pytest.raises(ValidationError, match=re.escape("owner and ask share the email 'priya@example.com'")):
        TeamValues.model_validate(
            TEAM.model_dump() | {"ask": {"key": "pat", "name": "Pat Shah", "email": "priya@example.com"}}
        )


def test_a_library_entry_naming_a_value_no_team_gives_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"names \{team.budget\}, which no team value fills"):
        LibraryScenario(
            situation="s",
            good_agent="g",
            patterns=["honest_closure"],
            scenario={"name": "x", "goal": "{team.budget}", "assess": [{"id": "r"}]},
        )


def test_a_library_entry_without_rules_is_refused() -> None:
    with pytest.raises(ValidationError, match="carries the rules that judge it"):
        LibraryScenario(situation="s", good_agent="g", patterns=["honest_closure"], scenario={"name": "x"})


def test_the_dispatch_rule_takes_the_team_wake_kind() -> None:
    [rule] = entry("planned_wake_twice").written(DEFAULTS).dispatch
    assert rule == DispatchRule(wakes=PlannedBy.REPORTED, fault=rule.fault, by=rule.by)


# -- the command -----------------------------------------------------------------------------------------------------

TEAM_ARGS = [
    "--goal",
    "Get it signed.",
    "--owner",
    "Priya Shah <priya@example.com>",
    "--ask",
    "Tomas Berg <t@example.com>",
]


def test_scenarios_lists_every_library_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios"]) == 0
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line and not line.startswith((" ", "minutehand"))] == NAMES


def test_scenarios_show_says_what_one_is_for(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios", "show", "approval_rejected"]) == 0
    out = capsys.readouterr().out
    assert (
        "rules: acts_only_once_approved, follows_up_when_due, not_done_while_waiting (in the scenario's `assess`" in out
    )
    assert "takes: --goal --owner --ask --other --answer --tell --credential-env" in out


def test_scenarios_new_all_writes_every_scenario(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios", "new", "--all", *TEAM_ARGS, "--out", str(tmp_path)]) == 0

    assert sorted(p.stem for p in tmp_path.glob("*.yaml")) == NAMES
    assert cli.main(["validate", *(str(p) for p in sorted(tmp_path.glob("*.yaml")))]) == 0
    assert str(tmp_path / "person_goes_quiet.yaml") in capsys.readouterr().out


def test_scenarios_new_over_an_existing_file_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["scenarios", "new", "person_goes_quiet", *TEAM_ARGS, "--out", str(tmp_path)]
    assert cli.main(args) == 0
    assert cli.main(args) == 2
    assert "exists; give --force to replace it" in capsys.readouterr().err
    assert cli.main([*args, "--force", "--goal", "Another goal."]) == 0
    assert "goal: Another goal." in (tmp_path / "person_goes_quiet.yaml").read_text()


@pytest.mark.parametrize(
    ("more", "said"),
    [
        (["--answer", "Done, ref AB-1."], "--answer and --tell go together"),
        (["--tell", "AB-1"], "--answer and --tell go together"),
        (["--other", "lena@example.com"], "write it as 'Name <email>'"),
    ],
)
def test_scenarios_new_with_values_that_do_not_fit_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], more: list[str], said: str
) -> None:
    assert cli.main(["scenarios", "new", "person_goes_quiet", *TEAM_ARGS, *more, "--out", str(tmp_path)]) == 2
    assert said in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_scenarios_new_of_a_name_not_in_the_library_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios", "new", "person_vanishes", *TEAM_ARGS]) == 2
    assert "no library scenario 'person_vanishes'; there are approval_rejected" in capsys.readouterr().err


def test_scenarios_new_with_neither_names_nor_all_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios", "new", *TEAM_ARGS]) == 2
    assert "name the scenarios to write, or give --all" in capsys.readouterr().err
