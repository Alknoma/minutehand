"""The scenario library: every entry is a world (no goal, owner, expectation or rule of its own) and once filled with a
team's people is a scenario `minutehand validate` accepts; the team's people land where they are meant to and stay
text; and the `minutehand scenarios` command lists, shows and writes them out."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand import cli
from minutehand.application.files import problems
from minutehand.application.library import entries, entry, write
from minutehand.domain.library import LibraryScenario, TeamValues, Who, WhoRefused
from minutehand.domain.scenario import DispatchRule, PersonPosts, PlannedBy, Scripted, Silent

TEAM = TeamValues(
    person=Who.written("Tomas Berg <tomas@example.com>"),
    other=Who.written("Lena Fox <lena@example.com>"),
    knows="Signed off. The approval number is QF-7781.",
    credential_env="FINANCE_APP_TOKEN",
    provider="microsoft",
    wakes=PlannedBy.BOOKED,
)
DEFAULTS = TeamValues(person=Who.written("Rosa Lind <r@x.example>"))
NAMES = [e.name for e in entries()]


def test_the_library_holds_the_situations_it_names() -> None:
    assert NAMES == [
        "approval_rejected",
        "approver_never_decides",
        "date_moves_earlier",
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
def test_each_entry_is_a_world_watched_for_a_window_with_people_who_have_profiles(name: str) -> None:
    world = entry(name).written(DEFAULTS)

    assert (world.goal, world.owner, world.deadline_after) == (None, None, None)
    assert (world.expect, world.assess) == ([], [])
    assert world.runs_for is not None
    assert all(p.profile for p in world.people), "each person is described, not only scripted"


@pytest.mark.parametrize("team", [TEAM, DEFAULTS], ids=["team", "defaults"])
@pytest.mark.parametrize("name", NAMES)
def test_each_entry_once_filled_passes_validate(name: str, team: TeamValues, tmp_path: Path) -> None:
    path = write(entry(name), team, tmp_path, replace=False)

    assert problems(path)[2] == []
    assert cli.main(["validate", str(path)]) == 0


def test_the_team_people_land_where_the_scenario_names_them() -> None:
    [quiet] = entry("person_goes_quiet").written(TEAM).people
    assert (quiet.key, quiet.name, quiet.email, quiet.reply) == ("tomas", "Tomas Berg", "tomas@example.com", Silent())
    assert quiet.facts == ["Signed off. The approval number is QF-7781."]

    [late] = entry("person_answers_late").written(TEAM).people
    assert isinstance(late.reply, Scripted) and late.reply.replies[0].facts == [TEAM.knows]

    away, cover = entry("person_away_with_delegate").written(TEAM).people
    assert (away.absences[0].delegate, away.absences[0].reason, cover.key) == ("lena", "on leave", "lena")

    [approver] = entry("approval_rejected").written(TEAM).people
    assert approver.key == "lena" and approver.credential is not None
    assert approver.credential.env == "FINANCE_APP_TOKEN"

    assert entry("planned_wake_late").written(TEAM).dispatch[0].wakes is PlannedBy.BOOKED
    [posted] = entry("someone_else_writes_while_waiting").written(TEAM).happenings
    assert isinstance(posted, PersonPosts) and (posted.provider, posted.person) == ("microsoft", "lena")


def test_a_fact_holding_yaml_and_braces_is_written_as_text(tmp_path: Path) -> None:
    knows = "Ship {the: thing}: [now], # not a comment, and {team.knows} stays as written"
    path = write(entry("person_goes_quiet"), TEAM.model_copy(update={"knows": knows}), tmp_path, replace=False)

    assert problems(path)[1].people[0].facts == [knows]  # type: ignore[union-attr]


def test_the_written_file_says_it_is_a_world(tmp_path: Path) -> None:
    text = write(entry("planned_wake_dropped"), TEAM, tmp_path, replace=False).read_text()

    head = text.split("\nname:")[0]
    assert head.startswith("# planned_wake_dropped, from the Minutehand scenario library")
    assert "# Situation: The first wake the agent plans for itself is never delivered" in head
    flat = " ".join(line.removeprefix("# ") for line in head.splitlines())
    assert "A world: it hands the agent no work. The agent brings its own" in flat
    assert all(line.startswith("#") or not line for line in head.splitlines())


def test_a_person_without_a_name_is_refused() -> None:
    with pytest.raises(WhoRefused, match="write it as 'Name <email>'"):
        Who.written("rosa@example.com")


def test_the_same_person_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match=re.escape("person and other share the email 'tomas@example.com'")):
        TeamValues.model_validate(
            TEAM.model_dump() | {"other": {"key": "pat", "name": "Pat Berg", "email": "tomas@example.com"}}
        )


def test_a_library_entry_naming_a_value_no_team_gives_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"names \{team.budget\}, which no team value fills"):
        LibraryScenario(situation="s", scenario={"name": "x", "people": [{"name": "{team.budget}"}]})


@pytest.mark.parametrize("handed", ["goal", "owner", "deadline_after", "expect", "assess"])
def test_a_library_entry_that_hands_the_agent_work_or_judges_it_is_refused(handed: str) -> None:
    with pytest.raises(ValidationError, match=f"a library scenario is a world: .* so it has no {handed}"):
        LibraryScenario(situation="s", scenario={"name": "x", handed: "anything"})


def test_the_dispatch_rule_takes_the_team_wake_kind() -> None:
    [rule] = entry("planned_wake_twice").written(DEFAULTS).dispatch
    assert rule == DispatchRule(wakes=PlannedBy.REPORTED, fault=rule.fault, by=rule.by)


# -- the command -----------------------------------------------------------------------------------------------------

TEAM_ARGS = ["--person", "Tomas Berg <t@example.com>"]


def test_scenarios_lists_every_library_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios"]) == 0
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line and not line.startswith((" ", "minutehand"))] == NAMES


def test_scenarios_show_says_what_situation_one_puts_the_agent_in(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios", "show", "approval_rejected"]) == 0
    out = capsys.readouterr().out
    assert "Situation: An approver in the agent's own product turns an item down, with a reason." in out
    assert "a world: it hands the agent no work" in out
    assert "takes: --other --credential-env" in out


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
    assert cli.main([*args, "--force", "--knows", "The code is ZZ-9."]) == 0
    assert "The code is ZZ-9." in (tmp_path / "person_goes_quiet.yaml").read_text()


def test_scenarios_new_with_a_person_that_does_not_read_as_one_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["scenarios", "new", "person_goes_quiet", "--person", "t@example.com", "--out", str(tmp_path)]) == 2
    assert "write it as 'Name <email>'" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_scenarios_new_of_a_name_not_in_the_library_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios", "new", "person_vanishes", *TEAM_ARGS]) == 2
    assert "no library scenario 'person_vanishes'; there are approval_rejected" in capsys.readouterr().err


def test_scenarios_new_with_neither_names_nor_all_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios", "new", *TEAM_ARGS]) == 2
    assert "name the scenarios to write, or give --all" in capsys.readouterr().err
