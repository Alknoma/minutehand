"""A fork, told: where it split from its parent, what it changed, whether its restore was proven, and how its
outcome differs from its parent's from that point on.

Every surface that shows a fork (`minutehand findings` and `runs`, the viewer, the MCP run summary) states it from
`ForkAccount`, so they cannot disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import AwareDatetime, Field

from minutehand.application.checkpoint import CHECKPOINT
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
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    GrantSnapshot,
    InteractionSnapshot,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    TicketSnapshot,
    WorldEvent,
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


class DivergentEvent(Model):
    """One change in the world on one side of a fork, in words."""

    seq: int
    wake: int
    at: AwareDatetime = Field(description="Simulated time")
    words: str


class FirstDivergence(Model):
    """The first change in the world, after the fork point, at which the two records part."""

    parent: DivergentEvent | None = Field(description="None: the parent's record has no change left there")
    fork: DivergentEvent | None = Field(description="None: the fork's record has no change left there")
    shared: int = Field(ge=0, description="Changes after the fork point both records made alike before this one")


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
            by_email = {p.email: p for p in scenario.people}
            holder = ticket_before.assignee_email if ticket_before is not None else None
            was = (by_email[holder].name if holder in by_email else holder) if holder else "nobody"
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
    names = {p.email: p.name for p in scenario.people} | {p.key: p.name for p in scenario.people}
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
        to = ", ".join(names.get(e, e) for e in after.recipient_emails) or after.channel
        what = (
            f"{who} {'sent' if event.operation is Operation.CREATE else verb} a message to {to}: {_quoted(after.text)}"
        )
    elif isinstance(after, TicketSnapshot):
        holder = f", for {names.get(after.assignee_email, after.assignee_email)}" if after.assignee_email else ""
        what = f"{who} {verb} ticket {_quoted(after.title, 80)} ({after.state.value}{holder})"
    elif isinstance(after, DocumentSnapshot):
        what = f"{who} {verb} document {_quoted(after.title, 80)}"
    elif isinstance(after, GrantSnapshot):
        what = f"{who} gave {after.to} {after.role.value} access to {_quoted(after.document, 80)}"
    elif isinstance(after, RecordSnapshot):
        what = f"{who} {verb} a {after.resource} record: {_quoted(after.text)}"
    elif isinstance(after, InteractionSnapshot):
        what = f"{names.get(after.person, after.person)} pressed {_quoted(after.label, 60)}"
    else:
        ref = event.entity
        what = f"{who} {verb} {ref.kind.value} {ref.external_id} in {ref.provider}"
    return f"wake {event.wake}, {_moment(event.sim_time)}: {what}"


def _changes_after(events: list[WorldEvent], at_seq: int) -> list[WorldEvent]:
    return [
        e
        for e in events
        if e.seq > at_seq and e.entity != CHECKPOINT and e.operation not in (Operation.READ, Operation.SEARCH)
    ]


def _same(a: WorldEvent, b: WorldEvent) -> bool:
    return (
        a.actor is b.actor
        and a.operation is b.operation
        and a.entity == b.entity
        and a.after == b.after
        and a.sim_time == b.sim_time
    )


def first_divergence(
    parent: list[WorldEvent], fork: list[WorldEvent], at_seq: int, scenario: Scenario, fork_scenario: Scenario
) -> FirstDivergence | None:
    """Where the two records part: the first change in the world after `at_seq`, in order, that the other record
    did not make at the same moment. Reads and searches are not compared; the run loop's checkpoints are not
    changes."""
    ours, theirs = _changes_after(parent, at_seq), _changes_after(fork, at_seq)
    shared = 0
    while shared < len(ours) and shared < len(theirs) and _same(ours[shared], theirs[shared]):
        shared += 1
    if shared == len(ours) and shared == len(theirs):
        return None

    def told(events: list[WorldEvent], words_in: Scenario) -> DivergentEvent | None:
        if shared >= len(events):
            return None
        e = events[shared]
        return DivergentEvent(seq=e.seq, wake=e.wake, at=e.sim_time, words=event_words(e, words_in))

    return FirstDivergence(parent=told(ours, scenario), fork=told(theirs, fork_scenario), shared=shared)


def outcomes(
    parent: RunResult,
    fork: RunResult,
    *,
    parent_events: list[WorldEvent],
    fork_events: list[WorldEvent],
    at_seq: int,
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
        first_divergence=first_divergence(parent_events, fork_events, at_seq, scenario, fork_scenario),
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
        lines.append("  the records make the same changes at the same moments: nothing diverged")
    else:
        where = (
            "at the first change in the world after the split"
            if split.shared == 0
            else f"after {split.shared} change{'s' if split.shared != 1 else ''} in the world made alike"
        )
        lines.append(f"  the records part {where}:")
        lines.append(f"    parent: {split.parent.words if split.parent else 'nothing more'}")
        lines.append(f"    fork:   {split.fork.words if split.fork else 'nothing more'}")
    return lines
