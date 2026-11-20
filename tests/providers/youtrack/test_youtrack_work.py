"""An issue assigned to a person is work they carry (`ports.transitions.CarriesWork`, docs/design-transitions.md phase
5), read as YouTrack holds it: due on the day its Due Date names, ranked by its Priority's place in its set, what its
State means of the work, each unresolved issue it depends on, and its estimate less its spent time."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from minutehand.adapters.providers.youtrack.state import issue_ref
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, EntityRef, TicketSnapshot
from tests.providers.youtrack.caller_world import SCENARIO, YOUTRACK_SEED, scenario_with, seeded
from tests.providers.youtrack.youtrack_instance import Instance

IRIS, TOMAS, NOOR = SCENARIO.people
NOTES, KEYNOTE, KITS = "Write the release notes", "Draft the keynote", "Ship the demo kits"


def _issue(team: Instance, summary: str) -> EntityRef:
    return issue_ref(next(i.id for i in team.youtrack.every_issue() if i.summary == summary))


def test_an_issue_assigned_and_unresolved_is_work_due_on_its_due_date_ranked_by_its_priority(team: Instance) -> None:
    [notes] = team.provider.items_for(TOMAS, team.store)
    assert (notes.item, notes.state, notes.work) == (_issue(team, NOTES), "Open", True)
    assert (notes.due, notes.rank) == (date(2026, 8, 26), 1), "Critical: second of Show-stopper to Minor"

    [keynote] = team.provider.items_for(IRIS, team.store)
    assert (keynote.item, keynote.state, keynote.work) == (_issue(team, KEYNOTE), "In Progress", True)
    assert (keynote.due, keynote.rank) == (None, 0), "no Due Date; its history made it a Show-stopper"

    assert team.provider.items_for(NOOR, team.store) == [], "her venue is resolved: nothing is hers to carry"


def test_in_progress_means_started_and_every_other_unresolved_state_open(team: Instance) -> None:
    notes = team.provider.legal(_issue(team, NOTES), Actor.PERSON, TOMAS, team.store)
    assert [(o.name, o.means) for o in notes if o.unprompted] == [
        ("In Progress", TicketState.STARTED),
        ("Fixed", TicketState.DONE),
        ("Won't fix", TicketState.CANCELLED),
    ]
    kits = team.provider.legal(_issue(team, KITS), Actor.PERSON, TOMAS, team.store)
    assert [(o.name, o.means) for o in kits if o.unprompted] == [
        ("Doing", TicketState.OPEN),
        ("Shipped", TicketState.DONE),
        ("Dropped", TicketState.CANCELLED),
    ], "Field Ops's own States mean only what its seed says of them"
    keynote = _issue(team, KEYNOTE)
    snapshots = [e.after for e in team.store.events() if e.entity == keynote and isinstance(e.after, TicketSnapshot)]
    assert snapshots[-1].state is TicketState.STARTED, "its history moved it In Progress"


def test_a_state_a_seed_says_is_started_is_offered_as_started(tmp_path: Path) -> None:
    projects = json.loads(json.dumps(YOUTRACK_SEED["projects"]))
    states = next(f for f in projects[1]["fields"] if f["name"] == "State")
    next(v for v in states["values"] if v["name"] == "Doing")["means"] = "started"
    team = seeded(tmp_path, scenario_with(projects=projects))

    kits = team.provider.legal(_issue(team, KITS), Actor.PERSON, TOMAS, team.store)

    assert next((o.name, o.means) for o in kits if o.unprompted) == ("Doing", TicketState.STARTED)


async def test_an_issue_is_held_by_an_unresolved_issue_it_depends_on_until_that_one_is_resolved(
    tmp_path: Path,
) -> None:
    issues = json.loads(json.dumps(YOUTRACK_SEED["issues"]))
    next(i for i in issues if i["ticket"] == "notes")["links"] = [{"phrase": "depends on", "ticket": "kits"}]
    team = seeded(tmp_path, scenario_with(issues=issues))
    notes, kits = _issue(team, NOTES), _issue(team, KITS)

    assert team.provider.holding(notes, team.store) == [kits], "the kits are required for the notes, and not shipped"
    assert team.provider.holding(kits, team.store) == [], "the kits depend on nothing"
    assert team.provider.holding(_issue(team, KEYNOTE), team.store) == [], "a subtask of the notes is held by nothing"

    await team.provider.apply(kits, "Shipped", Actor.PERSON, IRIS, "{}", team.store, team.clock)

    assert team.provider.holding(notes, team.store) == [], "the kits are shipped, a resolved state"


def test_the_work_left_is_the_estimate_less_the_spent_time(tmp_path: Path) -> None:
    issues = json.loads(json.dumps(YOUTRACK_SEED["issues"]))
    for detail, estimation, spent in (("notes", "3d", "1d"), ("keynote", "1h", "3h")):
        found = next(i for i in issues if i["ticket"] == detail)
        kept = [f for f in found["fields"] if f["field"] != "Estimation"]
        found["fields"] = [*kept, {"field": "Estimation", "value": estimation}, {"field": "Spent time", "value": spent}]
    team = seeded(tmp_path, scenario_with(issues=issues))

    assert team.provider.estimate(_issue(team, NOTES), team.store) == timedelta(hours=16), "3d less 1d, 8h a day"
    assert team.provider.estimate(_issue(team, KEYNOTE), team.store) == timedelta(0), "spent past it: none left"


def test_the_work_left_is_the_whole_estimate_while_nothing_is_spent_and_none_without_one(team: Instance) -> None:
    assert team.provider.estimate(_issue(team, NOTES), team.store) == timedelta(hours=10), "1d 2h, none spent"
    assert team.provider.estimate(_issue(team, KEYNOTE), team.store) is None, "no Estimation"
    assert team.provider.estimate(_issue(team, KITS), team.store) is None, "Field Ops carries no period field"


def test_a_project_that_tracks_no_time_holds_no_estimate_to_read(tmp_path: Path) -> None:
    projects = json.loads(json.dumps(YOUTRACK_SEED["projects"]))
    projects[0]["time_tracking"] = {"enabled": False}
    team = seeded(tmp_path, scenario_with(projects=projects))

    assert team.provider.estimate(_issue(team, NOTES), team.store) is None, "1d 2h in Estimation, time not tracked"
