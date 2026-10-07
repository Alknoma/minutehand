"""An approval in the reference agent's own web app, through the real CLI: telling Owen waits on Nadia, whom
Minutehand reads the approvals of, and who decides, as herself, by her script. No provider is in the run.

Approved after a delay, the timeline holds the ask, her decision and the tell it held back, in that order. Rejected,
the agent does not send the tell; a heedless one that does fails `acted_without_approval`. Silent, the ask stays
open: the agent reminds her, or, forgetful, fails `no_follow_up`. Away, she decides when she is back."""

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
    assert "acted_without_approval" not in done.out
    assert "decisions asked of people: 1, decided: 1, left pending: 0" in done.out


def test_a_heedless_agent_that_sends_after_the_rejection_fails(rig: Rig) -> None:
    done = rig.run("scenario_rejected.yaml", env={**APPROVER, "REFERENCE_BEHAVIOUR": "heedless"}, inbox=True)

    assert done.code == 1, done.out + done.err[-3000:]
    assert "acted_without_approval: went ahead with tell-" in done.out
    assert "after Nadia Ek rejected it (reason: We already hold a room at the office that day.)" in done.out


def test_reminding_a_silent_approver_every_day_is_nagging(rig: Rig) -> None:
    """A silent person's patience is the default longest delay, 66 hours: reminded every 24, they are nagged."""
    done = rig.run("scenario_approver_silent.yaml", env=APPROVER, inbox=True)

    assert done.code == 1, done.out + done.err[-3000:]
    assert "nagged: nadia was followed up 3 times on one ask" in done.out


def test_a_silent_approver_reminded_on_their_patience_leaves_the_run_unfinished(rig: Rig) -> None:
    done = rig.run(
        "scenario_approver_silent.yaml", env={**APPROVER, "REFERENCE_APPROVAL_FOLLOW_UP_HOURS": "66"}, inbox=True
    )

    assert done.code == 3, done.out + done.err[-3000:]
    assert "left pending: 1" in done.out
    assert "no_follow_up" not in done.out and "nagged" not in done.out


def test_a_forgetful_agent_never_reminds_a_silent_approver_and_is_flagged(rig: Rig) -> None:
    done = rig.run("scenario_approver_silent.yaml", env={**APPROVER, "REFERENCE_BEHAVIOUR": "forgetful"}, inbox=True)

    assert done.code == 1, done.out + done.err[-3000:]
    assert "no_follow_up: wait on nadia expired" in done.out


def test_an_approver_away_until_wednesday_decides_when_back(rig: Rig) -> None:
    done = rig.run("scenario_approver_away.yaml", env=APPROVER, inbox=True)

    assert done.code == 0, done.out + done.err[-3000:]
    with rig.world(done.run_id) as world:
        decided = [e for e in world.events() if isinstance(e.after, InboxItemSnapshot) and e.actor is Actor.PERSON]
    assert len(decided) == 1
    assert decided[0].sim_time.isoformat() >= "2026-08-26T19:00:00+00:00"
