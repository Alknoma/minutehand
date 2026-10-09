"""When people answer, on real events from the real store: delays, windows of available time, absences, working
hours, follow-ups and reminders."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.moments import sooner
from minutehand.application.replier import PeopleReplier, owed_in
from minutehand.application.run_clock import RunClock
from minutehand.domain.clock import DrawnFrom
from minutehand.domain.scenario import (
    Absence,
    AbsenceTrigger,
    AfterScript,
    DelayRange,
    Person,
    Scripted,
    ScriptedReply,
    Silent,
    Window,
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
    reply = await PeopleReplier(
        scenario(tom_finishes=False, people=[person("owner", Silent()), who], seed=seed), None
    ).decide(who, asked, store.events(), clock)
    assert reply is not None
    return reply.at


RANGED = Person(
    key="sofia",
    name="Sofia",
    email="sofia@example.com",
    reply=Scripted(
        then=AfterScript.SILENT,
        delay=DelayRange(shortest=timedelta(hours=6), longest=timedelta(hours=66)),
        replies=[ScriptedReply(to_ask=1, verbatim="yes")],
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
    replier = PeopleReplier(scenario(tom_finishes=False, people=[person("owner", Silent()), sofia]), None)
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
    assert await PeopleReplier(scenario(), None).decide(dania, asked, store.events(), clock) is None


# -- reply windows: drawn in the person's available time ----------------------------------------------------------

WINDOW = Window(min=timedelta(hours=2), max=timedelta(hours=6))
WORKDAYS = WorkingHours(timezone="UTC", opens=time(9), closes=time(17))


def windowed(**more: object) -> Person:
    return Person.model_validate(
        {
            "key": "sofia",
            "name": "Sofia",
            "email": "sofia@example.com",
            "reply_within": WINDOW.model_dump(),
            "reply": Scripted(then=AfterScript.SILENT, replies=[ScriptedReply(to_ask=1, verbatim="yes")]).model_dump(),
            **more,
        }
    )


async def test_a_window_draws_the_same_moment_for_the_same_seed_and_others_within_it(tmp_path: Path) -> None:
    who = windowed()
    first = await landing(tmp_path / "a", who, seed=5)
    assert first == await landing(tmp_path / "b", who, seed=5)
    drawn = {await landing(tmp_path / f"s{seed}", who, seed=seed) for seed in range(12)}
    assert len(drawn) > 6, "different seeds draw different moments"
    assert all(T0 + WINDOW.min <= at <= T0 + WINDOW.max for at in drawn)


async def test_a_window_is_counted_in_available_time_across_a_weekend_and_an_absence(tmp_path: Path) -> None:
    exact = Window(min=timedelta(hours=2), max=timedelta(hours=2))
    hours = windowed(reply_within=exact.model_dump(), working_hours=WORKDAYS.model_dump())
    # Asked at 16:00 on a Friday: one hour of Friday is available, the second is Monday's first.
    friday_four = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)
    assert await landing(tmp_path / "a", hours, at=friday_four) == datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    # Away all of Monday: Tuesday's first hour carries it.
    monday = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
    away = windowed(
        reply_within=exact.model_dump(),
        working_hours=WORKDAYS.model_dump(),
        absences=[Absence(starts_after=monday - T0, lasts=timedelta(days=1)).model_dump()],
    )
    assert await landing(tmp_path / "b", away, at=friday_four) == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


async def test_every_draw_lands_inside_working_hours_and_spreads_over_the_day(tmp_path: Path) -> None:
    hours = windowed(working_hours=WORKDAYS.model_dump())
    friday_four = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)
    drawn = [await landing(tmp_path / f"s{seed}", hours, seed=seed, at=friday_four) for seed in range(20)]
    assert all(at.weekday() < 5 and time(9) <= at.time() < time(17) for at in drawn)
    # Two to six hours of working time after 16:00 on a Friday: from 10:00 to 14:00 on Monday, never piled at 09:00.
    assert all(
        datetime(2026, 8, 31, 10, 0, tzinfo=UTC) <= at <= datetime(2026, 8, 31, 14, 0, tzinfo=UTC) for at in drawn
    )
    assert len({at.hour for at in drawn}) >= 3


async def test_a_steps_own_window_wins_over_the_persons(tmp_path: Path) -> None:
    soon = Window(min=timedelta(minutes=5), max=timedelta(minutes=5))
    who = windowed(
        reply=Scripted(
            then=AfterScript.SILENT, replies=[ScriptedReply(to_ask=1, verbatim="yes", within=soon)]
        ).model_dump()
    )
    assert await landing(tmp_path, who) == T0 + timedelta(minutes=5)


def test_a_reminder_draws_again_and_only_ever_moves_the_answer_sooner(tmp_path: Path) -> None:
    store, _ = world(tmp_path)
    sooner_window = Window(min=timedelta(hours=1), max=timedelta(hours=1))
    who = windowed(reminded={"sooner_within": sooner_window.model_dump()})
    scn = scenario(tom_finishes=False, people=[person("owner", Silent()), who])
    follow = ask(store, who.email, "m2")
    owed_late = follow.sim_time + timedelta(hours=5)
    moved = sooner(scn, who, follow, store.events(), owed_late)
    assert moved is not None and moved.lands_at == follow.sim_time + timedelta(hours=1)
    assert moved.source is DrawnFrom.REMINDED
    assert sooner(scn, who, follow, store.events(), follow.sim_time + timedelta(minutes=30)) is None, "never later"
    assert sooner(scn, windowed(), follow, store.events(), owed_late) is None, "no reminder declared: nothing moves"


async def test_a_follow_up_before_the_answer_lands_is_no_new_ask(tmp_path: Path) -> None:
    store, clock = world(tmp_path)
    sofia = person("sofia", scripted("first", "second", hours=1))
    replier = PeopleReplier(scenario(tom_finishes=False, people=[person("owner", Silent()), sofia]), None)
    first = ask(store, sofia.email, "m1")
    reply = await replier.decide(sofia, first, store.events(), clock, store)
    assert reply is not None
    store.remember(reply)
    chase = ask(store, sofia.email, "m2")  # the same direct conversation, an hour before her answer lands
    assert replier.plan(sofia, chase, store.events(), owed_in(store)) is None
    # Another thread of the same channel is a conversation of its own, and its ask draws its own moment.
    elsewhere = store.apply(
        Change(
            entity=EntityRef(provider="testchat", kind=EntityKind.MESSAGE, external_id="m3"),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body="{}",
            after=MessageSnapshot(text="?", channel="dm", recipient_emails=[sofia.email], thread_of="t9"),
        )
    )
    planned = replier.plan(sofia, elsewhere, store.events(), owed_in(store))
    assert planned is not None and planned.nth == 2 and planned.step is not None and planned.step.verbatim == "second"
