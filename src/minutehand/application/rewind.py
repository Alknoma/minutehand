"""A fork: a finished run restarted from one of its checkpoints, with the scenario changed.

The child shares the parent's log up to the checkpoint (`Store.fork`), so the world, the clock and the
pending set come back by reading the log. Replies the parent's people had already decided are copied, not
asked for again, except where a `PersonChange` makes someone answer who had decided not to: each message
to them still unanswered at the fork is put to them again under their new behaviour.

The agent's own state comes back through the restore sequence in `application.restore`, and is verified there
against the report recorded at the checkpoint. Without hooks the fork is refused, because a world rewound under
an agent that remembers the future is not a rerun; so is a fork from a checkpoint at which the agent did not
settle. Every refusal that can be decided from the parent is decided before the child run exists, and a child
refused after it exists (its restore failed) is discarded, so no refusal leaves a run behind.

What no fork can rewind, because it was never in the log or the snapshot: what a real third-party service the
run reached keeps (the proxy refuses unclaimed hosts, but a model API is reached for real), what a model
provider keeps on its side (a stored conversation, a cache, a batch), the AWS provider's queues and schedules
(in moto's memory), and work the agent does in the background that outlives the quiet period.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from minutehand.application.checkpoint import (
    Checkpoint,
    NoHooks,
    NotRestorable,
    PendingBooking,
    PendingReply,
    Restorable,
    checkpoints,
)
from minutehand.application.orchestrator import Mounts, Orchestrator, Reach, Scorer, Services
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import OwnProgram, Progress, Restored, Traffic, restore_agent
from minutehand.application.run_clock import RunClock
from minutehand.application.state_hooks import SNAPSHOT_PRUNED, materialised, restore_dir
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.experiment import DeadlineShift, Fork, ModelSwap, PersonChange, PromptPatch, TicketEdit
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.world import Actor, MessageSnapshot, Operation
from minutehand.ports.agent import Reports, TakesReplies
from minutehand.ports.clock import Clock
from minutehand.ports.people import Replier
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

WireOverride = PromptPatch | ModelSwap

RESTORE_RECORD = "restore.json"
"""`Restored`, in the directory of the run the restore started."""

CANNOT_REWIND = (
    "A fork rewinds the fakes' world and the agent's own state; it cannot rewind what a real third-party service "
    "the run reached keeps, what a model provider keeps on its side, the AWS provider's queues and schedules, "
    "or work the agent does in the background after the quiet period."
)


class OnTheWire(Protocol):
    """Whoever edits the agent's model calls in flight (the proxy). `fork_run` hands these over untouched."""

    def apply(self, run_id: str, overrides: list[WireOverride]) -> None: ...


def changed_scenario(scenario: Scenario, fork: Fork) -> Scenario:
    """The scenario the child runs: people and deadline as the fork's overrides say."""
    people = {p.key: p for p in scenario.people}
    deadline_after = scenario.deadline_after
    for override in fork.overrides:
        if isinstance(override, PersonChange):
            if override.person not in people:
                raise RunRefused(f"the fork changes {override.person}, who is not in scenario {scenario.name}")
            people[override.person] = people[override.person].model_copy(update={"reply": override.reply})
        elif isinstance(override, DeadlineShift):
            if deadline_after is None:
                raise RunRefused(f"the fork shifts the deadline of scenario {scenario.name}, which has none")
            deadline_after += override.by
    return Scenario.model_validate(
        {**scenario.model_dump(), "people": [p.model_dump() for p in people.values()], "deadline_after": deadline_after}
    )


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
) -> list[RunRecord]:
    """Run the fork once per `Fork.samples`, each a child of `parent` named `run_id` (suffixed when sampled).

    `open_parent` opens the parent run's store stamping from the clock it is given; the child the store's
    `fork` makes stamps from that same clock, which this function moves to the checkpoint. `own` is the agent's
    program when Minutehand started it, stopped and started again around the restore; `progress` hears each
    restore step as it is taken. Each child's restore is kept as `restore.json` in its directory.
    """
    if fork.parent_run != parent.run_id:
        raise RunRefused(f"the fork names parent {fork.parent_run}; the record given is {parent.run_id}")
    hooks = agent.state
    if hooks is None:
        raise RunRefused(
            f"agent {agent.name} has no state hooks, so its own state cannot be rewound; "
            "declare `state: {snapshot: [...], restore: [...]}` or rerun from the beginning. " + CANNOT_REWIND
        )
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
        restorable = _restorable(checkpoint, agent, parent.run_id, fork.at_seq)
        _refuse_unkept(parent_store, restorable, agent, parent.run_id, fork.at_seq)
        _refuse_pending_bookings(checkpoint, parent.run_id, fork.at_seq)
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
                state_dir=state_dir,
                signing=signing,
                parent_run=parent.run_id,
                forked_at=fork.at_seq,
                prior_wakes=[w for w in parent.wakes if w.index <= checkpoint.wake],
                traffic=traffic,
                channels=channels,
            )
            orchestrator.mount()
            with materialised(
                parent_store, restorable.snapshot_of, restorable.wake, restore_dir(state_dir, child_id)
            ) as snapshot:
                restored = await restore_agent(
                    hooks,
                    snapshot,
                    checkpoint_seq=fork.at_seq,
                    recorded=restorable.report,
                    reports=reports,
                    own=own,
                    progress=progress,
                    fingerprint=restorable.fingerprint,
                )
            for reply in parent_store.replies()[: checkpoint.replies]:
                child.remember(reply)
            changed_people = {o.person for o in fork.overrides if isinstance(o, PersonChange)}
            if changed_people:
                checkpoint = await _ask_again(child, changed, changed_people, checkpoint, replier, clock)
            _edit_tickets(fork, services, changed, child, clock)
        except RunRefused:
            child.discard()
            raise
        _keep(state_dir / child_id, restored)
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


def _restorable(checkpoint: Checkpoint, agent: AgentUnderTest, parent: str, at_seq: int) -> Restorable:
    """The snapshot to restore from, or the reason there is none, recorded when the checkpoint was taken."""
    state = checkpoint.agent
    if isinstance(state, NotRestorable):
        raise RunRefused(
            f"the checkpoint at seq {at_seq} of run {parent} is not restorable: {state.reason}. "
            "Fork from a checkpoint `minutehand findings` lists as restorable. " + CANNOT_REWIND
        )
    if isinstance(state, NoHooks):
        raise RunRefused(
            f"run {parent} was played by an agent with no state hooks, so nothing of agent {agent.name}'s own "
            f"state was kept at seq {at_seq}; declare `state:` and play the run again. " + CANNOT_REWIND
        )
    return state


def _refuse_unkept(store: Store, restorable: Restorable, agent: AgentUnderTest, parent: str, at_seq: int) -> None:
    """The snapshot a restorable checkpoint names must still be kept: one pruned, or never kept, is refused."""
    kept = store.snapshot(restorable.snapshot_of, restorable.wake)
    if kept is None:
        raise RunRefused(
            f"no snapshot of agent {agent.name} after wake {restorable.wake} of run {restorable.snapshot_of} is kept"
        )
    if kept.pruned:
        raise RunRefused(
            f"the checkpoint at seq {at_seq} of run {parent} is not restorable: {SNAPSHOT_PRUNED}. "
            "Fork from a checkpoint `minutehand checkpoints` lists as restorable, or pin one before it is pruned. "
            + CANNOT_REWIND
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


def _keep(directory: Path, restored: Restored) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RESTORE_RECORD).write_text(restored.model_dump_json(indent=2), encoding="utf-8")


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


async def _ask_again(
    child: Store,
    scenario: Scenario,
    people: set[str],
    checkpoint: Checkpoint,
    replier: Replier,
    clock: Clock,
) -> Checkpoint:
    """Put every message to a changed person that has no reply decided to them again, under their new behaviour.

    A reply decided before the fork stays as it was: a `PersonChange` does not withdraw what was already said.
    A reply that would have landed before the fork lands at the fork instead, since the past is shared.
    """
    events = child.events()
    replies = child.replies()
    answered = {(r.in_reply_to, r.person) for i, r in enumerate(replies) if i not in checkpoint.withdrawn}
    changed = {p.email: p for p in scenario.people if p.key in people}
    pending = list(checkpoint.pending)
    count = len(replies)
    for event in events:
        after = event.after
        if not (
            event.actor is Actor.AGENT
            and event.operation is Operation.CREATE
            and isinstance(after, MessageSnapshot)
            and after.answerable
        ):
            continue
        for email in after.recipient_emails:
            if email not in changed or (event.entity, changed[email].key) in answered:
                continue
            reply = await replier.decide(changed[email], event, [e for e in events if e.seq <= event.seq], clock)
            if reply is None:
                continue
            reply = reply.model_copy(update={"at": max(reply.at, clock.now())})
            child.remember(reply)
            pending.append(
                PendingReply(due=Due(at=reply.at, kind=DueKind.PERSON_REPLY, ref=f"reply:{count}"), reply=count)
            )
            count += 1
    return checkpoint.model_copy(update={"pending": pending, "replies": count})
