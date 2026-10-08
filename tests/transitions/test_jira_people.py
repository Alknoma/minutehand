"""People act on Jira issues through the one port (docs/design-transitions.md): an issue assigned to a person and
not done is pending on them; at their moment they take one of its workflow transitions, pinned or a model's pick,
through the same path as `POST /issue/{key}/transitions`, and the move is recorded once as theirs."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from minutehand.adapters.providers.jira.provider import build
from minutehand.adapters.providers.jira.state import JiraWorld, issue_ref
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.orchestrator import refuse_fates_beside_the_engine
from minutehand.application.people import TRANSITION_PROMPT_VERSION, People
from minutehand.application.refusals import RunRefused
from minutehand.application.run_clock import RunClock
from minutehand.domain.conversation import Wrote
from minutehand.domain.scenario import Scenario, Silent
from minutehand.domain.world import Actor, EntityKind, EntityRef, PendingSnapshot, PendingStatus, TransitionSnapshot
from tests.providers.jira.jira_site import API, NOOR, SCENARIO, START, TOMAS, Site, ok
from tests.support.people import people_model


def _scenario(**people: dict[str, Any]) -> Scenario:
    """The site's scenario with the engine playing Jira, each named person changed as given."""
    body = SCENARIO.model_dump(mode="json")
    body["transitions_on"] = ["jira"]
    for person in body["people"]:
        person.update(people.get(person["key"], {}))
    return Scenario.model_validate(body)


def _engine(site: Site, scenario: Scenario) -> People:
    return People(scenario, lambda _: site.provider, people_model())


def _ref(site: Site, key: str) -> EntityRef:
    issue = site.jira.find_issue(key)
    assert issue is not None
    return issue_ref(issue.id)


def _moves(site: Site) -> list[Any]:
    return [e for e in site.store.events() if isinstance(e.after, TransitionSnapshot)]


def _pending(site: Site, ref: EntityRef) -> PendingSnapshot:
    stored = site.store.get(ref)
    assert stored is not None
    return PendingSnapshot.model_validate_json(stored.body)


SILENT = {"reply": Silent().model_dump(mode="json")}


def test_an_issue_assigned_to_a_person_and_not_done_is_pending_on_them(site: Site) -> None:
    engine = _engine(site, _scenario(noor=SILENT))
    looked = engine.look(site.store, site.clock)

    assert sorted((b.person, b.item) for b in looked.booked) == sorted(
        [("tomas", _ref(site, "LAUNCH-1")), ("iris", _ref(site, "VAULT-1"))]
    ), "noor's venue is done: nothing waits on her"
    held = engine.held("tomas", site.store)
    assert [h.pending.state for h in held] == ["To Do"]
    assert held[0].pending.status is PendingStatus.PENDING and held[0].pending.due_at is not None
    assert engine.look(site.store, site.clock).booked == [], "an item already held is not booked twice"


async def test_a_pinned_take_moves_the_issue_with_its_words_as_the_person(site: Site) -> None:
    takes = [{"provider": "jira", "take": "Start work", "after": "PT2H", "verbatim": "On it from today."}]
    engine = _engine(site, _scenario(tomas={"takes": takes}, iris=SILENT))
    booked = next(b for b in engine.look(site.store, site.clock).booked if b.person == "tomas")
    assert booked.at == START + timedelta(hours=2)

    site.clock.jump(START + timedelta(hours=2))
    acted = await engine.act(booked.pending, site.store, site.clock)

    assert acted.transition is not None and acted.transition.name == "Start work"
    read = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "status,comment"}))
    assert read["fields"]["status"]["name"] == "In Progress"
    comment = read["fields"]["comment"]["comments"][-1]
    assert comment["author"]["accountId"] == TOMAS
    assert comment["body"]["content"][0]["content"][0]["text"] == "On it from today."
    log = ok(await site.http.get(f"{API}/issue/LAUNCH-1/changelog"))["values"]
    assert log[-1]["author"]["accountId"] == TOMAS
    moved = _moves(site)[-1]
    assert moved.actor is Actor.PERSON and moved.sim_time == START + timedelta(hours=2)
    assert isinstance(moved.after, TransitionSnapshot)
    assert (moved.after.who, moved.after.from_state, moved.after.to_state) == ("tomas", "To Do", "In Progress")
    assert json.loads(moved.after.content) == {"comment": "On it from today."}
    held = _pending(site, booked.pending)
    assert held.status is PendingStatus.ACTED and held.transition == moved.seq


async def test_a_person_acts_once_per_turn_until_someone_else_moves_it(site: Site) -> None:
    takes = [{"provider": "jira", "take": "Start work", "after": "PT1H"}]
    engine = _engine(site, _scenario(tomas={"takes": takes}, iris=SILENT))
    booked = next(b for b in engine.look(site.store, site.clock).booked if b.person == "tomas")
    site.clock.jump(START + timedelta(hours=1))
    await engine.act(booked.pending, site.store, site.clock)
    assert not [b for b in engine.look(site.store, site.clock).booked if b.person == "tomas"], (
        "still assigned and not done, but it was their own move: nothing new waits on them"
    )

    ok(await site.http.post(f"{API}/issue/LAUNCH-1/transitions", json={"transition": {"id": "21"}}), 204)
    agent_move = _moves(site)[-1]
    assert agent_move.actor is Actor.AGENT and agent_move.after.who is None

    again = [b for b in engine.look(site.store, site.clock).booked if b.person == "tomas"]
    assert [b.item for b in again] == [_ref(site, "LAUNCH-1")], "the agent moved it: it waits on them again"
    assert _pending(site, again[0].pending).turn == agent_move.seq
    assert _pending(site, again[0].pending).state == "In Review"


async def test_a_model_picks_the_transition_and_writes_its_comment_from_what_they_know(site: Site) -> None:
    facts = {"facts": ["I will drop this: the export feature was cut from the launch."]}
    engine = _engine(site, _scenario(tomas=facts, iris=SILENT))
    booked = next(b for b in engine.look(site.store, site.clock).booked if b.person == "tomas")
    assert booked.at is not None
    site.clock.jump(booked.at)

    acted = await engine.act(booked.pending, site.store, site.clock)

    assert acted.transition is not None and acted.transition.name == "Drop"
    assert json.loads(acted.transition.content) == {
        "comment": "I will drop this: the export feature was cut from the launch."
    }
    read = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "status"}))
    assert read["fields"]["status"]["name"] == "Won't Do"
    calls = [c for c in site.store.person_calls() if c.wrote is Wrote.TRANSITION]
    assert [(c.person, c.prompt_version, c.asked) for c in calls] == [
        ("tomas", TRANSITION_PROMPT_VERSION, _ref(site, "LAUNCH-1"))
    ]


async def test_an_issue_reassigned_before_their_moment_is_gone_and_never_moved(site: Site) -> None:
    engine = _engine(site, _scenario(iris=SILENT))
    booked = next(b for b in engine.look(site.store, site.clock).booked if b.person == "tomas")

    ok(await site.http.put(f"{API}/issue/LAUNCH-1/assignee", json={"accountId": NOOR}), 204)
    looked = engine.look(site.store, site.clock)

    assert booked.pending in looked.gone
    assert _pending(site, booked.pending).status is PendingStatus.GONE
    head = site.store.head()
    assert (await engine.act(booked.pending, site.store, site.clock)).transition is None
    assert site.store.head() == head, "a gone item is never acted on"
    assert [b.person for b in looked.booked if b.item == _ref(site, "LAUNCH-1")] == ["noor"], "now it waits on noor"


def test_a_silent_person_holds_the_item_and_never_acts(site: Site) -> None:
    engine = _engine(site, _scenario(iris=SILENT))
    looked = engine.look(site.store, site.clock)
    iris = next(b for b in looked.booked if b.person == "iris")
    assert iris.at is None and _pending(site, iris.pending).due_at is None


async def test_a_pinned_take_the_issue_does_not_offer_is_dropped_saying_so_and_refused(site: Site) -> None:
    takes = [{"provider": "jira", "take": "Approve", "after": "PT1H"}]
    engine = _engine(site, _scenario(tomas={"takes": takes}, iris=SILENT))
    booked = next(b for b in engine.look(site.store, site.clock).booked if b.person == "tomas")
    site.clock.jump(START + timedelta(hours=1))

    assert (await engine.act(booked.pending, site.store, site.clock)).transition is None

    held = _pending(site, booked.pending)
    assert held.status is PendingStatus.GONE
    assert held.failure is not None and "'Approve', which is not offered" in held.failure
    read = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "status"}))
    assert read["fields"]["status"]["name"] == "To Do"


async def test_an_offer_no_longer_legal_is_refused_as_jira_refuses_it(site: Site) -> None:
    tomas = next(p for p in SCENARIO.people if p.key == "tomas")
    offers = site.provider.legal(_ref(site, "LAUNCH-1"), Actor.PERSON, tomas, site.store)
    assert [(o.name, o.to_state) for o in offers] == [("Start work", "In Progress"), ("Drop", "Won't Do")]
    assert [f.name for f in offers[0].fields] == ["comment"]
    with pytest.raises(ValueError, match="offers no transition 'Submit for review' from To Do"):
        await site.provider.apply(
            _ref(site, "LAUNCH-1"), "Submit for review", Actor.PERSON, tomas, "{}", site.store, site.clock
        )


async def test_the_agents_own_create_and_transition_are_recorded_as_the_agents(site: Site) -> None:
    created = ok(
        await site.http.post(
            f"{API}/issue",
            json={"fields": {"project": {"key": "LAUNCH"}, "issuetype": {"name": "Task"}, "summary": "Print badges"}},
        ),
        201,
    )
    ok(await site.http.post(f"{API}/issue/{created['key']}/transitions", json={"transition": {"id": "11"}}), 204)

    moves = [m.after for m in _moves(site) if isinstance(m.after, TransitionSnapshot)]
    assert [(m.name, m.from_state, m.to_state) for m in moves] == [
        ("Create", None, "To Do"),
        ("Start work", "To Do", "In Progress"),
    ]
    assert {e.actor for e in _moves(site)} == {Actor.AGENT}
    kinds = {e.entity.kind for e in site.store.events() if e.actor is Actor.AGENT}
    assert EntityKind.TRANSITION in kinds and EntityKind.TICKET in kinds, "recorded beside the issue's own versions"


def test_a_person_a_model_moves_with_no_model_configured_is_refused(site: Site) -> None:
    with pytest.raises(RunRefused, match=r"a model writes what .*tomas \(a model picks what they do on jira\)"):
        People(_scenario(), lambda _: site.provider, None)


def test_a_take_on_a_provider_the_engine_does_not_play_is_refused() -> None:
    body = SCENARIO.model_dump(mode="json")
    body["people"][1]["takes"] = [{"provider": "jira", "take": "Done"}]
    with pytest.raises(ValueError, match="which the people engine does not play: add it to `transitions_on`"):
        Scenario.model_validate(body)


async def test_a_transition_whose_screen_requires_a_field_is_not_offered_to_a_person(tmp_path: Path) -> None:
    seed = json.loads(SCENARIO.provider_seeds[0].body)
    approve = next(t for t in seed["projects"][0]["transitions"] if t["name"] == "Approve")
    approve["required"] = ["resolution"]
    scenario = SCENARIO.model_copy(
        update={"provider_seeds": [SCENARIO.provider_seeds[0].model_copy(update={"body": json.dumps(seed)})]}
    )
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = build()
    provider.seed(scenario, store)
    issue = JiraWorld(store).find_issue("LAUNCH-1")
    assert issue is not None
    tomas = next(p for p in SCENARIO.people if p.key == "tomas")
    for name in ("Start work", "Submit for review"):
        await provider.apply(issue_ref(issue.id), name, Actor.PERSON, tomas, "{}", store, clock)

    offers = provider.legal(issue_ref(issue.id), Actor.PERSON, tomas, store)

    assert [o.name for o in offers] == ["Back to work", "Drop"], "Approve needs a resolution a person cannot type"


def test_a_ticket_fate_beside_the_engine_on_a_ticket_provider_is_refused(site: Site) -> None:
    body = _scenario().model_dump(mode="json")
    body["ticket_fates"] = [{"assignee": "tomas", "becomes": "done", "after": "P1D"}]
    with pytest.raises(RunRefused, match="ticket fates and the people engine plays jira"):
        refuse_fates_beside_the_engine(Scenario.model_validate(body), [site.provider.manifest])
    refuse_fates_beside_the_engine(_scenario(), [site.provider.manifest])
