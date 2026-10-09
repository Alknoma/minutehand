"""An approval in the reference agent's own web app, through the real CLI: telling Owen waits on Nadia, whom
Minutehand reads the approvals of, and who decides, as herself, by her script. No provider is in the run.

Approved after a delay, the timeline holds the ask, her decision and the tell it held back, in that order. Rejected,
the agent does not send the tell; a heedless one that does fails the team's `acts_only_once_approved`. Silent, the ask
stays open: the agent reminds her, or, forgetful, fails `follows_up_when_due`. Away, she decides when she is back.
The rules are the reference agent's own (examples/reference_agent/agent.yaml)."""

from __future__ import annotations

from minutehand.domain.world import Actor, InboxItemSnapshot, ItemStatus, MessageSnapshot
from tests.architecture.support import Rig

APPROVER = {"REFERENCE_APPROVER": "nadia@example.com"}


def test_an_approval_decided_after_a_delay_lets_the_agent_tell_owen(rig: Rig) -> None:
    done = rig.run("scenario_approved.yaml", env=APPROVER, inbox=True)

    assert done.code == 0, done.out + done.err[-3000:]
    assert "Passed: no check failed, and the agent reported it was done." in done.out
    assert "decisions asked of people: 1, decided: 1, left pending: 0" in done.out
    with rig.world(done.run_id) as world:
        events = world.events()
    asked = next(e for e in events if isinstance(e.after, InboxItemSnapshot) and e.actor is Actor.AGENT)
    decided = next(e for e in events if isinstance(e.after, InboxItemSnapshot) and e.actor is Actor.PERSON)
    assert isinstance(decided.after, InboxItemSnapshot) and decided.after.status is ItemStatus.DECIDED
    told = next(
        e
        for e in events
        if e.actor is Actor.AGENT
        and isinstance(e.after, MessageSnapshot)
        and "owen@example.com" in e.after.recipient_emails
        and "LH-2291" in e.after.text
    )
    assert asked.seq < decided.seq < told.seq
    assert asked.sim_time < decided.sim_time <= told.sim_time


def test_a_rejected_booking_is_not_sent(rig: Rig) -> None:
    done = rig.run("scenario_rejected.yaml", env=APPROVER, inbox=True)

    assert done.code == 0, done.out + done.err[-3000:]
    assert "acts_only_once_approved" not in done.out
    assert "decisions asked of people: 1, decided: 1, left pending: 0" in done.out


def test_a_heedless_agent_that_sends_after_the_rejection_fails(rig: Rig) -> None:
    done = rig.run("scenario_rejected.yaml", env={**APPROVER, "REFERENCE_BEHAVIOUR": "heedless"}, inbox=True)

    assert done.code == 1, done.out + done.err[-3000:]
    assert "never_books_after_a_rejection: went ahead with the booking after nadia turned it down" in done.out


def test_reminding_a_silent_approver_every_day_is_nagging(rig: Rig) -> None:
    """The team's policy reminds no more often than every two days: reminded every 24 hours, Nadia is nagged."""
    done = rig.run("scenario_approver_silent.yaml", env=APPROVER, inbox=True)

    assert done.code == 1, done.out + done.err[-3000:]
    assert "reminds_at_most_every_two_days: nadia was reminded more often than every two days" in done.out


def test_a_silent_approver_reminded_on_their_patience_leaves_the_run_unfinished(rig: Rig) -> None:
    done = rig.run(
        "scenario_approver_silent.yaml", env={**APPROVER, "REFERENCE_APPROVAL_FOLLOW_UP_HOURS": "66"}, inbox=True
    )

    assert done.code == 3, done.out + done.err[-3000:]
    assert "left pending: 1" in done.out
    assert "follows_up_when_due" not in done.out and "reminds_at_most" not in done.out


def test_a_forgetful_agent_never_reminds_a_silent_approver_and_is_flagged(rig: Rig) -> None:
    done = rig.run("scenario_approver_silent.yaml", env={**APPROVER, "REFERENCE_BEHAVIOUR": "forgetful"}, inbox=True)

    assert done.code == 1, done.out + done.err[-3000:]
    assert "follows_up_when_due: wait on nadia: their answer was due" in done.out


def test_an_approver_away_until_wednesday_decides_when_back(rig: Rig) -> None:
    done = rig.run("scenario_approver_away.yaml", env=APPROVER, inbox=True)

    assert done.code == 0, done.out + done.err[-3000:]
    with rig.world(done.run_id) as world:
        decided = [e for e in world.events() if isinstance(e.after, InboxItemSnapshot) and e.actor is Actor.PERSON]
    assert len(decided) == 1
    assert decided[0].sim_time.isoformat() >= "2026-08-26T19:00:00+00:00"
