"""A fork: a finished run restarted from one of its checkpoints, with the scenario changed.

The child shares the parent's log up to the checkpoint (`Store.fork`), so the world, the clock and the
pending set come back by reading the log. Replies the parent's people had already decided are copied, not
asked for again, except where a `PersonChange` makes someone answer who had decided not to: each message
to them still unanswered at the fork is put to them again under their new behaviour. The agent's own state comes back through its `StateHooks.restore`; without hooks the fork
is refused, because a world rewound under an agent that remembers the future is not a rerun.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from minutehand.application.checkpoint import Checkpoint, PendingReply, checkpoint_seqs, read_checkpoint
from minutehand.application.orchestrator import Mounts, Orchestrator, Reach, Scorer, Services
from minutehand.application.refusals import RunRefused
from minutehand.application.run_clock import RunClock
from minutehand.application.state_hooks import run_hook, wake_dir
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.experiment import DeadlineShift, Fork, ModelSwap, PersonChange, PromptPatch, TicketEdit
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, MessageSnapshot, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.people import Replier
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

WireOverride = PromptPatch | ModelSwap


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
    scorer: Scorer | None = None,
    poll_interval: float = 0.05,
    max_polls: int = 1200,
) -> list[RunRecord]:
    """Run the fork once per `Fork.samples`, each a child of `parent` named `run_id` (suffixed when sampled).

    `open_parent` opens the parent run's store stamping from the clock it is given; the child the store's
    `fork` makes stamps from that same clock, which this function moves to the checkpoint.
    """
    if fork.parent_run != parent.run_id:
        raise RunRefused(f"the fork names parent {fork.parent_run}; the record given is {parent.run_id}")
    if agent.state is None:
        raise RunRefused(
            f"agent {agent.name} has no state hooks, so its own state cannot be rewound; "
            "declare `state: {snapshot: [...], restore: [...]}` or rerun from the beginning"
        )
    on_wire = [o for o in fork.overrides if isinstance(o, (PromptPatch, ModelSwap))]
    if on_wire and wire is None:
        raise RunRefused(
            f"the fork patches the agent's model calls ({', '.join(o.kind for o in on_wire)}) "
            "and nothing is on the wire to apply them"
        )
    changed = changed_scenario(scenario, fork)
    records: list[RunRecord] = []
    for sample in range(fork.samples):
        child_id = run_id if fork.samples == 1 else f"{run_id}-{sample + 1}"
        clock = RunClock(scenario.starts_at)
        parent_store = open_parent(clock)
        seqs = checkpoint_seqs(parent_store)
        if fork.at_seq not in seqs:
            raise RunRefused(
                f"run {parent.run_id} has no checkpoint at seq {fork.at_seq}; "
                f"a fork is taken where a wake ended: {seqs}"
            )
        child = parent_store.fork(child_id, at_seq=fork.at_seq, clock=clock)
        checkpoint = read_checkpoint(child)
        assert checkpoint is not None
        clock.jump(checkpoint.now)
        while clock.wake() < checkpoint.wake:
            clock.begin_wake()
        for reply in parent_store.replies()[: checkpoint.replies]:
            child.remember(reply)
        replier = replier_for(changed)
        changed_people = {o.person for o in fork.overrides if isinstance(o, PersonChange)}
        if changed_people:
            checkpoint = await _ask_again(child, changed, changed_people, checkpoint, replier, clock)
        for override in fork.overrides:
            if isinstance(override, TicketEdit):
                if override.entity.provider not in services.editors:
                    raise RunRefused(
                        f"the fork edits a ticket on {override.entity.provider}, which cannot edit tickets"
                    )
                editor = services.editors[override.entity.provider]
                assignee = next((p.email for p in changed.people if p.key == override.assignee), None)
                if override.assignee is not None and assignee is None:
                    raise RunRefused(f"the fork assigns a ticket to {override.assignee}, who is not in the scenario")
                editor.edit(override.entity, state=override.state, assignee_email=assignee, world=child, clock=clock)
        restore_from = wake_dir(state_dir, parent.run_id, checkpoint.wake)
        if not restore_from.is_dir():
            raise RunRefused(f"no snapshot of agent {agent.name} at {restore_from}")
        await run_hook(agent.state.restore, restore_from)
        if wire is not None and on_wire:
            wire.apply(child_id, on_wire)
        records.append(
            await Orchestrator(
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
                poll_interval=poll_interval,
                max_polls=max_polls,
                parent_run=parent.run_id,
                forked_at=fork.at_seq,
                prior_wakes=[w for w in parent.wakes if w.index <= checkpoint.wake],
            ).resume(checkpoint)
        )
    return records


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
    answered = {(r.in_reply_to, r.person) for r in replies}
    changed = {p.email: p for p in scenario.people if p.key in people}
    pending = list(checkpoint.pending)
    count = len(replies)
    for event in events:
        after = event.after
        if not (
            event.actor is Actor.AGENT and event.operation is Operation.CREATE and isinstance(after, MessageSnapshot)
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
