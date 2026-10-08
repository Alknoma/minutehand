"""A long run, written straight into a state directory: a year and more of simulated time with tens of thousands of
marks, for the viewer's tests of how its API and page cope with size.

    uv run python -m tests.web.long_run <state> [--days N]

Every day of the run the agent wakes once at nine: it reads and writes its memory, messages some of the people
(each send an HTTP call to Slack that made the event), reads a channel, stores an item for a declared host every few
days, and makes a model call whose span the run received. A person answers some messages hours later, their words
written by a model whose call is kept with tokens; the run loop's table gains the agent's next wake and each reply,
and every so often a dispatch rule holds a wake back. The run is checked by the real checks and kept as finished, so
every view of the viewer has something to draw at every zoom.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from minutehand import session
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import Checkpoint, Remembered, write_checkpoint
from minutehand.application.memory import digest
from minutehand.application.run_clock import RunClock
from minutehand.checks.runner import evaluate_run
from minutehand.domain.assessments import Rule
from minutehand.domain.checks import WakeRecord
from minutehand.domain.clock import Drawn, DrawnFrom, Due, DueClosed, DueEntry, DueKind, DueSource
from minutehand.domain.conversation import PersonCall, Provenance, Wrote
from minutehand.domain.people import PersonReply, Writing
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import DelayRange, DispatchFault, Person, PersonAsked, Scenario, Silent
from minutehand.domain.telemetry import Attribute, IntValue, ReceivedSpan, SpanSource, StringValue
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    MemorySnapshot,
    MessageSnapshot,
    Operation,
    StoredSnapshot,
)
from minutehand.session import AGENT, RECORD, RESULT, SCENARIO, WORLD

T0 = datetime(2025, 1, 6, 9, 0, tzinfo=UTC)  # a Monday
RUN_ID = "longyear0001"
PEOPLE = [
    Person(key="olive", name="Olive Owner", email="olive@example.com", reply=Silent()),
    *(
        Person(key=k, name=n, email=f"{k}@example.com", reply=Silent())
        for k, n in [
            ("rosa", "Rosa Lind"),
            ("dani", "Dani Park"),
            ("ines", "Ines Moreau"),
            ("kofi", "Kofi Mensah"),
            ("lena", "Lena Novak"),
            ("tariq", "Tariq Aziz"),
        ]
    ),
]
WORDS = [
    "venue",
    "budget",
    "catering",
    "schedule",
    "invoice",
    "contract",
    "travel",
    "agenda",
    "badge",
    "speaker",
    "room",
    "seating",
]


def scenario(days: int) -> Scenario:
    return Scenario(
        name="year_of_follow_ups",
        goal="Keep the year's events on track with everyone involved.",
        owner="olive",
        starts_at=T0,
        deadline_after=timedelta(days=days),
        people=PEOPLE,
        expect=[PersonAsked(person="rosa")],
        assess=[
            Rule.model_validate(
                {
                    "id": "comes_back_within_a_day",
                    "each": "ask",
                    "when": {"answered": True},
                    "count": {"touches": {}, "since": "answer", "until": "answer+P1D"},
                    "at_least": 1,
                    "message": "{person.key} answered and the agent did not come back to it within a day",
                }
            ),
            Rule.model_validate(
                {
                    "id": "no_more_than_three_follow_ups",
                    "each": "ask",
                    "count": {"follow_ups": {}},
                    "at_most": 3,
                }
            ),
        ],
    )


def _span(rng: random.Random, start: datetime, n: int) -> list[ReceivedSpan]:
    trace = f"{rng.getrandbits(128):032x}"
    root = f"{rng.getrandbits(64):016x}"
    call = f"{rng.getrandbits(64):016x}"
    tokens_in, tokens_out = rng.randint(400, 4000), rng.randint(40, 600)
    return [
        ReceivedSpan(trace_id=trace, span_id=root, name="wake", start=start, end=start + timedelta(seconds=3)),
        ReceivedSpan(
            trace_id=trace,
            span_id=call,
            parent_span_id=root,
            name="chat m-large",
            start=start + timedelta(milliseconds=200),
            end=start + timedelta(milliseconds=200 + rng.randint(300, 2500)),
            attributes=[
                Attribute(key="gen_ai.operation.name", value=StringValue(value="chat")),
                Attribute(key="gen_ai.request.model", value=StringValue(value="m-large" if n % 5 else "m-small")),
                Attribute(
                    key="gen_ai.input.messages",
                    value=StringValue(
                        value=json.dumps([{"role": "user", "parts": [{"type": "text", "content": "x"}]}])
                    ),
                ),
                Attribute(key="gen_ai.usage.input_tokens", value=IntValue(value=tokens_in)),
                Attribute(key="gen_ai.usage.output_tokens", value=IntValue(value=tokens_out)),
            ],
        ),
    ]


def write(state: Path, *, days: int = 400, seed: int = 7) -> str:
    """Write the run under `state` and answer its id."""
    rng = random.Random(seed)
    played = scenario(days)
    directory = session.run_dir(state, RUN_ID)
    directory.mkdir(parents=True)
    (directory / SCENARIO).write_text(played.model_dump_json(), encoding="utf-8")
    clock = RunClock(T0)
    store = SqliteStore(directory / WORLD, RUN_ID, clock)
    began = time.monotonic()
    empty = Checkpoint(wake=0, now=T0, replies=0, pending=[], agent=Remembered(report=None, memory=digest({})))
    write_checkpoint(store, empty)
    wakes: list[WakeRecord] = []
    due_n = 0
    landing: list[tuple[datetime, Person, EntityRef]] = []
    for day in range(days):
        now = T0 + timedelta(days=day)
        clock.jump(now)
        wake = clock.begin_wake()
        changes: list[Change] = []
        for i in range(60):
            key = f"jobs/{rng.choice(WORDS)}-{rng.randint(1, 40)}"
            write_it = i % 3 == 0
            changes.append(
                Change(
                    entity=EntityRef(provider="memory", kind=EntityKind.MEMORY, external_id=f"default/{key}"),
                    operation=Operation.UPDATE if write_it else Operation.READ,
                    actor=Actor.AGENT,
                    body=json.dumps({"day": day, "n": i}) if write_it else None,
                    after=MemorySnapshot(
                        collection="default",
                        key=key,
                        value=json.dumps({"day": day, "n": i}, sort_keys=True) if write_it else None,
                    ),
                )
            )
        store.apply_all(changes)
        sent = 0
        for person in rng.sample(PEOPLE[1:], k=rng.randint(2, 5)):
            ts = f"{int(now.timestamp())}.{day:03d}{sent:03d}"
            text = f"Hi {person.name.split()[0]}, any news on the {rng.choice(WORDS)} for event {day // 7}?"
            ref = EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=ts)
            [event] = store.apply_all(
                [
                    Change(
                        entity=ref,
                        operation=Operation.CREATE,
                        actor=Actor.AGENT,
                        body=json.dumps({"ts": ts, "text": text}),
                        after=MessageSnapshot(text=text, channel=f"D{person.key}", recipient_emails=[person.email]),
                    )
                ]
            )
            store.attach(
                Exchange(
                    method="POST",
                    host="slack.com",
                    path="/api/chat.postMessage",
                    status=200,
                    request_body=json.dumps({"channel": f"D{person.key}", "text": text}),
                    response_body=json.dumps({"ok": True, "ts": ts}),
                ),
                first_seq=event.seq,
                last_seq=event.seq,
                provider="slack",
            )
            sent += 1
            if rng.random() < 0.35:
                landing.append((now + timedelta(hours=rng.randint(2, 40)), person, ref))
        for _ in range(8):
            store.attach(
                Exchange(
                    method="GET",
                    host="slack.com",
                    path=f"/api/conversations.history?channel=C{rng.randint(1, 9)}",
                    status=200 if rng.random() > 0.02 else 429,
                    response_body=json.dumps({"ok": True, "messages": []}),
                ),
                first_seq=store.head() + 1,
                last_seq=store.head(),
                provider="slack",
            )
        if day % 3 == 0:
            item = json.dumps({"id": day, "name": f"contact {day}", "stage": rng.choice(WORDS)})
            store.apply(
                Change(
                    entity=EntityRef(provider="crm", kind=EntityKind.STORED, external_id=f"contacts/{day % 50}"),
                    operation=Operation.CREATE if day < 150 else Operation.UPDATE,
                    actor=Actor.AGENT,
                    body=item,
                    after=StoredSnapshot(
                        host="api.crm.example", collection="contacts", path="/v1/contacts", id=str(day % 50), item=item
                    ),
                )
            )
        store.receive(_span(rng, datetime.now(UTC), day), source=SpanSource.RECEIVED)  # clock-lint: exempt tests
        fault = DispatchFault.LATE if day % 37 == 5 else None
        nxt = now + timedelta(days=1)
        entry = DueEntry(
            due=Due(at=nxt, kind=DueKind.AGENT_WAKE, ref=f"wake-{day + 1}"),
            source=DueSource.REPORTED,
            entered_at=now,
            entered_wake=wake,
            closed=DueClosed.DELAYED if fault is not None else DueClosed.FIRED,
            closed_at=nxt,
            closed_wake=wake + 1,
            fault=fault,
        )
        store.apply(
            Change(
                entity=EntityRef(provider="minutehand", kind=EntityKind.DUE, external_id=f"due-{due_n}"),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=entry.model_dump_json(),
            )
        )
        due_n += 1
        for at, person, ref in sorted((x for x in landing if x[0] < nxt), key=lambda x: x[0]):
            landing.remove((at, person, ref))
            clock.jump(at)
            text = f"Done: the {rng.choice(WORDS)} is sorted."
            reply = PersonReply(
                person=person.key,
                in_reply_to=ref,
                text=text,
                at=at,
                written_by=Provenance(model="m-people", prompt_version="1"),
                writing=Writing.SCRIPT,
                drawn=Drawn(
                    source=DrawnFrom.DELAY,
                    seed=seed,
                    asked_at=now,
                    delay=DelayRange(shortest=timedelta(hours=2), longest=timedelta(hours=40)),
                    offset=at - now,
                    lands_at=at,
                ),
            )
            store.remember(reply)
            store.record_person_call(
                PersonCall(
                    key=f"{rng.getrandbits(256):064x}",
                    person=person.key,
                    wrote=Wrote.REPLY,
                    asked=ref,
                    model="m-people",
                    prompt_version="1",
                    input_tokens=rng.randint(300, 900),
                    output_tokens=rng.randint(10, 80),
                    answer=json.dumps({"text": text}),
                    sim_time=at,
                    wake=wake,
                )
            )
            store.apply(
                Change(
                    entity=EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=f"{ref.external_id}.r"),
                    operation=Operation.CREATE,
                    actor=Actor.PERSON,
                    body=json.dumps({"text": text}),
                    after=MessageSnapshot(text=text, channel=f"D{person.key}", thread_of=ref.external_id),
                )
            )
            due = DueEntry(
                due=Due(at=at, kind=DueKind.PERSON_REPLY, ref=f"reply-{person.key}-{day}"),
                source=DueSource.REPLY,
                entered_at=now,
                entered_wake=wake,
                closed=DueClosed.FIRED,
                closed_at=at,
                closed_wake=wake,
                drawn=reply.drawn,
            )
            store.apply(
                Change(
                    entity=EntityRef(provider="minutehand", kind=EntityKind.DUE, external_id=f"due-{due_n}"),
                    operation=Operation.CREATE,
                    actor=Actor.SCENARIO,
                    body=due.model_dump_json(),
                )
            )
            due_n += 1
        clock.jump(max(clock.now(), now))
        write_checkpoint(store, empty.model_copy(update={"wake": wake, "now": clock.now()}))
        wakes.append(WakeRecord(index=wake, sim_time=now, world_changes=sent + 8, commitments_changed=False))
    events = store.events()
    result = evaluate_run(played, events, wakes, store.replies(), stop=StopReason.DEADLINE_PASSED)
    record = RunRecord(
        run_id=RUN_ID,
        scenario=played.name,
        seed=seed,
        started_at=T0,
        ended_at=T0 + timedelta(days=days),
        wall_seconds=time.monotonic() - began,
        stop=StopReason.DEADLINE_PASSED,
        providers=["slack"],
        wakes=wakes,
    )
    store.close()
    (directory / RECORD).write_text(record.model_dump_json(), encoding="utf-8")
    (directory / RESULT).write_text(result.model_dump_json(), encoding="utf-8")
    assert not (directory / AGENT).exists()
    return RUN_ID


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("--days", type=int, default=400)
    args = parser.parse_args()
    began = time.monotonic()
    run_id = write(args.state, days=args.days)
    print(f"wrote run {run_id} under {args.state} in {time.monotonic() - began:.1f} s")


if __name__ == "__main__":
    main()
