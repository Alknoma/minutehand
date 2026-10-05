"""A fork, told: where it split from its parent, what it changed, whether its restore was proven, and how its
outcome differs from its parent's from that point on.

Every surface that shows a fork (`minutehand findings` and `runs`, the viewer, the MCP run summary) states it from
`ForkAccount`, so they cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.application.checkpoint import CHECKPOINT, Checkpoint, Restorable
from minutehand.application.model_calls import is_model_call, model_call
from minutehand.application.restore import Restored, Verification
from minutehand.checks.runner import RunResult
from minutehand.domain.checks import Effectiveness, Finding
from minutehand.domain.experiment import (
    CallMatch,
    DeadlineShift,
    Fork,
    ModelSwap,
    Override,
    PersonChange,
    PromptPatch,
    TicketEdit,
)
from minutehand.domain.run import Verdict
from minutehand.domain.scenario import (
    Answers,
    Helpfulness,
    Model,
    Person,
    ReplyBehaviour,
    Scenario,
    Scripted,
    Silent,
)
from minutehand.domain.telemetry import StoredSpan
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    GrantSnapshot,
    InteractionSnapshot,
    MessageSnapshot,
    Operation,
    RecordedCall,
    RecordSnapshot,
    TicketSnapshot,
    WorldEvent,
    assigned,
    reached,
)

QUOTED = 160
"""Characters of a message or a patch quoted in one line before it is cut."""


class ScoreLine(Model):
    """One line of a scorecard, as every surface prints it."""

    label: str
    value: str


class ScoreDifference(Model):
    label: str
    parent: str
    fork: str


class DivergenceKind(StrEnum):
    """What a run's record holds after a fork point, each compared between a fork and its parent."""

    CHANGE = "change"  # a change in the world: who did what to which entity, and what it then said
    CALL = "call"  # a call the agent made, captured and tunnelled ones too: where to, what it sent and was answered
    REPORT = "report"  # what the agent reported at the end of a wake: its status, next wake and commitments
    MODEL_CALL = "model_call"  # a model call the run received or recorded: what the model was asked and answered


KIND_WORDS = {
    DivergenceKind.CHANGE: ("change in the world", "changes in the world"),
    DivergenceKind.CALL: ("call", "calls"),
    DivergenceKind.REPORT: ("report of the agent's", "reports of the agent's"),
    DivergenceKind.MODEL_CALL: ("model call", "model calls"),
}


class DivergentEvent(Model):
    """One thing in the record on one side of a fork, in words: a change, a call, a report or a model call."""

    kind: DivergenceKind
    seq: int = Field(
        description="Where in the log: the change's or report's seq, the seq a call began at, the head a "
        "model call arrived at"
    )
    wake: int
    at: AwareDatetime = Field(description="Simulated time")
    words: str


class FirstDivergence(Model):
    """The first thing in the record, after the fork point, at which the two records part: a change in the world,
    a call, a report of the agent's or a model call, whichever comes first."""

    kind: DivergenceKind = Field(description="The kind of thing that differed first")
    parent: DivergentEvent | None = Field(description="None: the parent's record has nothing more of it there")
    fork: DivergentEvent | None = Field(description="None: the fork's record has nothing more of it there")
    shared: int = Field(
        ge=0, description="Things of the record after the fork point both made alike before this one, of this kind"
    )
    differs: str = Field(description="What about it differs, in words")


class ChangedFinding(Model):
    """A finding of the same check and kind on both sides that says something different in the fork."""

    parent: Finding
    fork: Finding


class Outcomes(Model):
    """How the fork's outcome differs from its parent's. Findings are paired by check and kind, in order: a pair
    whose messages differ is changed, one with no partner is gained or lost."""

    parent_verdict: Verdict
    fork_verdict: Verdict
    verdict_changed: bool
    scorecard: list[ScoreDifference] = Field(description="Only the lines that differ")
    findings_gained: list[Finding] = Field(description="In the fork and not in its parent")
    findings_lost: list[Finding] = Field(description="In the parent and not in the fork")
    findings_changed: list[ChangedFinding] = Field(description="On both sides, saying something different")
    first_divergence: FirstDivergence | None = Field(
        description="None: from the fork point on, the two records made the same changes at the same moments"
    )


class RestoreAccount(Model):
    verified: bool
    by: list[Verification] = Field(description="What the restore was compared by; empty when not verified")
    unverified: str | None = Field(description="Why it could not be verified")
    words: str


class ForkAccount(Model):
    parent_run: str
    at_seq: int = Field(description="The checkpoint the fork split from")
    after_wake: int = Field(description="The wake whose end that checkpoint is; 0 is setup")
    at: AwareDatetime = Field(description="The simulated moment of the split")
    ran_on: bool = Field(
        default=False,
        description="The checkpoint is not the wake's end but a later one: the clock ran on, with nothing due, to "
        "the scenario's deadline",
    )
    changes: list[str] = Field(description="Each change the fork made, in words, in the order it was asked for")
    summary: str = Field(description="What it changed, short enough for a list of runs")
    restore: RestoreAccount | None = Field(description="None when no restore was recorded with the fork")
    outcome: Outcomes | None = Field(description="None until both the fork and its parent have finished")


# -- what it changed ------------------------------------------------------------------------------------------


def span(delta: timedelta) -> str:
    """A simulated duration as a person reads it."""
    secs = abs(delta.total_seconds())
    if secs < 90:
        return f"{secs:.0f} seconds"
    if secs < 5400:
        return f"{secs / 60:.0f} minutes"
    if secs < 172800:
        hours = round(secs / 3600)
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{secs / 86400:.1f} days"


def _moment(at: datetime) -> str:
    return f"{at:%Y-%m-%d %H:%M} UTC"


def _quoted(text: str, limit: int = QUOTED) -> str:
    flat = " ".join(text.split())
    return f"“{flat if len(flat) <= limit else flat[: limit - 1] + '…'}”"


def behaviour(reply: ReplyBehaviour) -> str:
    """How a person answers, in words."""
    if isinstance(reply, Silent):
        return "never answers"
    shortest, longest = reply.delay.shortest, reply.delay.longest
    delay = f"after {span(shortest)}" if shortest == longest else f"after {span(shortest)} to {span(longest)}"
    if isinstance(reply, Scripted):
        said = f"answers with {len(reply.replies)} scripted repl{'y' if len(reply.replies) == 1 else 'ies'} {delay}"
        if reply.presses_every is not None:
            said += f", and presses {_quoted(reply.presses_every.label)} on every message that offers it"
        return said
    assert isinstance(reply, Answers)
    how = {
        Helpfulness.FULL: "answers in full",
        Helpfulness.PARTIAL: "answers in part",
        Helpfulness.ASKS_BACK: "answers with a question of their own",
        Helpfulness.DECLINES: "declines, naming nobody",
        Helpfulness.MISTAKEN: "answers from an out-of-date fact",
    }[reply.helpfulness]
    return f"{how}, written by a model, {delay}"


def _calls(where: CallMatch) -> str:
    parts: list[str] = []
    if where.host is not None:
        parts.append(f"to {where.host}")
    if where.model is not None:
        parts.append(f"asking for {where.model}")
    if where.system_contains is not None:
        parts.append(f"whose system prompt contains {_quoted(where.system_contains, 60)}")
    return "every model call the agent makes" if not parts else "the agent's model calls " + ", ".join(parts)


def _person(people: dict[str, Person], key: str) -> str:
    person = people.get(key)
    return f"{person.name} ({key})" if person is not None else key


def change_words(
    override: Override,
    scenario: Scenario,
    *,
    ticket_before: TicketSnapshot | None = None,
    models_before: list[str] | None = None,
) -> str:
    """One override as a sentence: what it changed, from what to what, and where.

    `ticket_before` is the ticket a `TicketEdit` names as it stood at the fork; `models_before` the models the
    parent's model calls asked for after the fork point, as its telemetry names them."""
    people = {p.key: p for p in scenario.people}
    if isinstance(override, PersonChange):
        was = people.get(override.person)
        before = behaviour(was.reply) if was is not None else "was not in the scenario"
        return f"{_person(people, override.person)} {behaviour(override.reply)} from the fork on; before, {before}"
    if isinstance(override, PromptPatch):
        if override.find is None:
            return f"appends {_quoted(override.text)} to the system prompt of {_calls(override.where)}"
        return f"replaces {_quoted(override.find)} with {_quoted(override.text)} in the system prompt of " + _calls(
            override.where
        )
    if isinstance(override, ModelSwap):
        before = (
            override.where.model
            or (", ".join(models_before) if models_before else None)
            or "the model each call asked for (the parent's telemetry names none)"
        )
        return f"swaps the model from {before} to {override.to} for {_calls(override.where)}"
    if isinstance(override, TicketEdit):
        ref = override.entity
        name = (
            f"{_quoted(ticket_before.title, 60)} ({ref.provider} {ref.external_id})"
            if ticket_before
            else (f"{ref.provider} {ref.external_id}")
        )
        said: list[str] = []
        if override.state is not None:
            was = ticket_before.state.value if ticket_before is not None else "unknown"
            said.append(f"state {was} to {override.state.value}")
        if override.assignee is not None:
            held = assigned(ticket_before, scenario.people) if ticket_before is not None else None
            holder = ticket_before.assignee_email if ticket_before is not None else None
            was = held.name if held is not None else (holder or "nobody")
            said.append(f"assignee {was} to {_person(people, override.assignee)}")
        return f"edits ticket {name} at the fork: " + (", ".join(said) or "nothing")
    assert isinstance(override, DeadlineShift)
    which = "later" if override.by >= timedelta(0) else "earlier"
    deadline = scenario.deadline
    if deadline is None:
        return f"moves the deadline {span(override.by)} {which}; the scenario had none"
    return (
        f"moves the deadline {span(override.by)} {which}: from {_moment(deadline)} to {_moment(deadline + override.by)}"
    )


def short_words(override: Override, scenario: Scenario) -> str:
    """One override in a few words, for a list of runs."""
    people = {p.key: p for p in scenario.people}
    if isinstance(override, PersonChange):
        person = people.get(override.person)
        name = person.name if person is not None else override.person
        return f"{name} {behaviour(override.reply).split(',')[0]}"
    if isinstance(override, PromptPatch):
        verb = "prompt +" if override.find is None else "prompt edit"
        return f"{verb} {_quoted(override.text, 40)}"
    if isinstance(override, ModelSwap):
        return f"model to {override.to}"
    if isinstance(override, TicketEdit):
        to = [override.state.value] if override.state is not None else []
        to += [f"to {override.assignee}"] if override.assignee is not None else []
        return f"ticket {override.entity.external_id} {' '.join(to)}".rstrip()
    assert isinstance(override, DeadlineShift)
    sign = "+" if override.by >= timedelta(0) else "-"
    return f"deadline {sign}{span(override.by)}"


def summary(fork: Fork, scenario: Scenario) -> str:
    if not fork.overrides:
        return "Rerun, nothing changed"
    return "; ".join(short_words(o, scenario) for o in fork.overrides)


# -- whether its restore was proven ---------------------------------------------------------------------------


def restore_account(restored: Restored) -> RestoreAccount:
    if restored.verified:
        by = " and ".join(
            {
                Verification.REPORT: "the agent's report (status, next wake, commitments)",
                Verification.FINGERPRINT: "the fingerprint of its state",
            }[v]
            for v in restored.verified_by
        )
        words = f"verified: {by} equal{'s' if len(restored.verified_by) == 1 else ''} the checkpoint's"
    else:
        words = f"not verified: {restored.unverified}"
    return RestoreAccount(
        verified=restored.verified, by=restored.verified_by, unverified=restored.unverified, words=words
    )


# -- how its outcome differs ----------------------------------------------------------------------------------


def scorecard_lines(card: Effectiveness) -> list[ScoreLine]:
    lines = [
        ScoreLine(label="expectations met", value=f"{card.expectations_met} of {card.expectations_total}"),
        ScoreLine(label="waits opened", value=f"{card.waits_opened}, still open at the end: {card.waits_open_at_end}"),
        ScoreLine(
            label="follow-ups due",
            value=f"{card.follow_ups_due}, made: {card.follow_ups_made}, late: {card.follow_ups_late}, "
            f"early: {card.follow_ups_early}",
        ),
        ScoreLine(label="time the agent lost", value=_lost(card.time_lost)),
        ScoreLine(label="wakes", value=f"{card.wakes}, of which changed nothing: {card.idle_wakes}"),
        ScoreLine(label="messages to people", value=str(card.messages_to_people)),
        ScoreLine(label="failed checks", value=str(card.failed_checks)),
    ]
    if card.slowest_follow_up is not None:
        lines.insert(
            4, ScoreLine(label="slowest follow-up", value=f"{_lost(card.slowest_follow_up)} after its wait expired")
        )
    return lines


def _lost(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.0f} hours"


def _paired(parent: list[Finding], fork: list[Finding]) -> tuple[list[Finding], list[Finding], list[ChangedFinding]]:
    """Gained, lost and changed: findings paired by check and kind in order, an identical message first."""
    gained: list[Finding] = []
    changed: list[ChangedFinding] = []
    left = list(parent)
    for found in fork:
        same = [f for f in left if (f.check, f.kind) == (found.check, found.kind)]
        if not same:
            gained.append(found)
            continue
        partner = next((f for f in same if f.message == found.message), same[0])
        left.remove(partner)
        if partner.message != found.message:
            changed.append(ChangedFinding(parent=partner, fork=found))
    return gained, left, changed


def event_words(event: WorldEvent, scenario: Scenario) -> str:
    """One change in the world as a sentence: who did what to what, and when."""
    who = {Actor.AGENT: "the agent", Actor.PERSON: "a person", Actor.SCENARIO: "the scenario"}[event.actor]
    verb = {Operation.CREATE: "created", Operation.UPDATE: "changed", Operation.DELETE: "deleted"}.get(
        event.operation, event.operation.value
    )
    after = event.after
    if isinstance(after, MessageSnapshot) and event.actor is Actor.PERSON:
        what = (
            f"a person {'wrote' if event.operation is Operation.CREATE else verb + ' a message'}: {_quoted(after.text)}"
        )
    elif isinstance(after, MessageSnapshot):
        named = reached(after, scenario.people)
        known = {p.email for p in named if p.email is not None}
        to = ", ".join([p.name for p in named] + [e for e in after.recipient_emails if e not in known]) or after.channel
        what = (
            f"{who} {'sent' if event.operation is Operation.CREATE else verb} a message to {to}: {_quoted(after.text)}"
        )
    elif isinstance(after, TicketSnapshot):
        held = assigned(after, scenario.people)
        holder = (
            f", for {held.name if held is not None else after.assignee_email}" if held or after.assignee_email else ""
        )
        what = f"{who} {verb} ticket {_quoted(after.title, 80)} ({after.state.value}{holder})"
    elif isinstance(after, DocumentSnapshot):
        what = f"{who} {verb} document {_quoted(after.title, 80)}"
    elif isinstance(after, GrantSnapshot):
        what = f"{who} gave {after.to} {after.role.value} access to {_quoted(after.document, 80)}"
    elif isinstance(after, RecordSnapshot):
        what = f"{who} {verb} a {after.resource} record: {_quoted(after.text)}"
    elif isinstance(after, InteractionSnapshot):
        names = {p.key: p.name for p in scenario.people}
        what = f"{names.get(after.person, after.person)} pressed {_quoted(after.label, 60)}"
    else:
        ref = event.entity
        what = f"{who} {verb} {ref.kind.value} {ref.external_id} in {ref.provider}"
    return f"wake {event.wake}, {_moment(event.sim_time)}: {what}"


@dataclass(frozen=True)
class Record:
    """A run's record from a fork point on, as compared: its changes in the world, its calls, its checkpoints (the
    agent's reports) and the model calls it received, each as the store holds it."""

    events: list[WorldEvent]
    calls: list[RecordedCall] = field(default_factory=lambda: list[RecordedCall]())
    spans: list[StoredSpan] = field(default_factory=lambda: list[StoredSpan]())
    checkpoints: dict[int, Checkpoint] = field(default_factory=lambda: dict[int, Checkpoint]())


@dataclass(frozen=True)
class _Item:
    kind: DivergenceKind
    wake: int
    seq: int
    rank: int
    at: datetime
    key: tuple[object, ...]
    words: str


def _digest(text: str | None, raw: bytes | None) -> str:
    return hashlib.sha256(raw if raw is not None else (text or "").encode("utf-8")).hexdigest()


def _call_words(call: RecordedCall) -> str:
    x = call.exchange
    if x.tunnelled is not None:
        return f"the agent's tunnel to {x.path} carried a call, never opened"
    sent = f", sending {_quoted(x.request_body, 100)}" if x.request_body else ""
    answered = f"; answered {x.status} {_quoted(x.response_body, 100)}" if x.response_body else f"; answered {x.status}"
    return f"the agent called {x.method} {x.host}{x.path.split('?', 1)[0]}{sent}{answered}"


def _items(record: Record, at_seq: int, after_wake: int, scenario: Scenario) -> list[_Item]:
    """The record after the fork point in log order: each call where it began, ahead of the changes it made; each
    change; each checkpoint as the agent's report at the end of its wake. Reads and searches are not compared."""
    found: list[_Item] = []
    for e in record.events:
        if e.seq <= at_seq or e.operation in (Operation.READ, Operation.SEARCH):
            continue
        if e.entity == CHECKPOINT:
            continue
        key = (e.actor, e.operation, e.entity, e.after, e.sim_time)
        found.append(_Item(DivergenceKind.CHANGE, e.wake, e.seq, 1, e.sim_time, key, event_words(e, scenario)))
    for seq, checkpoint in ((q, c) for q, c in record.checkpoints.items() if q > at_seq):
        report = checkpoint.agent.report if isinstance(checkpoint.agent, Restorable) else None
        key = (checkpoint.wake, json.dumps([c.model_dump(mode="json") for c in checkpoint.commitments or []]),
               report.model_dump_json() if report is not None else None)  # fmt: skip
        status = f"{report.status.value}" if report is not None else "nothing of its status"
        held = len(checkpoint.commitments or [])
        words = (
            f"wake {checkpoint.wake}, {_moment(checkpoint.now)}: the agent reported {status}, "
            f"with {held} commitment{'s' if held != 1 else ''} open"
            + (f", next wake {_moment(report.next_wake)}" if report is not None and report.next_wake else "")
        )
        found.append(_Item(DivergenceKind.REPORT, checkpoint.wake, seq, 2, checkpoint.now, key, words))
    for call in record.calls:
        if call.first_seq <= at_seq:
            continue
        x = call.exchange
        key = (
            (x.method, x.host, x.path)
            if x.tunnelled is not None
            else (x.method, x.host, x.path, x.status, _digest(x.request_body, x.request_bytes),
                  _digest(x.response_body, x.response_bytes))
        )  # fmt: skip
        words = f"wake {call.wake}, {_moment(call.sim_time)}: {_call_words(call)}"
        found.append(_Item(DivergenceKind.CALL, call.wake, call.first_seq, 0, call.sim_time, key, words))
    found.sort(key=lambda i: (i.seq, i.rank))
    models = sorted(
        (s for s in record.spans if s.wake > after_wake and is_model_call(s)), key=lambda s: (s.wake, s.span.start)
    )
    for stored in models:
        asked = model_call(stored)
        key = (asked.model, asked.system_instructions, asked.input_messages, asked.output_messages)
        words = (
            f"wake {stored.wake}, {_moment(stored.sim_time)}: the agent asked {asked.model or 'a model'} "
            f"{_quoted(asked.input_messages or '', 100)} and was answered {_quoted(asked.output_messages or '', 100)}"
        )
        found.append(_Item(DivergenceKind.MODEL_CALL, stored.wake, stored.after_seq, 3, stored.sim_time, key, words))
    return found


def _parted(ours: list[_Item], theirs: list[_Item]) -> tuple[int, _Item | None, _Item | None] | None:
    shared = 0
    while (
        shared < len(ours)
        and shared < len(theirs)
        and ours[shared].kind is theirs[shared].kind
        and (ours[shared].key == theirs[shared].key)
    ):
        shared += 1
    if shared == len(ours) and shared == len(theirs):
        return None
    return (
        shared,
        ours[shared] if shared < len(ours) else None,
        theirs[shared] if shared < len(theirs) else None,
    )


def _differs(ours: _Item | None, theirs: _Item | None) -> str:
    if ours is None or theirs is None:
        left = theirs if ours is None else ours
        assert left is not None
        return f"the {'parent' if ours is None else 'fork'}'s record has no more {KIND_WORDS[left.kind][1]} there"
    if ours.kind is not theirs.kind:
        return f"the parent has a {KIND_WORDS[ours.kind][0]} where the fork has a {KIND_WORDS[theirs.kind][0]}"
    if ours.kind is DivergenceKind.CALL and len(ours.key) == len(theirs.key) == 6:
        if ours.key[:3] != theirs.key[:3]:
            return "a different call was made"
        if ours.key[4] != theirs.key[4]:
            return "the same call sent something different"
        return "the same call was answered differently"
    if ours.kind is DivergenceKind.MODEL_CALL:
        return "the model was asked or answered differently"
    if ours.kind is DivergenceKind.REPORT:
        return "the agent reported differently"
    return "a different change was made, or the same one at another moment"


def first_divergence(
    parent: Record, fork: Record, at_seq: int, scenario: Scenario, fork_scenario: Scenario, *, after_wake: int = 0
) -> FirstDivergence | None:
    """Where the two records part: the first thing after `at_seq` that the other record did not do alike. The
    changes, calls and reports are compared in log order, where each sits; the model calls (whose spans arrive when
    the agent exports them, not where in the log they happened) in their own order; the earlier parting of the
    two, by wake and then by log position, is the first. Reads and searches are not changes, and the run loop's
    checkpoints are compared as the agent's reports."""
    ours, theirs = _items(parent, at_seq, after_wake, scenario), _items(fork, at_seq, after_wake, fork_scenario)
    logged = _parted(
        [i for i in ours if i.kind is not DivergenceKind.MODEL_CALL],
        [i for i in theirs if i.kind is not DivergenceKind.MODEL_CALL],
    )
    modelled = _parted(
        [i for i in ours if i.kind is DivergenceKind.MODEL_CALL],
        [i for i in theirs if i.kind is DivergenceKind.MODEL_CALL],
    )

    def where(parted: tuple[int, _Item | None, _Item | None]) -> tuple[int, int]:
        return min((i.wake, i.seq) for i in parted[1:] if i is not None)

    found = [p for p in (logged, modelled) if p is not None]
    if not found:
        return None
    shared, mine, yours = min(found, key=where)
    first = min((i for i in (mine, yours) if i is not None), key=lambda i: (i.wake, i.seq, i.rank))

    def told(item: _Item | None) -> DivergentEvent | None:
        if item is None:
            return None
        return DivergentEvent(kind=item.kind, seq=item.seq, wake=item.wake, at=item.at, words=item.words)

    return FirstDivergence(
        kind=first.kind, parent=told(mine), fork=told(yours), shared=shared, differs=_differs(mine, yours)
    )


def outcomes(
    parent: RunResult,
    fork: RunResult,
    *,
    parent_record: Record,
    fork_record: Record,
    at_seq: int,
    after_wake: int,
    scenario: Scenario,
    fork_scenario: Scenario,
) -> Outcomes:
    was = {line.label: line.value for line in scorecard_lines(parent.effectiveness)}
    now = {line.label: line.value for line in scorecard_lines(fork.effectiveness)}
    labels = list(dict.fromkeys([*was, *now]))
    differing = [
        ScoreDifference(label=label, parent=was.get(label, "none"), fork=now.get(label, "none"))
        for label in labels
        if was.get(label) != now.get(label)
    ]
    gained, lost, changed = _paired(parent.findings, fork.findings)
    return Outcomes(
        parent_verdict=parent.verdict,
        fork_verdict=fork.verdict,
        verdict_changed=parent.verdict.kind is not fork.verdict.kind,
        scorecard=differing,
        findings_gained=gained,
        findings_lost=lost,
        findings_changed=changed,
        first_divergence=first_divergence(
            parent_record, fork_record, at_seq, scenario, fork_scenario, after_wake=after_wake
        ),
    )


def described(account: ForkAccount) -> list[str]:
    """The account as lines of text, for the command line."""
    lines = [
        f"forked from {account.parent_run} at seq {account.at_seq}: after wake {account.after_wake}, "
        + ("with the clock run on to " if account.ran_on else "")
        + f"{_moment(account.at)} (simulated)"
    ]
    if account.changes:
        lines.append("what it changed:")
        lines += [f"  {c}" for c in account.changes]
    else:
        lines.append("what it changed: nothing; a rerun from the same moment")
    if account.restore is not None:
        lines.append(f"its restore was {account.restore.words}")
    outcome = account.outcome
    if outcome is None:
        lines.append("against its parent: not compared until both have finished")
        return lines
    lines.append("against its parent, from the fork on:")
    if outcome.verdict_changed:
        lines.append(f"  verdict: {outcome.parent_verdict.kind.value} -> {outcome.fork_verdict.kind.value}")
    else:
        lines.append(f"  verdict: {outcome.fork_verdict.kind.value} in both")
    lines += [f"  {d.label}: {d.parent} -> {d.fork}" for d in outcome.scorecard]
    lines += [f"  gained {f.kind.value} {f.check}: {f.message}" for f in outcome.findings_gained]
    lines += [f"  lost {f.kind.value} {f.check}: {f.message}" for f in outcome.findings_lost]
    lines += [
        f"  changed {c.fork.kind.value} {c.fork.check}: {c.parent.message} -> {c.fork.message}"
        for c in outcome.findings_changed
    ]
    split = outcome.first_divergence
    if split is None:
        lines.append(
            "  the records make the same changes and calls, report alike and ask their models alike: nothing diverged"
        )
    else:
        one = KIND_WORDS[split.kind][0]
        where = (
            f"at the first {one} after the split"
            if split.shared == 0
            else f"at a {one}, after {split.shared} thing{'s' if split.shared != 1 else ''} made alike"
        )
        lines.append(f"  the records part {where}: {split.differs}")
        lines.append(f"    parent: {split.parent.words if split.parent else 'nothing more'}")
        lines.append(f"    fork:   {split.fork.words if split.fork else 'nothing more'}")
    return lines
