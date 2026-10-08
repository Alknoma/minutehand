"""A run written by hand through the real store, with every kind of row the read model holds, and a fork of it.

The agent asks Sofia for the partner pricing and Dania for an agreement (at 06:00 in New York, before her day), keeps
its asks in its memory, stores a contact, notifies a declared hook, is refused by a host nobody declared, follows up on
Sofia a day later, hears her answer, tells the owner and reports done; then writes its memory once more after the
checkpoint. The fork, taken after the first wake, follows up in other words. Every value a test asserts is here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import Checkpoint, Remembered, write_checkpoint
from minutehand.application.memory import NEXT_WAKE, ref
from minutehand.application.run_clock import RunClock
from minutehand.checks.runner import evaluate_run
from minutehand.domain.agent import AgentReport, AgentStatus, WakeReason
from minutehand.domain.checks import WakeRecord
from minutehand.domain.clock import Drawn, DrawnFrom, Due, DueClosed, DueEntry, DueKind, DueSource
from minutehand.domain.conversation import PersonCall, Provenance, Wrote
from minutehand.domain.people import PersonReply, Writing
from minutehand.domain.prices import Price, Prices
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import (
    AfterScript,
    DelayRange,
    Person,
    PersonAsked,
    Scenario,
    Scripted,
    ScriptedReply,
    Silent,
    WorkingHours,
)
from minutehand.domain.telemetry import Attribute, IntValue, ReceivedSpan, SpanSource, StringValue
from minutehand.domain.world import (
    Actor,
    AnsweredBy,
    Body,
    BodyKept,
    Captured,
    CaptureMode,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    MemorySnapshot,
    MessageSnapshot,
    NextWakeSnapshot,
    Operation,
    StoredSnapshot,
)
from minutehand.session import RECORD, RESULT, RUNS, SCENARIO, WORLD
from tests.support.rules import rules

T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)  # a Monday
OWNER = "owner@example.com"
SOFIA = "sofia@example.com"
DANIA = "dania@example.com"
QUESTION = "Could you confirm the partner pricing for the new contract, please?"
TO_DANIA = "Dania, could you send the signed agreement when you have a moment?"
FOLLOW_UP = "Following up on the partner pricing: could you confirm it today?"
FORK_FOLLOW_UP = "Sofia, any news on the partner pricing? It is holding up the contract."
ANSWER = "Yes, 40k a year."
TOLD = "Sofia confirmed the partner pricing: 40k a year."
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN = "00f067aa0ba902b7"
HISTORY = json.dumps(
    {"ok": True, "messages": [{"ts": f"1724400000.{i:06d}", "text": f"earlier message number {i}"} for i in range(40)]}
)
"""The answer to the agent's first read: over 512 bytes, so the store keeps it compressed in `content`."""

POLICY = """
- id: follows_up_when_due
  each: ask
  where: {person_not: [owner]}
  when: {open_at: due+PT1H}
  count: {follow_ups: {}, since: ask+PT1H, until: due+PT1H}
  at_least: 1
  message: "wait on {person.key} expired and the agent had not followed up an hour later"
  pattern: expiry_on_every_wait
"""

PRICES = Prices(prices=[Price(model="gpt-test", input_per_million=2.0, output_per_million=8.0)])


def scenario() -> Scenario:
    return Scenario(
        name="partner_pricing",
        goal="The partner pricing is confirmed with Sofia.",
        owner="owner",
        starts_at=T0,
        deadline_after=timedelta(days=14),
        people=[
            Person(key="owner", name="Olive Owner", email=OWNER, reply=Scripted(then=AfterScript.SILENT)),
            Person(
                key="sofia",
                name="Sofia Romano",
                email=SOFIA,
                working_hours=WorkingHours(timezone="Europe/Rome", opens=time(9), closes=time(18)),
                reply=Scripted(
                    then=AfterScript.SILENT,
                    delay=DelayRange(shortest=timedelta(hours=20), longest=timedelta(hours=20)),
                    replies=[ScriptedReply(to_ask=1, facts=["40k a year"])],
                ),
            ),
            Person(
                key="dania",
                name="Dania Kovac",
                email=DANIA,
                working_hours=WorkingHours(timezone="America/New_York"),
                reply=Silent(),
            ),
        ],
        expect=[PersonAsked(person="sofia"), PersonAsked(person="owner", mentions=["confirmed"])],
        assess=rules(POLICY),
    )


@dataclass(frozen=True)
class Fixture:
    state: Path
    run_id: str
    fork_id: str
    fork_at: int
    asked: int
    """The seq of the agent's question to Sofia."""
    to_dania: int
    follow_up: int
    answered: int
    """The seq of Sofia's answer landing."""
    told: int
    late_write: int
    history_read: int


class _Writer:
    def __init__(self, store: SqliteStore, clock: RunClock) -> None:
        self.store, self.clock = store, clock
        self.calls = 0

    def change(self, change: Change) -> int:
        return self.store.apply(change).seq

    def memory(self, key: str, operation: Operation, value: str | None = None, actor: Actor = Actor.AGENT) -> int:
        return self.change(
            Change(
                entity=ref("default", key),
                operation=operation,
                actor=actor,
                body=value if operation in (Operation.CREATE, Operation.UPDATE) else None,
                after=MemorySnapshot(collection="default", key=key, value=value),
            )
        )

    def message(
        self,
        channel: str,
        ts: str,
        text: str,
        to: list[str],
        *,
        actor: Actor = Actor.AGENT,
        thread: str | None = None,
        traceparent: str | None = None,
    ) -> int:
        entity = EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=f"{channel}/{ts}")
        seq = self.change(
            Change(
                entity=entity,
                operation=Operation.CREATE,
                actor=actor,
                body=json.dumps({"ts": ts, "text": text}),
                parent=channel,
                after=MessageSnapshot(text=text, channel=channel, recipient_emails=to, thread_of=thread),
            )
        )
        if actor is Actor.AGENT:
            self.store.attach(
                Exchange(
                    method="POST",
                    host="slack.com",
                    path="/api/chat.postMessage",
                    status=200,
                    request_body=json.dumps({"channel": channel, "text": text}),
                    response_body=json.dumps({"ok": True, "ts": ts}),
                    traceparent=traceparent,
                ),
                first_seq=seq,
                last_seq=seq,
                provider="slack",
            )
        return seq

    def model_call(self, said: str, tokens: tuple[int, int], span_id: str) -> None:
        """A model call recorded on the wire, ended before the message it wrote is written."""
        began = datetime.now(UTC)
        ended = began + timedelta(microseconds=1)
        self.store.receive(
            [
                ReceivedSpan(
                    trace_id=TRACE,
                    span_id=span_id,
                    name="chat gpt-test",
                    start=began,
                    end=ended,
                    attributes=[
                        Attribute(key="gen_ai.operation.name", value=StringValue(value="chat")),
                        Attribute(key="gen_ai.system", value=StringValue(value="openai")),
                        Attribute(key="gen_ai.request.model", value=StringValue(value="gpt-test")),
                        Attribute(key="gen_ai.usage.input_tokens", value=IntValue(value=tokens[0])),
                        Attribute(key="gen_ai.usage.output_tokens", value=IntValue(value=tokens[1])),
                        Attribute(
                            key="gen_ai.output.messages",
                            value=StringValue(value=json.dumps([{"role": "assistant", "content": said}])),
                        ),
                    ],
                )
            ],
            source=SpanSource.WIRE,
        )
        while datetime.now(UTC) <= ended:
            pass

    def due(self, number: int, entry: DueEntry) -> None:
        self.change(
            Change(
                entity=EntityRef(provider="minutehand", kind=EntityKind.DUE, external_id=f"due:{number}"),
                operation=Operation.CREATE if entry.closed is None else Operation.UPDATE,
                actor=Actor.SCENARIO,
                body=entry.model_dump_json(),
            )
        )

    def checkpoint(self, replies: int, status: AgentStatus, next_wake: datetime | None) -> int:
        return write_checkpoint(
            self.store,
            Checkpoint(
                wake=self.clock.wake(),
                now=self.clock.now(),
                replies=replies,
                pending=[],
                agent=Remembered(report=AgentReport(status=status, next_wake=next_wake), memory="0" * 64),
            ),
        )


def _wake(index: int, at: datetime, reason: WakeReason, changes: int, reads: int, writes: int) -> WakeRecord:
    return WakeRecord(
        index=index,
        sim_time=at,
        reason=reason.value,
        world_changes=changes,
        commitments_changed=False,
        memory_reads=reads,
        memory_writes=writes,
    )


def _keep(
    state: Path, run_id: str, played: Scenario, record: RunRecord, store: SqliteStore, wakes: list[WakeRecord]
) -> None:
    directory = state / RUNS / run_id
    directory.mkdir(parents=True, exist_ok=True)
    result = evaluate_run(played, store.events(), wakes, store.replies(), stop=record.stop, ended=record.ended_at)
    (directory / SCENARIO).write_text(played.model_dump_json(), encoding="utf-8")
    (directory / RECORD).write_text(record.model_dump_json(), encoding="utf-8")
    (directory / RESULT).write_text(result.model_dump_json(), encoding="utf-8")


def build_fixture(state: Path) -> Fixture:
    played = scenario()
    run_id, fork_id = "run000000001", "fork00000001"
    directory = state / RUNS / run_id
    directory.mkdir(parents=True)
    clock = RunClock(T0)
    store = SqliteStore(directory / WORLD, run_id, clock)
    w = _Writer(store, clock)
    w.memory("team", Operation.CREATE, '{"name":"core"}', actor=Actor.SCENARIO)
    w.checkpoint(0, AgentStatus.WORKING, None)

    # wake 1, at the start: read, ask Sofia and Dania, remember, store, notify, be refused, plan the next wake
    clock.begin_wake()
    w.memory("asks/sofia", Operation.READ)
    history_read = w.change(
        Change(
            entity=EntityRef(provider="slack", kind=EntityKind.CHANNEL, external_id="D0SOFIA"),
            operation=Operation.READ,
            actor=Actor.AGENT,
        )
    )
    store.attach(
        Exchange(
            method="GET",
            host="slack.com",
            path="/api/conversations.history?channel=D0SOFIA",
            status=200,
            response_body=HISTORY,
        ),
        first_seq=history_read,
        last_seq=history_read,
        provider="slack",
    )
    w.model_call(QUESTION, (1200, 80), "a1a1a1a1a1a1a1a1")
    asked = w.message("D0SOFIA", "1724493600.000100", QUESTION, [SOFIA], traceparent=f"00-{TRACE}-{CALLER_SPAN}-01")
    store.receive(
        [
            ReceivedSpan(
                trace_id=TRACE,
                span_id=CALLER_SPAN,
                name="POST slack.com",
                start=datetime.now(UTC),
                end=datetime.now(UTC),
            )
        ],
        source=SpanSource.RECEIVED,
    )
    to_dania = w.message("D0DANIA", "1724493600.000200", TO_DANIA, [DANIA])
    w.memory("asks/sofia", Operation.CREATE, '{"status":"asked"}')
    w.change(
        Change(
            entity=EntityRef(provider="crm", kind=EntityKind.STORED, external_id="/v1/contacts/7"),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body='{"id":7,"name":"Sofia Romano"}',
            after=StoredSnapshot(
                host="crm.example.com",
                collection="contacts",
                path="/v1/contacts",
                id="7",
                item='{"id":7,"name":"Sofia Romano"}',
            ),
        )
    )
    head = store.head()
    began = datetime.now(UTC)
    store.attach(
        Exchange(
            method="POST",
            host="hooks.example.com",
            path="/notify",
            status=202,
            request_body='{"event":"asked","person":"sofia"}',
            response_body='{"accepted":true}',
            captured=Captured(
                mode=CaptureMode.ACKNOWLEDGE,
                declared_as="hooks.example.com",
                answered_by=AnsweredBy.DECLARATION,
                started=began,
                ended=began + timedelta(milliseconds=12),
                request=Body(size=34, kept=BodyKept.WHOLE, sha256="0" * 64),
                response=Body(size=17, kept=BodyKept.WHOLE, sha256="0" * 64),
            ),
        ),
        first_seq=head + 1,
        last_seq=head,
    )
    store.attach(
        Exchange(method="GET", host="api.unknown.example", path="/v1/thing", status=502),
        first_seq=head + 1,
        last_seq=head,
    )
    w.change(
        Change(
            entity=NEXT_WAKE,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body=json.dumps({"at": (T0 + timedelta(hours=26)).isoformat()}),
            after=NextWakeSnapshot(at=T0 + timedelta(hours=26)),
        )
    )
    next_wake = DueEntry(
        due=Due(at=T0 + timedelta(hours=26), kind=DueKind.AGENT_WAKE, ref="next_wake"),
        source=DueSource.REPORTED,
        entered_at=T0,
        entered_wake=1,
    )
    w.due(1, next_wake)
    reply = PersonReply(
        person="sofia",
        in_reply_to=EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id="D0SOFIA/1724493600.000100"),
        text=ANSWER,
        at=T0 + timedelta(hours=30),
        written_by=Provenance(model="people-model", prompt_version="person-step/1"),
        writing=Writing.SCRIPT,
        facts=["40k a year"],
        drawn=Drawn(
            source=DrawnFrom.DELAY,
            seed=7,
            asked_at=T0,
            delay=DelayRange(shortest=timedelta(hours=30), longest=timedelta(hours=30)),
            offset=timedelta(hours=30),
            lands_at=T0 + timedelta(hours=30),
        ),
    )
    store.remember(reply)
    store.record_person_call(
        PersonCall(
            key="k" * 64,
            person="sofia",
            wrote=Wrote.REPLY,
            asked=reply.in_reply_to,
            model="people-model",
            prompt_version="person-step/1",
            input_tokens=300,
            output_tokens=12,
            answer=json.dumps({"text": ANSWER}),
            sim_time=T0,
            wake=1,
        )
    )
    landing = DueEntry(
        due=Due(at=T0 + timedelta(hours=30), kind=DueKind.PERSON_REPLY, ref="reply:0"),
        source=DueSource.REPLY,
        entered_at=T0,
        entered_wake=1,
        drawn=reply.drawn,
    )
    w.due(2, landing)
    fork_at = w.checkpoint(1, AgentStatus.WORKING, T0 + timedelta(hours=26))
    wakes = [_wake(1, T0, WakeReason.START, 4, 1, 1)]

    # wake 2, a day later: the agent follows up
    clock.jump(T0 + timedelta(hours=26))
    w.due(1, next_wake.model_copy(update={"closed": DueClosed.FIRED, "closed_at": clock.now(), "closed_wake": 1}))
    clock.begin_wake()
    w.memory("asks/sofia", Operation.READ)
    w.model_call(FOLLOW_UP, (900, 60), "b2b2b2b2b2b2b2b2")
    follow_up = w.message("D0SOFIA", "1724587200.000100", FOLLOW_UP, [SOFIA], thread="D0SOFIA/1724493600.000100")
    w.memory("asks/sofia", Operation.UPDATE, '{"status":"chased"}')
    w.checkpoint(1, AgentStatus.WORKING, None)
    wakes.append(_wake(2, clock.now(), WakeReason.DUE, 1, 1, 1))

    # wake 3: Sofia's answer lands; the agent tells the owner and is done, then writes its memory once more
    clock.jump(T0 + timedelta(hours=30))
    w.due(2, landing.model_copy(update={"closed": DueClosed.FIRED, "closed_at": clock.now(), "closed_wake": 2}))
    clock.begin_wake()
    answered = w.message(
        "D0SOFIA", "1724601600.000100", ANSWER, [], actor=Actor.PERSON, thread="D0SOFIA/1724493600.000100"
    )
    w.memory("asks/sofia", Operation.READ)
    told = w.message("D0OWNER", "1724601600.000200", TOLD, [OWNER])
    w.memory("asks/sofia", Operation.UPDATE, '{"status":"answered"}')
    w.checkpoint(1, AgentStatus.DONE, None)
    late_write = w.memory("notes/last", Operation.CREATE, '"told the owner"')
    wakes.append(_wake(3, clock.now(), WakeReason.PERSON_REPLIED, 1, 1, 2))
    record = RunRecord(
        run_id=run_id,
        scenario=played.name,
        seed=7,
        started_at=T0,
        ended_at=clock.now(),
        wall_seconds=1.0,
        stop=StopReason.AGENT_DONE,
        providers=["slack"],
        wakes=wakes,
    )
    _keep(state, run_id, played, record, store, wakes)

    # the fork, after wake 1: the same reply copied, and another follow-up
    fork_clock = RunClock(T0)
    fork_clock.enter(1)
    forked = store.fork(fork_id, at_seq=fork_at, clock=fork_clock)
    fw = _Writer(forked, fork_clock)
    forked.remember(reply)
    fork_clock.jump(T0 + timedelta(hours=26))
    fw.due(1, next_wake.model_copy(update={"closed": DueClosed.FIRED, "closed_at": fork_clock.now(), "closed_wake": 1}))
    fork_clock.begin_wake()
    fw.message("D0SOFIA", "1724587200.000900", FORK_FOLLOW_UP, [SOFIA], thread="D0SOFIA/1724493600.000100")
    fw.checkpoint(1, AgentStatus.DONE, None)
    fork_wakes = [wakes[0], _wake(2, fork_clock.now(), WakeReason.DUE, 1, 0, 0)]
    fork_record = record.model_copy(
        update={
            "run_id": fork_id,
            "parent_run": run_id,
            "forked_at": fork_at,
            "ended_at": fork_clock.now(),
            "wakes": fork_wakes,
        }
    )
    _keep(state, fork_id, played, fork_record, forked, fork_wakes)
    forked.close()
    store.close()
    return Fixture(
        state=state,
        run_id=run_id,
        fork_id=fork_id,
        fork_at=fork_at,
        asked=asked,
        to_dania=to_dania,
        follow_up=follow_up,
        answered=answered,
        told=told,
        late_write=late_write,
        history_read=history_read,
    )
