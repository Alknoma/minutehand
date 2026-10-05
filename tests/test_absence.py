"""The one rule placing an absence on the run's clock, which the ledger and the Slack and Microsoft providers read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minutehand.domain.absence import first_ask, placed
from minutehand.domain.world import Actor, EntityKind, EntityRef, MessageSnapshot, Operation, WorldEvent

T0 = datetime(2026, 8, 24, 9, tzinfo=UTC)


def _message(seq: int, actor: Actor, to: str, hours: float) -> WorldEvent:
    return WorldEvent(
        seq=seq,
        run_id="r",
        wake=1,
        sim_time=T0 + timedelta(hours=hours),
        wall_time=T0,
        actor=actor,
        operation=Operation.CREATE,
        entity=EntityRef(provider="mail", kind=EntityKind.MESSAGE, external_id=str(seq)),
        after=MessageSnapshot(text="hi", channel="c", recipient_emails=[to]),
    )


def test_an_absence_is_placed_from_the_start_or_from_the_first_ask_plus_its_own_offset() -> None:
    events = [_message(1, Actor.PERSON, "rosa@x", 1), _message(2, Actor.AGENT, "rosa@x", 3)]
    asked = first_ask("rosa", "rosa@x", events)

    assert asked == T0 + timedelta(hours=3)
    assert placed(from_start=T0, asked=asked, starts_after=timedelta(hours=1), lasts=timedelta(hours=2)) == (
        T0 + timedelta(hours=1),
        T0 + timedelta(hours=3),
    )
    assert placed(from_start=None, asked=asked, starts_after=timedelta(hours=1), lasts=timedelta(hours=2)) == (
        T0 + timedelta(hours=4),
        T0 + timedelta(hours=6),
    )
    assert placed(from_start=None, asked=None, starts_after=timedelta(0), lasts=timedelta(hours=2)) is None
