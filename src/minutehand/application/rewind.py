"""A fork: a finished run restarted from one of its checkpoints, with the scenario changed.

The child shares the parent's log up to the checkpoint (`Store.fork`), so the world, the clock and the
pending set come back by reading the log. Replies the parent's people had already decided are copied, not
asked for again, except for a person a `PersonChange` changes: each message to them not answered by the fork
(no reply decided, or one decided that had not landed yet) is put to them again under their new behaviour.

The agent's memory (`minutehand.agent.store`) is part of the same log, so the child has its parent's memory as it
stood at the checkpoint with nothing restored. `application.restore.start_fork` proves it (the memory's digest against
the checkpoint's), starts the agent's program when Minutehand runs it, and compares the agent's report with the one
recorded at the checkpoint: a report that differs means state outside the store, and the fork is refused, saying so.
A checkpoint the agent went on writing its memory after, in the same wake, is not one a fork can start from. Every
refusal that can be decided from the parent is decided before the child run exists, and a child refused after it
exists is discarded, so no refusal leaves a run behind.

What no fork can rewind, because it was never in the log: whatever the agent keeps outside the store (its own
database, files, a cache, a process's memory), what a real third-party service the run reached keeps (the proxy
refuses unclaimed hosts, but a model API is reached for real), what a model provider keeps on its side (a stored
conversation, a cache, a batch), and the AWS provider's queues and schedules (in moto's memory). A provider that says
it keeps state outside the log (`Manifest.state_outside_log`, AWS) refuses any fork after the parent first used it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from minutehand.application import memory
from minutehand.application.checkpoint import (
    Checkpoint,
    NotRestorable,
    Pending,
    PendingBooking,
    PendingReply,
    Unwritten,
    checkpoints,
)
from minutehand.application.inboxes import Inboxes, items_in
from minutehand.application.memory import store_digest
from minutehand.application.moments import asks_of, pinned
from minutehand.application.orchestrator import Environment, Mounts, Orchestrator, OutsideState, Reach, Scorer, Services
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import OwnProgram, Progress, Restored, start_fork
from minutehand.application.run_clock import RunClock
from minutehand.application.traffic import Traffic
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.experiment import (
    DeadlineShift,
    DispatchChange,
    Fork,
    MemoryEdit,
    ModelSwap,
    PersonChange,
    PromptPatch,
    ReplyAt,
    TicketEdit,
)
from minutehand.domain.provider import Manifest
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.world import (
    Actor,
    EntityKind,
    InboxItemSnapshot,
    ItemStatus,
    MemorySnapshot,
    MessageSnapshot,
    Operation,
    RecordedCall,
)
from minutehand.ports.agent import Reports, TakesReplies
from minutehand.ports.clock import Clock
from minutehand.ports.model import ModelFailed
from minutehand.ports.people import Replier
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

WireOverride = PromptPatch | ModelSwap

RESTORE_RECORD = "restore.json"
"""`Restored`, in the directory of the run the restore started."""

FORK_RECORD = "fork.json"
"""`Fork`, in the directory of each run it started: what the child changed, as it was asked for."""

CANNOT_REWIND = (
    "A fork rewinds the fakes' world and the agent's memory (`minutehand.agent.store`); it cannot rewind what the "
    "agent keeps anywhere else, what a real third-party service the run reached keeps, what a model provider keeps on "
    "its side, the AWS provider's queues and schedules, or what an external emulator holds."
)


class OnTheWire(Protocol):
    """Whoever edits the agent's model calls in flight (the proxy). `fork_run` hands these over untouched."""

    def apply(self, run_id: str, overrides: list[WireOverride]) -> None: ...


def changed_scenario(scenario: Scenario, fork: Fork) -> Scenario:
    """The scenario the child runs: people, deadline and dispatch rules as the fork's overrides say."""
    people = {p.key: p for p in scenario.people}
    deadline_after = scenario.deadline_after
    dispatch = scenario.dispatch
    for override in fork.overrides:
        if isinstance(override, ReplyAt) and override.person not in people:
            raise RunRefused(f"the fork pins a reply of {override.person}, who is not in scenario {scenario.name}")
        if isinstance(override, PersonChange):
            if override.person not in people:
                raise RunRefused(f"the fork changes {override.person}, who is not in scenario {scenario.name}")
            people[override.person] = people[override.person].model_copy(update={"reply": override.reply})
        elif isinstance(override, DeadlineShift):
            if deadline_after is None:
                raise RunRefused(f"the fork shifts the deadline of scenario {scenario.name}, which has none")
            deadline_after += override.by
        elif isinstance(override, DispatchChange):
            dispatch = override.rules
    try:
        return Scenario.model_validate(
            {
                **scenario.model_dump(),
                "people": [p.model_dump() for p in people.values()],
                "deadline_after": deadline_after,
                "dispatch": [r.model_dump() for r in dispatch],
                "seed": fork.seed if fork.seed is not None else scenario.seed,
            }
        )
    except ValidationError as e:
        problems = "; ".join(str(error["msg"]) for error in e.errors())
        raise RunRefused(f"the fork's changes leave scenario {scenario.name} self-contradictory: {problems}") from e


async def fork_run(
    *,
    fork: Fork,
    parent: RunRecord,
    open_parent: Callable[[Clock], Store],
    run_id: str,
    scenario: Scenario,
    agent: AgentUnderTest,
    reach: Reach,
    services: Services,
    replier_for: Callable[[Scenario], Replier],
    state_dir: Path,
    wire: OnTheWire | None = None,
    telemetry: Telemetry | None = None,
    mounts: Mounts | None = None,
    traffic: Traffic | None = None,
    scorer: Scorer | None = None,
    signing: Mapping[ProviderKey, str] | None = None,
    own: OwnProgram | None = None,
    progress: Progress | None = None,
    channels: Mapping[ProviderKey, TakesReplies] | None = None,
    manifests: Sequence[Manifest] = (),
    environment: Environment | None = None,
    inboxes: Inboxes | None = None,
    outside: OutsideState | None = None,
) -> list[RunRecord]:
    """Run the fork once per `Fork.samples`, each a child of `parent` named `run_id` (suffixed when sampled).

    `open_parent` opens the parent run's store stamping from the clock it is given; the child the store's
    `fork` makes stamps from that same clock, which this function moves to the checkpoint. `own` is the agent's
    program when Minutehand runs it, not yet started: it is started once the child exists, so it reads the child's
    memory from its first call. `progress` hears each step as it is taken. How each child's agent was found at its
    start is kept as `restore.json` in its directory.

    `manifests` are those of every installed provider, beside the run's own `services`: a provider the agent called
    without the scenario or agent file naming it is still one whose state may be outside the log.
    """
    if fork.parent_run != parent.run_id:
        raise RunRefused(f"the fork names parent {fork.parent_run}; the record given is {parent.run_id}")
    on_wire = [o for o in fork.overrides if isinstance(o, (PromptPatch, ModelSwap))]
    if on_wire and wire is None:
        raise RunRefused(
            f"the fork patches the agent's model calls ({', '.join(o.kind for o in on_wire)}) "
            "and nothing is on the wire to apply them"
        )
    changed = changed_scenario(scenario, fork)
    _refuse_ticket_edits(fork, services, changed)
    main = reach.main
    reports = main if isinstance(main, Reports) else None
    records: list[RunRecord] = []
    for sample in range(fork.samples):
        child_id = run_id if fork.samples == 1 else f"{run_id}-{sample + 1}"
        clock = RunClock(scenario.starts_at)
        parent_store = open_parent(clock)
        checkpoint = _checkpoint_at(parent_store, parent.run_id, fork.at_seq)
        unsettled = not_restorable(parent_store, fork.at_seq, checkpoint)
        if unsettled is not None:
            raise RunRefused(
                f"the checkpoint at seq {fork.at_seq} of run {parent.run_id} is not restorable: {unsettled.reason}. "
                "Fork from a checkpoint `minutehand findings` lists as restorable. " + CANNOT_REWIND
            )
        _refuse_pending_bookings(checkpoint, parent.run_id, fork.at_seq)
        _refuse_state_outside_log(
            parent_store, [*manifests, *(p.manifest for p in services.providers)], checkpoint, fork.at_seq
        )
        _refuse_emulated(parent_store, checkpoint, fork.at_seq)
        child = parent_store.fork(child_id, at_seq=fork.at_seq, clock=clock)
        try:
            clock.jump(checkpoint.now)
            while clock.wake() < checkpoint.wake:
                clock.begin_wake()
            replier = replier_for(changed)
            orchestrator = Orchestrator(
                scenario=changed,
                agent=agent,
                reach=reach,
                store=child,
                clock=clock,
                services=services,
                replier=replier,
                telemetry=telemetry,
                mounts=mounts,
                scorer=scorer,
                signing=signing,
                parent_run=parent.run_id,
                forked_at=fork.at_seq,
                prior_wakes=[w for w in parent.wakes if w.index <= checkpoint.wake],
                traffic=traffic,
                channels=channels,
                environment=environment,
                inboxes=Inboxes(changed, list(inboxes.reaches.values())) if inboxes is not None else None,
                outside=outside,
            )
            orchestrator.mount()
            edits = [o for o in fork.overrides if isinstance(o, MemoryEdit)]

            def edit(child: Store = child, edits: list[MemoryEdit] = edits) -> None:
                for one in edits:
                    memory.edit(child, one.put, [(k.collection, k.key) for k in one.delete])

            restored = await start_fork(
                checkpoint.agent,
                memory=store_digest(child),
                checkpoint_seq=fork.at_seq,
                reports=reports,
                own=own,
                progress=progress,
                edit=edit if edits else None,
            )
            for reply in parent_store.replies()[: checkpoint.replies]:
                child.remember(reply)
            changed_people = {o.person for o in fork.overrides if isinstance(o, PersonChange)}
            if changed_people:
                checkpoint = await _ask_again(child, changed, changed_people, checkpoint, replier, clock)
            pins = [o for o in fork.overrides if isinstance(o, ReplyAt)]
            if pins:
                checkpoint = _pin_owed(child, changed, pins, checkpoint, clock)
            _edit_tickets(fork, services, changed, child, clock)
        except RunRefused:
            child.discard()
            raise
        _keep(state_dir / child_id, restored, fork)
        if wire is not None and on_wire:
            wire.apply(child_id, on_wire)
        records.append(await orchestrator.resume(checkpoint, replan=restored.replanned))
    return records


def _checkpoint_at(store: Store, parent: str, at_seq: int) -> Checkpoint:
    found = checkpoints(store)
    if at_seq not in found:
        raise RunRefused(
            f"run {parent} has no checkpoint at seq {at_seq}; a fork is taken where a wake ended: {list(found)}"
        )
    return found[at_seq]


def not_restorable(store: Store, at_seq: int, checkpoint: Checkpoint) -> NotRestorable | None:
    """Why a fork cannot start at the checkpoint at `at_seq`, or None when it can. The agent's memory there is its
    log up to `at_seq`; one the agent went on writing in the same wake, after it said it was no longer working, was
    caught half written, and a fork from it would start from memory the agent never finished."""
    late = [
        e
        for e in store.events(since=at_seq)
        if e.wake == checkpoint.wake
        and e.actor is Actor.AGENT
        and e.entity.kind is EntityKind.MEMORY
        and e.operation not in (Operation.READ, Operation.SEARCH)
    ]
    if not late:
        return None
    first = late[0].after
    key = f"{first.collection}/{first.key}" if isinstance(first, MemorySnapshot) else late[0].entity.external_id
    return NotRestorable(
        reason=f"the agent went on writing its memory after this checkpoint, in the same wake ({len(late)} write(s), "
        f"the first to {key} at seq {late[0].seq}): its memory here was half written. Report WORKING until the "
        "wake's writes are done"
    )


def _refuse_ticket_edits(fork: Fork, services: Services, scenario: Scenario) -> None:
    """Every `TicketEdit` must land: on a provider that edits tickets, to a person in the scenario."""
    for override in fork.overrides:
        if not isinstance(override, TicketEdit):
            continue
        if override.entity.provider not in services.editors:
            raise RunRefused(f"the fork edits a ticket on {override.entity.provider}, which cannot edit tickets")
        if override.assignee is not None and override.assignee not in {p.key for p in scenario.people}:
            raise RunRefused(f"the fork assigns a ticket to {override.assignee}, who is not in the scenario")


def _edit_tickets(fork: Fork, services: Services, scenario: Scenario, child: Store, clock: Clock) -> None:
    emails = {p.key: p.email for p in scenario.people}
    for override in fork.overrides:
        if isinstance(override, TicketEdit):
            assignee = emails[override.assignee] if override.assignee is not None else None
            services.editors[override.entity.provider].edit(
                override.entity, state=override.state, assignee_email=assignee, world=child, clock=clock
            )


def _keep(directory: Path, restored: Restored, fork: Fork) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RESTORE_RECORD).write_text(restored.model_dump_json(indent=2), encoding="utf-8")
    (directory / FORK_RECORD).write_text(fork.model_dump_json(indent=2), encoding="utf-8")


def _refuse_pending_bookings(checkpoint: Checkpoint, parent: str, at_seq: int) -> None:
    """A booking pending at the fork could not be delivered by the child: a scheduler keeps the schedule's target
    (AWS: the SQS queue, in moto's memory of the process that played the parent, under the parent's account) out
    of the run's log, so the child has nowhere to deliver it. Refused before the agent is restored or woken."""
    pending = [p for p in checkpoint.pending if isinstance(p, PendingBooking)]
    if pending:
        named = ", ".join(f"{p.provider} {p.ref}" for p in pending)
        raise RunRefused(
            f"run {parent} has {len(pending)} booked wake(s) pending at seq {at_seq} ({named}); a scheduler keeps "
            "what a booking delivers to outside the run's log, so a fork could not deliver it. Fork from a "
            "checkpoint with no booking pending"
        )


def _refuse_state_outside_log(store: Store, manifests: Sequence[Manifest], checkpoint: Checkpoint, at_seq: int) -> None:
    """A provider whose state lives outside the log (`Manifest.state_outside_log`) that the parent used before the
    fork would answer the child from nothing: a fresh account, its queues gone, while the restored agent holds
    their names. That is a rerun silently wrong, so it is refused, naming what was used. Used means a call it
    answered that the child would share, by the rule `Store.fork` applies, or a change it wrote at or before the
    fork's seq (a call that changed nothing it logs still built state, e.g. a queue created)."""
    outside = {m.key: m.state_outside_log for m in manifests if m.state_outside_log is not None}
    used: dict[ProviderKey, list[str]] = {}
    for call in store.calls():
        if call.provider in outside and call.first_seq - 1 <= at_seq and call.wake <= checkpoint.wake:
            used.setdefault(call.provider, []).append(
                f"{call.exchange.method} {call.exchange.host}{call.exchange.path.split('?')[0]}"
            )
    for event in store.events():
        key = event.entity.provider
        if key in outside and event.seq <= at_seq:
            used.setdefault(key, [])
    if used:
        named = "; ".join(
            f"{key} ({f'{len(calls)} call(s) before it, the first {calls[0]}' if calls else 'its changes in the log'})"
            f", which keeps {outside[key]}"
            for key, calls in sorted(used.items())
        )
        raise RunRefused(
            f"the fork at seq {at_seq} of run {store.run_id} cannot rewind what the run had built up in {named}: "
            "the child would be answered from none of it. Fork from a checkpoint before the agent first used it"
        )


def _refuse_emulated(store: Store, checkpoint: Checkpoint, at_seq: int) -> None:
    """An external emulator keeps its state outside the run's record, where no fork can put it back: a fork after
    the parent's first call to one would be answered by the emulator as it is now, holding everything the parent
    did after the fork as well. Refused, naming the emulator and that call, as a provider whose state is outside
    the log is (`_refuse_state_outside_log`)."""
    first: dict[str, RecordedCall] = {}
    for call in store.calls():
        captured = call.exchange.captured
        if captured is None or captured.emulator is None or call.wake > checkpoint.wake:
            continue
        first.setdefault(captured.emulator, call)
    if first:
        named = "; ".join(
            f"{name} (first {c.exchange.method} {c.exchange.host}{c.exchange.path.split('?')[0]} at wake {c.wake})"
            for name, c in first.items()
        )
        raise RunRefused(
            f"the fork at seq {at_seq} of run {store.run_id} cannot rewind the external emulator(s) it had used: "
            f"{named}. An external emulator keeps its state outside the run's record, so a fork would be answered "
            "by it as the parent left it. Fork from a checkpoint before the agent first called it, or rerun from "
            "the beginning"
        )


async def _ask_again(
    child: Store,
    scenario: Scenario,
    people: set[str],
    checkpoint: Checkpoint,
    replier: Replier,
    clock: Clock,
) -> Checkpoint:
    """Put every message to a changed person that they have not answered by the fork again, and every item still
    waiting on them in the agent's own product, under their new behaviour.

    A reply that landed before the fork stays as it was: a `PersonChange` does not withdraw what was already said.
    A reply decided before the fork that had not landed by it was never said: it is withdrawn, as an edited
    message's is, and the person is asked again as they now are. A reply that would have landed before the fork
    lands at the fork instead, since the past is shared.
    """
    events = child.events()
    replies = child.replies()
    unsaid = [
        p.reply
        for p in checkpoint.pending
        if isinstance(p, PendingReply) and replies[p.reply].person in people and p.reply not in checkpoint.withdrawn
    ]
    withdrawn = [*checkpoint.withdrawn, *unsaid]
    answered = {(r.in_reply_to, r.person) for i, r in enumerate(replies) if i not in withdrawn}
    changed = {p.email: p for p in scenario.people if p.key in people}
    pending = [p for p in checkpoint.pending if not (isinstance(p, PendingReply) and p.reply in unsaid)]
    count = len(replies)
    unwritten = list(checkpoint.unwritten)
    held = items_in(events)
    for event in events:
        after = event.after
        if event.actor is Actor.AGENT and event.operation is Operation.CREATE and isinstance(after, InboxItemSnapshot):
            emails = [p.email for p in scenario.people if p.key == after.person]
            if held[event.entity].status is not ItemStatus.PENDING:
                continue  # decided or withdrawn by the fork: nothing is left to decide
        elif (
            event.actor is Actor.AGENT
            and event.operation is Operation.CREATE
            and isinstance(after, MessageSnapshot)
            and after.answerable
        ):
            emails = after.recipient_emails
        else:
            continue
        for email in emails:
            if email not in changed or (event.entity, changed[email].key) in answered:
                continue
            person = changed[email]
            history = [e for e in events if e.seq <= event.seq]
            owed = [(r.in_reply_to, r.at) for i, r in enumerate(child.replies()) if r.answers and i not in withdrawn]
            planned = replier.plan(person, event, history, owed)
            if planned is None:
                continue
            try:
                reply = await replier.write(person, event, planned, history, child, clock)
            except ModelFailed:
                unwritten.append(Unwritten(person=person.key, asked=event.seq))
                continue
            if reply is None:
                continue
            reply = reply.model_copy(update={"at": max(reply.at, clock.now())})
            child.remember(reply)
            pending.append(
                PendingReply(due=Due(at=reply.at, kind=DueKind.PERSON_REPLY, ref=f"reply:{count}"), reply=count)
            )
            count += 1
    return checkpoint.model_copy(
        update={"pending": pending, "replies": count, "withdrawn": withdrawn, "unwritten": unwritten}
    )


def _pin_owed(
    child: Store, scenario: Scenario, pins: list[ReplyAt], checkpoint: Checkpoint, clock: Clock
) -> Checkpoint:
    """A reply owed at the fork that answers an ask the fork pins lands where the pin says, or at the fork when that
    is already past: the pin wins over the moment drawn before the fork."""
    events = child.events()
    replies = child.replies()
    by_key = {p.key: p for p in scenario.people}
    owed = [(r.in_reply_to, r.at) for i, r in enumerate(replies) if r.answers and i not in checkpoint.withdrawn]
    pending: list[Pending] = []
    withdrawn = list(checkpoint.withdrawn)
    count = len(replies)
    for item in checkpoint.pending:
        if not isinstance(item, PendingReply) or item.reply in withdrawn:
            pending.append(item)
            continue
        reply = replies[item.reply]
        person = by_key[reply.person]
        asks = asks_of(person, events, owed)
        nth = next((n for n, e in enumerate(asks, start=1) if e.entity == reply.in_reply_to), None)
        pin = next((p for p in pins if p.person == person.key and p.to_ask == nth), None)
        if pin is None or nth is None:
            pending.append(item)
            continue
        asked = asks[nth - 1]
        drawn = pinned(scenario, asked, pin.after)
        moved = reply.model_copy(update={"at": max(drawn.lands_at, clock.now()), "drawn": drawn})
        child.remember(moved)
        withdrawn.append(item.reply)
        pending.append(PendingReply(due=Due(at=moved.at, kind=DueKind.PERSON_REPLY, ref=f"reply:{count}"), reply=count))
        count += 1
    return checkpoint.model_copy(update={"pending": pending, "replies": count, "withdrawn": withdrawn})
