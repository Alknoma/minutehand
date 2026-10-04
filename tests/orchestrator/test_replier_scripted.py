"""The scripted replier on real events from the real store."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import (
    Absence,
    AbsenceTrigger,
    DelayRange,
    Person,
    Scripted,
    ScriptedReply,
    Silent,
    WorkingHours,
)
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation, WorldEvent
from tests.orchestrator.rig import T0, person, scenario, scripted

FRIDAY_1630 = datetime(2026, 8, 28, 16, 30, tzinfo=UTC)


def ask(store: SqliteStore, email: str, external_id: str) -> WorldEvent:
    return store.apply(
        Change(
            entity=EntityRef(provider="testchat", kind=EntityKind.MESSAGE, external_id=external_id),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body="{}",
            after=MessageSnapshot(text="?", channel="dm", recipient_emails=[email]),
        )
    )


def world(tmp_path: Path, at: datetime = T0) -> tuple[SqliteStore, RunClock]:
    clock = RunClock(at)
    tmp_path.mkdir(parents=True, exist_ok=True)
    return SqliteStore(tmp_path / "world.db", "r", clock), clock


async def landing(tmp_path: Path, who: Person, *, seed: int = 17, at: datetime = T0, message: str = "m1") -> datetime:
    store, clock = world(tmp_path, at)
    asked = ask(store, who.email, message)
    reply = await ScriptedReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), who], seed=seed)).decide(
        who, asked, store.events(), clock
    )
    assert reply is not None
    return reply.at


RANGED = Person(
    key="sofia",
    name="Sofia",
    email="sofia@example.com",
    reply=Scripted(
        delay=DelayRange(shortest=timedelta(hours=6), longest=timedelta(hours=66)),
        replies=[ScriptedReply(to_ask=1, text="yes")],
    ),
)


async def test_the_same_seed_gives_the_same_delay_and_another_seed_a_different_one(tmp_path: Path) -> None:
    first = await landing(tmp_path / "a", RANGED, seed=17)
    again = await landing(tmp_path / "b", RANGED, seed=17)
    other = await landing(tmp_path / "c", RANGED, seed=18)
    assert first == again
    assert other != first
    assert T0 + timedelta(hours=6) <= first <= T0 + timedelta(hours=66)


async def test_the_delay_depends_on_which_message_was_asked(tmp_path: Path) -> None:
    assert await landing(tmp_path / "a", RANGED, message="m1") != await landing(tmp_path / "b", RANGED, message="m2")


async def test_an_absence_from_the_start_pushes_the_reply_to_its_end(tmp_path: Path) -> None:
    away = person("sofia", scripted("yes", hours=1)).model_copy(
        update={"absences": [Absence(starts_after=timedelta(0), lasts=timedelta(days=5))]}
    )
    assert await landing(tmp_path, away) == T0 + timedelta(days=5)


async def test_an_absence_on_first_ask_starts_when_the_person_is_first_asked(tmp_path: Path) -> None:
    away = person("sofia", scripted("yes", hours=1)).model_copy(
        update={
            "absences": [
                Absence(
                    trigger=AbsenceTrigger.ON_FIRST_ASK, starts_after=timedelta(minutes=30), lasts=timedelta(days=2)
                )
            ]
        }
    )
    later = T0 + timedelta(days=1)
    assert await landing(tmp_path, away, at=later) == later + timedelta(minutes=30) + timedelta(days=2)


async def test_a_reply_outside_working_hours_lands_when_they_next_open(tmp_path: Path) -> None:
    hours = person("sofia", scripted("yes", hours=1)).model_copy(update={"working_hours": WorkingHours()})
    assert await landing(tmp_path, hours, at=FRIDAY_1630) == datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


async def test_working_hours_are_read_in_the_persons_own_timezone(tmp_path: Path) -> None:
    hours = person("sofia", scripted("yes", hours=1)).model_copy(
        update={"working_hours": WorkingHours(timezone="America/New_York", opens=time(9), closes=time(17))}
    )
    # 10:00 UTC on a Monday is 06:00 in New York, so the 11:00 UTC landing waits for 09:00 there (13:00 UTC).
    assert await landing(tmp_path, hours) == datetime(2026, 8, 24, 13, 0, tzinfo=UTC)


async def test_the_nth_ask_gets_the_nth_scripted_reply_and_an_unscripted_ask_gets_none(tmp_path: Path) -> None:
    store, clock = world(tmp_path)
    sofia = person("sofia", scripted("first", "second", hours=1))
    replier = ScriptedReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), sofia]))
    texts = []
    for n in (1, 2, 3):
        asked = ask(store, sofia.email, f"m{n}")
        reply = await replier.decide(sofia, asked, store.events(), clock)
        texts.append(reply.text if reply else None)
    assert texts == ["first", "second", None]


async def test_a_silent_person_returns_no_reply(tmp_path: Path) -> None:
    store, clock = world(tmp_path)
    dania = person("dania", Silent())
    asked = ask(store, dania.email, "m1")
    assert await ScriptedReplier(scenario()).decide(dania, asked, store.events(), clock) is None
