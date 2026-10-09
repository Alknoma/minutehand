"""People act on YouTrack issues and Asana tasks through the one port (`docs/design-transitions.md`, phase 3): an item
assigned to a person and still open is pending on them; at their moment they move it as its own field or section
reads, with their comment, as themselves, and the move is recorded once as theirs."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.asana.provider import build as asana
from minutehand.adapters.providers.asana.state import AsanaWorld
from minutehand.adapters.providers.youtrack.provider import build as youtrack
from minutehand.adapters.providers.youtrack.state import YouTrackWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Scenario, Take, TicketState
from minutehand.domain.world import Actor, PendingStatus, TransitionSnapshot
from minutehand.ports.store import Store
from tests.providers.asana.asana_workspace import SCENARIO as ASANA
from tests.providers.asana.asana_workspace import START as ASANA_START
from tests.providers.youtrack.youtrack_instance import SCENARIO as YOUTRACK
from tests.providers.youtrack.youtrack_instance import START as YOUTRACK_START
from tests.support.people import people_engine, people_model


def _played(scenario: Scenario, provider: str, take: str) -> Scenario:
    """The scenario with the engine playing `provider`, and tomas's first move there pinned to `take`."""
    pinned = Take(provider=provider, take=take, after=timedelta(hours=2), verbatim="Handled it this morning.")
    people = [p.model_copy(update={"takes": [pinned]}) if p.key == "tomas" else p for p in scenario.people]
    return scenario.model_copy(update={"transitions_on": [provider], "people": people})


def _moves(store: Store) -> list[TransitionSnapshot]:
    return [e.after for e in store.events() if isinstance(e.after, TransitionSnapshot)]


async def test_a_youtrack_issue_assigned_and_unresolved_is_pending_and_a_pinned_state_lands_as_theirs(
    tmp_path: Path,
) -> None:
    scenario = _played(YOUTRACK, "youtrack", "Fixed")
    clock = RunClock(YOUTRACK_START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = youtrack()
    provider.seed(scenario, store)
    engine = people_engine(scenario, {"youtrack": provider}, people_model())

    # Noor's issue is resolved already: nothing waits on her. Mutation: dropping `isResolved` books her too.
    [booked] = (await engine.look(store, clock)).booked
    assert booked.person == "tomas" and booked.at == YOUTRACK_START + timedelta(hours=2)
    tomas = scenario.people[1]
    assert [o.name for o in provider.legal(booked.item, Actor.PERSON, tomas, store) if o.unprompted] == [
        "In Progress",
        "Fixed",
        "Won't fix",
    ], "every State but the one it is in"

    clock.jump(YOUTRACK_START + timedelta(hours=2))
    acted = await engine.act(booked.pending, store, clock)

    assert acted.transition is not None
    [moved] = _moves(store)
    assert (moved.name, moved.from_state, moved.to_state, moved.who) == ("Fixed", "Open", "Fixed", "tomas")
    assert json.loads(moved.content) == {"comment": "Handled it this morning."}
    world = YouTrackWorld(store)
    issue = next(i for i in world.every_issue() if i.summary == "Write the release notes")
    project = world.project(issue.project)
    assert project is not None
    state = world.state_of(project, issue)
    assert state is not None and state.name == "Fixed"
    assert [c.text for c in world.comments(issue.id)] == ["Handled it this morning."]
    assert engine.pending(booked.pending, store).status is PendingStatus.ACTED
    assert (await engine.look(store, clock)).booked == [], "resolved: it no longer waits on them"


async def test_a_youtrack_state_the_issue_is_in_already_is_refused(tmp_path: Path) -> None:
    clock = RunClock(YOUTRACK_START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = youtrack()
    provider.seed(YOUTRACK, store)
    [waiting] = provider.items_for(YOUTRACK.people[1], store)

    # Mutation: dropping the check on the current State records a move from Open to Open.
    with pytest.raises(ValueError, match="offers no State 'Open'"):
        await provider.apply(waiting.item, "Open", Actor.PERSON, YOUTRACK.people[1], "{}", store, clock)
    assert _moves(store) == []


async def test_an_asana_task_assigned_and_open_is_pending_and_a_pinned_move_lands_as_theirs(tmp_path: Path) -> None:
    scenario = _played(ASANA, "asana", "Done (completed)")
    clock = RunClock(ASANA_START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = asana()
    provider.seed(scenario, store)
    engine = people_engine(scenario, {"asana": provider}, people_model())

    # Noor's task is done already. Mutation: dropping the open-state filter books her too.
    [booked] = (await engine.look(store, clock)).booked
    assert booked.person == "tomas"
    tomas = scenario.people[1]
    assert [o.name for o in provider.legal(booked.item, Actor.PERSON, tomas, store) if o.unprompted] == [
        "Done (completed)",
        "Cancelled (completed)",
    ]

    clock.jump(ASANA_START + timedelta(hours=2))
    acted = await engine.act(booked.pending, store, clock)

    assert acted.transition is not None
    [moved] = _moves(store)
    assert (moved.name, moved.from_state, moved.to_state, moved.who) == (
        "Done (completed)",
        "To do",
        "Done (completed)",
        "tomas",
    )
    world = AsanaWorld(store)
    task = next(t for t in world.tasks() if t.name == "Book the freight lift")
    assert world.state_of(task) is TicketState.DONE
    assert (await engine.look(store, clock)).booked == [], "done: it no longer waits on them"
