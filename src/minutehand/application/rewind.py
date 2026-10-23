"""A fork: a finished run restarted from one of its checkpoints, with the scenario changed.

The child shares the parent's log up to the checkpoint (`Store.fork`), so the world, the clock and the
pending set come back by reading the log. Replies the parent's people had already decided are copied, not
asked for again, except for a person a `PersonChange` changes: each message to them not answered by the fork
(no reply decided, or one decided that had not landed yet) is put to them again under their new behaviour.

The agent's memory (`minutehand.agent.store`) is part of the same log, so the child has its parent's memory as it
stood at the checkpoint with nothing restored. Everything else the agent is lives in its sandbox, brought back by
playing the agent's recorded inputs into a fresh one up to the checkpoint (`application.restore`,
`application.playback`): the agent is asked nothing, and a fork is refused where the agent does not do what it did
before. A checkpoint the agent went on writing its memory after, in the same wake, is not one a fork can start
from. Every refusal that can be decided from the parent is decided before the child run exists, and a child refused
after it exists is discarded, so no refusal leaves a run behind.

What no fork can rewind, because it was never in the log: what a real third-party service the run reached keeps
(the proxy refuses unclaimed hosts, but a model API is reached for real), what a model provider keeps on its side
(a stored conversation, a cache, a batch), and the AWS provider's queues and schedules (in moto's memory). A provider
that says it keeps state outside the log (`Manifest.state_outside_log`, AWS) refuses any fork after the parent first
used it, a booking pending with it among what it built.

A booking pending at the checkpoint with any other scheduler is the child's too: the scheduler keeps it in the log the
child shares (a Cloud Tasks task, a Slack scheduled message), the checkpoint's table carries it, and the child
delivers it when its clock reaches it.
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
    PendingTransition,
    checkpoints,
)
from minutehand.application.inboxes import Inboxes
from minutehand.application.memory import store_digest
from minutehand.application.orchestrator import (
    Environment,
    Mounts,
    Orchestrator,
    OutsideState,
    Replay,
    Scorer,
    Services,
    acting,
    booked_due,
)
from minutehand.application.people import People, move
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import OwnProgram, Playback, Progress, Restored, start_fork
from minutehand.application.run_clock import RunClock
from minutehand.application.sandbox import SandboxClock
from minutehand.application.services import ServiceDesk
from minutehand.application.traffic import Traffic
from minutehand.domain.agent import AgentUnderTest
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
from minutehand.domain.transitions import ASSIGNEE, REASSIGN
from minutehand.domain.world import (
    Actor,
    EntityKind,
    MemorySnapshot,
    Operation,
    RecordedCall,
    WorldEvent,
)
from minutehand.ports.agent import TakesReplies
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.people import Replier
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry
from minutehand.ports.transitions import ProvidesTransitions

WireOverride = PromptPatch | ModelSwap

RESTORE_RECORD = "restore.json"
"""`Restored`, in the directory of the run the restore started."""

FORK_RECORD = "fork.json"
"""`Fork`, in the directory of each run it started: what the child changed, as it was asked for."""

CANNOT_REWIND = (
    "A fork rewinds the fakes' world, the agent's memory (`minutehand.agent.store`) and the agent's sandbox, from "
    "the record; it cannot rewind what a real third-party service the run reached keeps, what a model provider keeps "
    "on its side, the AWS provider's queues and schedules, or what an external emulator holds."
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
            changed: dict[str, object] = {"reply": override.reply}
            if override.takes is not None:
                changed["takes"] = override.takes
            people[override.person] = people[override.person].model_copy(update=changed)
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
    sandbox: SandboxClock,
    playback_for: Callable[[Store, Checkpoint], Playback],
    services: Services,
    replier_for: Callable[[Scenario, Sequence[ReplyAt]], Replier],
    model: LanguageModel | None = None,
    desk: ServiceDesk | None = None,
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
    replay: Replay | None = None,
    code_changed: Sequence[str] = (),
) -> list[RunRecord]:
    """Run the fork once per `Fork.samples`, each a child of `parent` named `run_id` (suffixed when sampled).

    `open_parent` opens the parent run's store stamping from the clock it is given; the child the store's
    `fork` makes stamps from that same clock, which this function moves to the checkpoint. `own` is the agent's
    program (its sandbox), not yet started: it is started once the child exists, and `playback_for` gives, for the
    parent's store and the checkpoint, the playback of the agent's recorded inputs into it. `progress` hears each
    step as it is taken. How each child's agent was brought back is kept as `restore.json` in its directory.

    `manifests` are those of every installed provider, beside the run's own `services`: a provider the agent called
    without the scenario or agent file naming it is still one whose state may be outside the log. `replay` is the
    parent's record answering the child's model calls after the checkpoint; frozen, it stops the child where the
    agent asks something the parent did not. `code_changed` says how the agent's program differs from the parent's,
    for a refusal to name it.
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
        _refuse_state_outside_log(
            parent_store, [*manifests, *(p.manifest for p in services.providers)], checkpoint, fork.at_seq
        )
        _refuse_emulated(parent_store, checkpoint, fork.at_seq)
        child = parent_store.fork(child_id, at_seq=fork.at_seq, clock=clock)
        try:
            clock.jump(checkpoint.now)
            while clock.wake() < checkpoint.wake:
                clock.begin_wake()
            replier = replier_for(changed, [o for o in fork.overrides if isinstance(o, ReplyAt)])
            orchestrator = Orchestrator(
                scenario=changed,
                agent=agent,
                sandbox=sandbox,
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
                model=model,
                desk=desk,
                pins=[o for o in fork.overrides if isinstance(o, ReplyAt)],
                replay=replay,
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
                playback=playback_for(parent_store, checkpoint),
                own=own,
                progress=progress,
                edit=edit if edits else None,
                code_changed=code_changed,
            )
            for reply in parent_store.replies()[: checkpoint.replies]:
                child.remember(reply)
            replanning = {o.person for o in fork.overrides if isinstance(o, PersonChange | ReplyAt)}
            if replanning:
                checkpoint = await _replanned(orchestrator, child, replanning, checkpoint, clock)
            await _edit_tickets(fork, orchestrator.people, changed, child, clock)
        except RunRefused:
            child.discard()
            raise
        _keep(state_dir / child_id, restored, fork)
        if wire is not None and on_wire:
            wire.apply(child_id, on_wire)
        records.append(await orchestrator.resume(checkpoint))
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
    writes = store.events(since=at_seq, kinds=[EntityKind.MEMORY], actors=[Actor.AGENT])
    return refused_by(memory_writes(writes), at_seq, checkpoint)


def memory_writes(events: Sequence[WorldEvent]) -> dict[int, list[WorldEvent]]:
    """The agent's writes to its memory, by the wake they were made in, in order: what `refused_by` reads, gathered
    once for every checkpoint of a log."""
    found: dict[int, list[WorldEvent]] = {}
    for e in events:
        if (
            e.actor is Actor.AGENT
            and e.entity.kind is EntityKind.MEMORY
            and e.operation not in (Operation.READ, Operation.SEARCH)
        ):
            found.setdefault(e.wake, []).append(e)
    return found


def refused_by(writes: dict[int, list[WorldEvent]], at_seq: int, checkpoint: Checkpoint) -> NotRestorable | None:
    """`not_restorable`, over the memory writes of the log (`memory_writes`)."""
    late = [e for e in (writes[checkpoint.wake] if checkpoint.wake in writes else []) if e.seq > at_seq]
    if not late:
        return None
    first = late[0].after
    key = f"{first.collection}/{first.key}" if isinstance(first, MemorySnapshot) else late[0].entity.external_id
    return NotRestorable(
        reason=f"the agent went on writing its memory after this checkpoint, in the same wake ({len(late)} write(s), "
        f"the first to {key} at seq {late[0].seq}): its memory here was half written, since the sandbox fell idle "
        "before the agent had finished"
    )


def _refuse_ticket_edits(fork: Fork, services: Services, scenario: Scenario) -> None:
    """Every `TicketEdit` must land: on a provider in the run whose tickets the scenario can set, to a person in it."""
    for override in fork.overrides:
        if not isinstance(override, TicketEdit):
            continue
        found = next((p for p in services.providers if p.manifest.key == override.entity.provider), None)
        if (
            found is None
            or not isinstance(acting(found), ProvidesTransitions)
            or EntityKind.TICKET not in found.manifest.kinds
        ):
            raise RunRefused(f"the fork edits a ticket on {override.entity.provider}, which cannot edit tickets")
        if override.assignee is not None and override.assignee not in {p.key for p in scenario.people}:
            raise RunRefused(f"the fork assigns a ticket to {override.assignee}, who is not in the scenario")


async def _edit_tickets(fork: Fork, people: People, scenario: Scenario, child: Store, clock: Clock) -> None:
    """Each `TicketEdit`, made by the scenario through the ticket's provider (`apply`): its state set, past any
    workflow, and its assignee changed, each recorded as the transition it is."""
    emails = {p.key: p.email for p in scenario.people if p.email}
    for override in fork.overrides:
        if not isinstance(override, TicketEdit):
            continue
        port = people.port(override.entity.provider)
        try:
            if override.state is not None:
                await move(port, override.entity, override.state.value, Actor.SCENARIO, None, {}, child, clock)
            if override.assignee is not None:
                given = {ASSIGNEE: emails[override.assignee]}
                await move(port, override.entity, REASSIGN, Actor.SCENARIO, None, given, child, clock)
        except ValueError as e:
            raise RunRefused(f"the fork's edit of {override.entity.external_id} cannot land: {e}") from e


def _keep(directory: Path, restored: Restored, fork: Fork) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RESTORE_RECORD).write_text(restored.model_dump_json(indent=2), encoding="utf-8")
    (directory / FORK_RECORD).write_text(fork.model_dump_json(indent=2), encoding="utf-8")


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
    for event in store.events(until=at_seq, providers=list(outside)) if outside else []:
        used.setdefault(event.entity.provider, [])
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


async def _replanned(
    orchestrator: Orchestrator, child: Store, people: set[str], checkpoint: Checkpoint, clock: Clock
) -> Checkpoint:
    """Every ask `people` still owe at the fork planned again under the scenario as the fork has it (their new
    behaviour, a moment it pins), each booked at its new moment, or at the fork when that is already past since the
    past is shared. What they said before the fork stays said."""
    moved = await orchestrator.people.replan(child, clock, people)
    refs = {b.pending.external_id for b in moved}
    pending: list[Pending] = [
        p for p in checkpoint.pending if not (isinstance(p, PendingTransition) and p.pending.external_id in refs)
    ]
    for booked in moved:
        due = booked_due(booked, clock.now())
        if due is not None:
            pending.append(due)
    return checkpoint.model_copy(update={"pending": pending})
