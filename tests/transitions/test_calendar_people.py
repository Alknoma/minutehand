"""People answer Calendar invitations through the one port (docs/design-transitions.md): an event a person is a
guest of and has not answered is pending on them; at their moment they set their response, through the same path
an answer lands by, and the move is recorded once as theirs. In a run, the replier is never asked about an
invitation the engine holds, and the ledger's wait on the guest settles at their move."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.providers.google_workspace import calendar_wire
from minutehand.adapters.providers.google_workspace.calendars import CalendarWorld, event_ref
from minutehand.adapters.providers.google_workspace.provider import build
from minutehand.adapters.providers.google_workspace.seed import SeededAttendee, SeededEvent, WorkspaceSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import PendingTransition, checkpoint_seqs, checkpoints
from minutehand.application.dues import due_entries
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.application.rewind import fork_run
from minutehand.application.run_clock import RunClock
from minutehand.checks.ledger import build as ledger
from minutehand.domain.agent import AgentUnderTest, Command
from minutehand.domain.checks import ObligationKind
from minutehand.domain.clock import DueClosed, DueSource
from minutehand.domain.conversation import ModelMessage, Wrote
from minutehand.domain.experiment import Fork
from minutehand.domain.scenario import AfterScript, Answers, Person, ProviderSeed, Scenario, Scripted, Silent
from minutehand.domain.world import Actor, PendingSnapshot, PendingStatus, TransitionSnapshot, WorldEvent
from minutehand.ports.model import Answered, AnswerT, ModelFailed
from minutehand.ports.store import Store
from tests.orchestrator.rig import rigged
from tests.orchestrator.world import RecordingClock
from tests.support.people import people_engine, people_model

START = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
AGENTS = Path(__file__).parent / "agents"


def _scenario(dov: Person, *, events: list[SeededEvent] | None = None) -> Scenario:
    return Scenario(
        name="planning_call",
        goal="A planning call with Dov is on the calendar.",
        owner="mara",
        starts_at=START,
        deadline_after=timedelta(days=3),
        transitions_on=["google_workspace"],
        people=[Person(key="mara", name="Mara Lind", email="mara@example.com", reply=Silent()), dov],
        provider_seeds=[
            ProviderSeed(provider="google_workspace", body=WorkspaceSeed(events=events or []).model_dump_json())
        ],
    )


SEEDED = [
    SeededEvent(
        calendar="mara",
        summary="Supplier review",
        starts=timedelta(days=1),
        lasts=timedelta(hours=1),
        attendees=[SeededAttendee(person="dov")],
    ),
    SeededEvent(
        calendar="mara",
        summary="Weekly sync",
        starts=timedelta(days=2),
        lasts=timedelta(hours=1),
        attendees=[SeededAttendee(person="dov", response="accepted")],
    ),
]


def _moves(store: Store) -> list[tuple[WorldEvent, TransitionSnapshot]]:
    return [(e, e.after) for e in store.events() if isinstance(e.after, TransitionSnapshot)]


def _dov(**more: object) -> Person:
    return Person.model_validate(
        {"key": "dov", "name": "Dov Aranha", "email": "dov@example.com", "reply": Answers(), **more}
    )


async def test_an_unanswered_invitation_is_pending_and_a_pinned_answer_lands_as_the_guests(tmp_path: Path) -> None:
    takes = [{"provider": "google_workspace", "take": "No", "after": "PT3H", "verbatim": "Away that week."}]
    scenario = _scenario(_dov(takes=[{**takes[0], "take": "declined"}]), events=SEEDED)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    google = build()
    google.seed(scenario, store)
    engine = people_engine(scenario, {"google_workspace": google}, people_model())

    [booked] = (await engine.look(store, clock)).booked
    assert booked.person == "dov" and booked.at == START + timedelta(hours=3), "the weekly sync is answered already"
    offers = google.legal(booked.item, Actor.PERSON, scenario.people[1], store)
    assert [o.name for o in offers] == ["reply", "accepted", "tentative", "declined"]
    assert not google.heard_of(booked.item, None, store, clock), "no watch: the agent finds the answer on its next read"

    clock.jump(START + timedelta(hours=3))
    acted = await engine.act(booked.pending, store, clock)

    assert acted.transition is not None
    found = CalendarWorld(store).event(booked.item.external_id)
    assert found is not None
    [guest] = [a for a in found[0].attendees or [] if a.email == "dov@example.com"]
    assert (guest.responseStatus, guest.comment) == ("declined", "Away that week.")
    moved = _moves(store)
    assert [(e.actor, t.who, t.from_state, t.to_state) for e, t in moved] == [
        (Actor.PERSON, "dov", "needsAction", "declined")
    ]
    assert (await engine.look(store, clock)).booked == [], "answered: it no longer waits on them"
    assert engine.pending(booked.pending, store).status is PendingStatus.ACTED


async def test_an_answer_the_invitation_does_not_offer_again_is_refused(tmp_path: Path) -> None:
    scenario = _scenario(_dov(), events=SEEDED)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    google = build()
    google.seed(scenario, store)
    event = next(e for e, _ in CalendarWorld(store).organized("mara@example.com") if e.summary == "Supplier review")
    dov = scenario.people[1]
    await google.apply(event_ref(event.id), "accepted", Actor.PERSON, dov, "{}", store, clock)

    assert "accepted" not in [o.name for o in google.legal(event_ref(event.id), Actor.PERSON, dov, store)]
    with pytest.raises(ValueError, match="does not offer dov 'accepted'"):
        await google.apply(event_ref(event.id), "accepted", Actor.PERSON, dov, "{}", store, clock)
    with pytest.raises(ValueError, match="only a guest answers"):
        await google.apply(event_ref(event.id), "declined", Actor.PERSON, scenario.people[0], "{}", store, clock)
    assert calendar_wire.ANSWERS["declined"] == "No"


async def test_in_a_run_the_engine_answers_the_agents_invitation_and_the_replier_is_never_asked(
    tmp_path: Path,
) -> None:
    dov = _dov(facts=["I have declined: I am away that week."])
    scenario = _scenario(dov)
    google = build()
    async with rigged(tmp_path) as rig:
        agent = AgentUnderTest(name="inviter", wakes=[Command(argv=[sys.executable, str(AGENTS / "inviter.py")])])
        clock = RecordingClock(START)
        store = SqliteStore(tmp_path / "run.db", "run", clock)
        record = await run_scenario(
            scenario=scenario,
            agent=agent,
            reach=reach_for(agent, env=rig.env()),
            store=store,
            clock=clock,
            services=Services(providers=[google]),
            replier=PeopleReplier(scenario, people_model()),
            mounts=rig.mounts,
            model=people_model(),
        )

    moved = _moves(store)
    assert [(t.who, t.to_state) for _, t in moved] == [("dov", "declined")]
    assert json.loads(moved[0][1].content) == {"text": "I have declined: I am away that week."}
    assert [r.press.action_id for r in store.replies() if r.press is not None] == ["declined"], "kept as said"
    entries = [d for d in due_entries(store) if d.source is DueSource.REPLY]
    assert [(d.closed, d.due.at) for d in entries] == [(DueClosed.FIRED, moved[0][0].sim_time)]
    assert moved[0][0].sim_time in clock.jumps
    [wait] = [o for o in ledger(scenario, store.events(), []) if o.kind is ObligationKind.ANSWER_FROM_PERSON]
    assert (wait.person, wait.settled_at) == ("dov", moved[0][0].sim_time), "the invitation asked; the move answered"
    assert len(record.wakes) == 1, "an answer nobody watches wakes nobody"


def test_a_script_that_goes_silent_with_nothing_pinned_never_answers() -> None:
    quiet = _dov(reply=Scripted(then=AfterScript.SILENT))
    from minutehand.application.people import needs_model

    assert needs_model(_scenario(quiet)) == []


class FailsFirst:
    """The people's model, failing its first call as a service that is down for a moment does."""

    def __init__(self) -> None:
        self._model = people_model()
        self.model_id = self._model.model_id
        self.failed = False

    async def answer(
        self,
        system: str,
        messages: Sequence[ModelMessage],
        answer: type[AnswerT],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Answered[AnswerT]:
        if not self.failed:
            self.failed = True
            raise ModelFailed("503 from the model service")
        return await self._model.answer(system, messages, answer, model=model, temperature=temperature)


async def test_a_move_whose_words_failed_is_owed_still_and_taken_on_the_runs_next_turn(tmp_path: Path) -> None:
    dov = _dov(facts=["I have declined: I am away that week."])
    scenario = _scenario(dov)
    google = build()
    model = FailsFirst()
    async with rigged(tmp_path) as rig:
        agent = AgentUnderTest(name="inviter", wakes=[Command(argv=[sys.executable, str(AGENTS / "inviter.py")])])
        clock = RecordingClock(START)
        store = SqliteStore(tmp_path / "run.db", "run", clock)
        await run_scenario(
            scenario=scenario,
            agent=agent,
            reach=reach_for(agent, env=rig.env()),
            store=store,
            clock=clock,
            services=Services(providers=[google]),
            replier=PeopleReplier(scenario, people_model()),
            mounts=rig.mounts,
            model=model,
        )

    calls = [c for c in store.person_calls() if c.wrote is Wrote.TRANSITION]
    assert [c.failure is not None for c in calls] == [True, False]
    assert [(t.who, t.to_state) for _, t in _moves(store)] == [("dov", "declined")]
    pending = [e.after for e in store.events() if isinstance(e.after, PendingSnapshot)]
    assert [p.failure for p in pending if p.failure] == ["503 from the model service"]


async def test_a_fork_keeps_a_move_booked_before_its_checkpoint_whatever_its_seed(tmp_path: Path) -> None:
    dov = _dov(facts=["I have declined: I am away that week."])
    scenario = _scenario(dov)
    google = build()
    async with rigged(tmp_path) as rig:
        agent = AgentUnderTest(name="inviter", wakes=[Command(argv=[sys.executable, str(AGENTS / "inviter.py")])])
        clock = RecordingClock(START)
        parent_store = rig.open("root", clock)
        parent = await run_scenario(
            scenario=scenario,
            agent=agent,
            reach=reach_for(agent, env=rig.env()),
            store=parent_store,
            clock=clock,
            services=Services(providers=[google]),
            replier=PeopleReplier(scenario, people_model()),
            mounts=rig.mounts,
            model=people_model(),
        )
        after_start = checkpoint_seqs(parent_store)[1]
        assert any(isinstance(p, PendingTransition) for p in checkpoints(parent_store)[after_start].pending)

        [child] = await fork_run(
            fork=Fork(parent_run="root", at_seq=after_start, seed=scenario.seed + 1),
            parent=parent,
            open_parent=lambda c: rig.open("root", c),
            run_id="child",
            scenario=scenario,
            agent=agent,
            reach=reach_for(agent, env=rig.env()),
            services=Services(providers=[google]),
            replier_for=lambda s, pins: PeopleReplier(s, people_model(), pins=pins),
            model=people_model(),
            state_dir=rig.tmp / "state",
            mounts=rig.mounts,
        )

    def moves(run_id: str) -> list[tuple[str | None, str, datetime]]:
        return [(t.who, t.to_state, e.sim_time) for e, t in _moves(rig.open(run_id, RecordingClock(START)))]

    assert child.parent_run == "root"
    assert moves("child") == moves("root") != [], "booked before the checkpoint: the fork keeps the parent's moment"
