"""An issue assigned to a person is work they carry (`ports.transitions.CarriesWork`, docs/design-transitions.md phase
5), read as Jira holds it: due on its due date, ranked by its priority's place among the site's priorities, what each
status's category says of the work, each unfinished issue that blocks it, and its remaining estimate."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from minutehand.adapters.providers.jira import wire
from minutehand.adapters.providers.jira.provider import build
from minutehand.adapters.providers.jira.state import JiraWorld, issue_ref
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, TicketState
from minutehand.domain.world import Actor, EntityRef, TicketSnapshot
from tests.providers.jira.jira_site import API, IRIS_TOKEN, SCENARIO, START, Site, basic, ok

DAY = wire.HOURS_A_DAY * 3600
"""A day of work in seconds, as `3d` is read on this site."""


def _person(key: str) -> Person:
    return next(p for p in SCENARIO.people if p.key == key)


def _ref(site: Site, key: str) -> EntityRef:
    issue = site.jira.find_issue(key)
    assert issue is not None
    return issue_ref(issue.id)


def _snapshots(site: Site, key: str) -> list[TicketSnapshot]:
    item = _ref(site, key)
    return [e.after for e in site.store.events() if e.entity == item and isinstance(e.after, TicketSnapshot)]


def test_an_issue_assigned_and_not_done_is_work_due_on_its_due_date_ranked_by_its_priority(site: Site) -> None:
    [notes] = site.provider.items_for(_person("tomas"), site.store)
    assert (notes.item, notes.state, notes.work) == (_ref(site, "LAUNCH-1"), "To Do", True)
    assert (notes.due, notes.rank) == (date(2026, 8, 28), 1), "seeded High: second of Highest to Lowest"

    [vault] = site.provider.items_for(_person("iris"), site.store)
    assert (vault.item, vault.state, vault.work) == (_ref(site, "VAULT-1"), "In Progress", True)
    assert (vault.due, vault.rank) == (None, 2), "no due date, and the site's default priority, Medium"

    assert site.provider.items_for(_person("noor"), site.store) == [], "her venue is done: nothing is hers to carry"


def test_each_status_means_what_its_category_says_of_the_work(site: Site) -> None:
    meanings = {s.name: (s.category, s.outcome) for s in site.jira.site().statuses}
    assert meanings == {
        "To Do": (wire.Category.NEW, TicketState.OPEN),
        "In Progress": (wire.Category.INDETERMINATE, TicketState.STARTED),
        "In Review": (wire.Category.INDETERMINATE, TicketState.STARTED),
        "Done": (wire.Category.DONE, TicketState.DONE),
        "Won't Do": (wire.Category.DONE, TicketState.CANCELLED),
    }, "Won't Do is a done status declared cancelled"


async def test_an_in_progress_status_is_offered_and_read_as_started(site: Site) -> None:
    tomas = _person("tomas")
    offers = site.provider.legal(_ref(site, "LAUNCH-1"), Actor.PERSON, tomas, site.store)
    assert [(o.name, o.to_state, o.means) for o in offers if o.unprompted] == [
        ("Start work", "In Progress", TicketState.STARTED),
        ("Drop", "Won't Do", TicketState.CANCELLED),
    ]
    vault = site.provider.legal(_ref(site, "VAULT-1"), Actor.PERSON, _person("iris"), site.store)
    assert [(o.name, o.means) for o in vault if o.unprompted] == [
        ("To Do", TicketState.OPEN),
        ("Done", TicketState.DONE),
        ("Won't Do", TicketState.CANCELLED),
    ], "the default workflow, from In Progress"
    assert _snapshots(site, "VAULT-1")[-1].state is TicketState.STARTED, "seeded In Progress"

    await site.provider.apply(_ref(site, "LAUNCH-1"), "Start work", Actor.PERSON, tomas, "{}", site.store, site.clock)

    assert _snapshots(site, "LAUNCH-1")[-1].state is TicketState.STARTED
    [still] = site.provider.items_for(tomas, site.store)
    assert (still.state, still.work) == ("In Progress", True), "started is not finished: it is still his to carry"


async def test_an_issue_is_held_by_an_unfinished_issue_that_blocks_it_until_that_one_is_done(site: Site) -> None:
    # The seed has LAUNCH-1 block LAUNCH-2: the blocked end is held back, the blocking end is not.
    assert site.provider.holding(_ref(site, "LAUNCH-2"), site.store) == [_ref(site, "LAUNCH-1")]
    assert site.provider.holding(_ref(site, "LAUNCH-1"), site.store) == []

    async with site.client(basic("iris@example.com", IRIS_TOKEN)) as iris:  # the agent cannot see Vault
        for kind, outward in (("Blocks", "FIELD-1"), ("Relates", "LAUNCH-1")):
            link = {"type": {"name": kind}, "outwardIssue": {"key": outward}, "inwardIssue": {"key": "VAULT-1"}}
            ok(await iris.post(f"{API}/issueLink", json=link), 201)
        assert site.provider.holding(_ref(site, "VAULT-1"), site.store) == [_ref(site, "FIELD-1")], (
            "FIELD-1 blocks it; LAUNCH-1 only relates to it"
        )

        transitions = ok(await iris.get(f"{API}/issue/FIELD-1/transitions"))["transitions"]
        done = next(t["id"] for t in transitions if t["to"]["name"] == "Done")
        ok(await iris.post(f"{API}/issue/FIELD-1/transitions", json={"transition": {"id": done}}), 204)

    assert site.provider.holding(_ref(site, "VAULT-1"), site.store) == [], "FIELD-1 is done: nothing holds it now"


def test_the_work_left_is_the_original_estimate_less_the_time_logged(tmp_path: Path) -> None:
    seed = json.loads(SCENARIO.provider_seeds[0].body)
    vault = next(i for i in seed["issues"] if i["ticket"] == "vault")
    vault["estimate_seconds"], vault["spent_seconds"] = 3 * DAY, DAY
    scenario = SCENARIO.model_copy(
        update={"provider_seeds": [SCENARIO.provider_seeds[0].model_copy(update={"body": json.dumps(seed)})]}
    )
    store = SqliteStore(tmp_path / "w.db", "w", RunClock(START))
    provider = build()
    provider.seed(scenario, store)
    issue = JiraWorld(store).find_issue("VAULT-1")
    assert issue is not None

    assert provider.estimate(issue_ref(issue.id), store) == timedelta(seconds=2 * DAY), "3d estimated, 1d logged"
    store.close()


async def test_the_work_left_is_what_a_worklog_set_the_remaining_estimate_to(site: Site) -> None:
    notes = _ref(site, "LAUNCH-1")
    assert site.provider.estimate(notes, site.store) == timedelta(hours=5), "8h estimated, 3h logged"

    log = {"started": "2026-08-24T09:00:00.000+0000", "timeSpent": "1h"}
    params = {"adjustEstimate": "new", "newEstimate": "2h"}
    ok(await site.http.post(f"{API}/issue/LAUNCH-1/worklog", json=log, params=params), 201)

    assert site.provider.estimate(notes, site.store) == timedelta(hours=2), "what Jira holds left, not 8h less 4h"


def test_an_issue_with_no_estimate_has_no_work_left_to_read(site: Site) -> None:
    assert site.provider.estimate(_ref(site, "VAULT-1"), site.store) is None
