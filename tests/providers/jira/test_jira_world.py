"""The site lives in the store and nowhere else; the provider meets its ports, claims its hosts, and lets a
person act on an issue at a moment of the run, recorded as that person."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.jira import state
from minutehand.adapters.providers.jira.manifest import MANIFEST
from minutehand.adapters.providers.jira.provider import build
from minutehand.adapters.providers.jira.state import JiraWorld
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.provider import Tier
from minutehand.domain.scenario import Scenario, TicketState
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation, TicketSnapshot
from minutehand.ports.provider import EditsTickets, HoldsTickets, Provider
from tests.providers.jira.jira_site import API, IRIS, NOOR, SCENARIO, START, TOMAS, Site, ok


def _ref(site: Site, key: str) -> state.EntityRef:
    issue = site.jira.find_issue(key)
    assert issue is not None
    return state.issue_ref(issue.id)


def test_seeding_writes_the_site_as_the_scenario_with_each_tickets_meaning(site: Site) -> None:
    events = site.store.events()
    assert {e.actor for e in events} == {Actor.SCENARIO}
    tickets = [e.after for e in events if e.entity.kind is EntityKind.TICKET]
    assert tickets == [
        TicketSnapshot(
            title="Write the release notes",
            body="Cover the new export.\nLink the changelog.",
            project="LAUNCH",
            assignee_email="tomas@example.com",
        ),
        TicketSnapshot(
            title="Book the venue", project="LAUNCH", assignee_email="noor@example.com", state=TicketState.DONE
        ),
        TicketSnapshot(title="Ship the demo kits", project="FIELD"),
        TicketSnapshot(title="Rotate the vault keys", project="VAULT", assignee_email="iris@example.com"),
    ]
    comments = [e.after for e in events if e.entity.kind is EntityKind.COMMENT]
    assert [c.text for c in comments if isinstance(c, MessageSnapshot)] == ["Draft is in the shared folder."]


def test_seeding_twice_gives_the_same_ids(tmp_path: Path) -> None:
    def ids(run: str) -> list[str]:
        store = SqliteStore(tmp_path / f"{run}.db", run, RunClock(START))
        build().seed(SCENARIO, store)
        return [e.entity.external_id for e in store.events()]

    assert ids("one") == ids("two")


def test_a_seed_naming_nobody_is_refused(tmp_path: Path) -> None:
    broken = Scenario.model_validate(
        {
            **SCENARIO.model_dump(),
            "provider_seeds": [{"provider": "jira", "body": {"credentials": [{"account": "ghost", "api_token": "t"}]}}],
        }
    )
    with pytest.raises(ValueError, match="'ghost', who is not the agent"):
        build().seed(broken, SqliteStore(tmp_path / "w.db", "run", RunClock(START)))


async def test_a_fork_sees_issues_only_up_to_the_fork(site: Site) -> None:
    body = {"fields": {"project": {"key": "FIELD"}, "issuetype": {"name": "Task"}}}
    ok(await site.http.post(f"{API}/issue", json={"fields": {**body["fields"], "summary": "Before"}}), 201)
    at = site.store.head()
    after = ok(await site.http.post(f"{API}/issue", json={"fields": {**body["fields"], "summary": "After"}}), 201)
    fork = site.store.fork("what-if", at_seq=at, clock=RunClock(START))
    forked = JiraWorld(fork)
    assert [i.summary for i in forked.issues(forked.find_project("FIELD").id)] == ["Ship the demo kits", "Before"]  # type: ignore[union-attr]
    assert forked.find_issue(after["key"]) is None
    assert forked.next_number(forked.find_project("FIELD").id) == 3, "a fork reuses no key"  # type: ignore[union-attr]


@pytest.mark.parametrize(("to", "status", "resolution"), [(TicketState.DONE, "Done", "Done"),
                                                          (TicketState.CANCELLED, "Won't Do", "Won't Do")])  # fmt: skip
async def test_transition_walks_the_workflow_as_the_assignee(
    site: Site, to: TicketState, status: str, resolution: str
) -> None:
    site.clock.jump(START + timedelta(days=2))
    site.provider.transition(_ref(site, "LAUNCH-1"), to, site.store, site.clock)

    last = site.store.events()[-1]
    assert (last.actor, last.operation) == (Actor.PERSON, Operation.UPDATE)
    assert isinstance(last.after, TicketSnapshot) and last.after.state is to
    read = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "status,resolution,resolutiondate"}))
    assert read["fields"]["status"]["name"] == status and read["fields"]["resolution"]["name"] == resolution
    assert read["fields"]["resolutiondate"] == "2026-08-26T10:50:03.000+0000"
    log = ok(await site.http.get(f"{API}/issue/LAUNCH-1/changelog"))["values"][1:]
    assert {e["author"]["accountId"] for e in log} == {TOMAS}
    assert {e["created"] for e in log} == {"2026-08-26T10:50:03.000+0000"}


async def test_transition_back_to_open_clears_the_resolution(site: Site) -> None:
    site.provider.transition(_ref(site, "LAUNCH-2"), TicketState.OPEN, site.store, site.clock)
    read = ok(await site.http.get(f"{API}/issue/LAUNCH-2", params={"fields": "status,resolution,resolutiondate"}))
    assert (read["fields"]["status"]["name"], read["fields"]["resolution"], read["fields"]["resolutiondate"]) == (
        "In Progress",
        None,
        None,
    )


def test_transition_of_an_unassigned_issue_is_refused(site: Site) -> None:
    with pytest.raises(ValueError, match="no assignee"):
        site.provider.transition(_ref(site, "FIELD-1"), TicketState.DONE, site.store, site.clock)


async def test_edit_rewrites_state_and_assignee_as_the_scenario(site: Site) -> None:
    site.provider.edit(
        _ref(site, "LAUNCH-1"),
        state=TicketState.CANCELLED,
        assignee_email="iris@example.com",
        world=site.store,
        clock=site.clock,
    )
    last = site.store.events()[-1]
    assert last.actor is Actor.SCENARIO
    assert last.after == TicketSnapshot(
        title="Write the release notes",
        body="Cover the new export.\nLink the changelog.",
        project="LAUNCH",
        assignee_email="iris@example.com",
        state=TicketState.CANCELLED,
    )


def test_edit_to_an_email_nobody_has_is_refused(site: Site) -> None:
    with pytest.raises(LookupError, match=r"nobody@example\.com"):
        site.provider.edit(
            _ref(site, "LAUNCH-1"), state=None, assignee_email="nobody@example.com", world=site.store, clock=site.clock
        )


async def test_a_person_reassigns_comments_on_and_deletes_an_issue_at_their_moment(site: Site) -> None:
    ref = _ref(site, "LAUNCH-1")
    site.clock.jump(START + timedelta(hours=5))
    site.provider.reassigns(ref, "noor@example.com", by_email="iris@example.com", world=site.store, clock=site.clock)
    site.provider.comments(ref, "Noor has it now.", by_email="iris@example.com", world=site.store, clock=site.clock)
    site.provider.moves(ref, TicketState.DONE, by_email="noor@example.com", world=site.store, clock=site.clock)

    read = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "assignee,status,comment"}))
    assert read["fields"]["assignee"]["accountId"] == NOOR and read["fields"]["status"]["name"] == "Done"
    last_comment = read["fields"]["comment"]["comments"][-1]
    assert last_comment["author"]["accountId"] == IRIS
    assert last_comment["created"] == "2026-08-24T15:50:03.000+0000"
    assert last_comment["body"]["content"][0]["content"][0]["text"] == "Noor has it now."
    log = ok(await site.http.get(f"{API}/issue/LAUNCH-1/changelog"))["values"]
    assert [(e["author"]["accountId"], e["items"][0]["field"]) for e in log[1:]] == [
        (IRIS, "assignee"),
        (NOOR, "status"),
        (NOOR, "status"),
        (NOOR, "status"),
    ]
    acts = [e for e in site.store.events() if e.actor is Actor.PERSON]
    assert {e.sim_time for e in acts} == {START + timedelta(hours=5)}

    site.provider.deletes(ref, by_email="iris@example.com", world=site.store, clock=site.clock)
    assert (await site.http.get(f"{API}/issue/LAUNCH-1")).status_code == 404
    assert site.store.events()[-1].actor is Actor.PERSON
    head = site.store.head()
    site.provider.comments(ref, "Too late.", by_email="iris@example.com", world=site.store, clock=site.clock)
    site.provider.moves(ref, TicketState.OPEN, by_email="iris@example.com", world=site.store, clock=site.clock)
    assert site.store.head() == head, "an act on an issue that is gone writes nothing"


def test_a_person_the_site_does_not_know_cannot_act(site: Site) -> None:
    with pytest.raises(LookupError, match=r"stranger@example\.com"):
        site.provider.comments(_ref(site, "LAUNCH-1"), "Hi", by_email="stranger@example.com", world=site.store,
                               clock=site.clock)  # fmt: skip


def test_the_provider_meets_its_three_ports() -> None:
    provider = build()
    held: Provider = provider
    holds: HoldsTickets = provider
    edits: EditsTickets = provider
    assert held.manifest is MANIFEST and holds is edits
    assert isinstance(provider, HoldsTickets) and isinstance(provider, EditsTickets)


def test_the_manifest_claims_jira_and_imports_nothing_else_of_the_provider() -> None:
    assert (MANIFEST.key, MANIFEST.tier, MANIFEST.path_prefix) == ("jira", Tier.FINISHED, "")
    assert MANIFEST.hosts == ["*.atlassian.net", "api.atlassian.com", "auth.atlassian.com"]
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, minutehand.adapters.providers.jira.manifest;"
            "print(sorted(m for m in sys.modules if m.startswith('minutehand.adapters.providers.jira.')))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "['minutehand.adapters.providers.jira.manifest']"


@pytest.mark.parametrize("host", ["lanternworks.atlassian.net", "Acme.Atlassian.Net", "api.atlassian.com",
                                  "auth.atlassian.com"])  # fmt: skip
def test_the_installed_registry_routes_atlassian_hosts_here(host: str) -> None:
    claimed = Registry.installed().claimant(host)
    assert claimed is not None and claimed.key == "jira"


@pytest.mark.parametrize("host", ["atlassian.net", "atlassian.com", "id.atlassian.com", "atlassian.net.evil.test"])
def test_other_hosts_are_not_claimed(host: str) -> None:
    claimed = Registry.installed().claimant(host)
    assert claimed is None or claimed.key != "jira"
