"""The viewer's paths for its timeline, its inspector and its views, each answered in its own typed shape: on real
runs of the end-to-end agent (a question Sofia never answers, and a fork where she answers 36 hours on), and on a
synthetic run of many days (`long_run.py`) that holds what the end-to-end agent never makes: model-written replies
with their tokens, stored items, the agent's spans and model calls, a dispatch rule holding a wake back."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from minutehand import run_all, session
from minutehand.adapters.query.reader import open_model
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import (
    AssessmentsResponse,
    BatchesResponse,
    CallDetail,
    CallRowsResponse,
    CallsResponse,
    DispatchResponse,
    EventDetail,
    FindingsResponse,
    LaneKind,
    MarkKind,
    MemoryResponse,
    ModelSide,
    ModelUseResponse,
    PeopleResponse,
    Refusal,
    RuleStatus,
    SpanResponse,
    StoredResponse,
    TimelineResponse,
)
from minutehand.application.run_clock import RunClock
from minutehand.domain.clock import DrawnFrom, DueKind, DueSource
from minutehand.domain.people import Writing
from minutehand.domain.prices import Price, Prices
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import ExpectedOutcome
from minutehand.domain.world import MessageSnapshot, Operation, WorldEvent
from tests.e2e.support import ANSWER, QUESTION
from tests.web import long_run
from tests.web.test_viewer_api import parent_and_fork


@asynccontextmanager
async def client(state: Path) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


async def read[M: BaseModel](c: httpx.AsyncClient, path: str, model: type[M]) -> M:
    response = await c.get(path)
    assert response.status_code == 200, response.text
    return model.model_validate_json(response.content)


@pytest.fixture
def days(tmp_path: Path) -> Path:
    """Twelve days of the synthetic run, written under a state directory of their own."""
    state = tmp_path / "days"
    long_run.write(state, days=12)
    return state


# -- on real runs --------------------------------------------------------------------------------------------------


async def test_the_timeline_puts_each_mark_in_its_lane_in_time_order_and_each_finding_at_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    parent, _, _ = await parent_and_fork(state, tmp_path, monkeypatch)
    async with client(state) as c:
        drawn = await read(c, f"/api/runs/{parent}/timeline", TimelineResponse)
        findings = await read(c, f"/api/runs/{parent}/findings", FindingsResponse)
        calls = await read(c, f"/api/runs/{parent}/calls", CallsResponse)

    m = drawn.marks
    assert len({len(m.sim), len(m.sim_end), len(m.real), len(m.real_end), len(m.lane), len(m.kind), len(m.ref)}) == 1
    assert m.sim == sorted(m.sim), "the page bins each lane by binary search: the marks come sorted"
    lanes = {lane.key: i for i, lane in enumerate(drawn.lanes)}
    assert drawn.lanes[0].kind is LaneKind.WAKES and "person:sofia" in lanes and "provider:slack" in lanes
    assert [lane.marks for lane in drawn.lanes] == [m.lane.count(i) for i in range(len(drawn.lanes))]
    sent = [i for i, k in enumerate(m.kind) if k is MarkKind.SENT and m.lane[i] == lanes["person:sofia"]]
    assert [m.label[i] for i in sent][:1] == [QUESTION]
    [wait] = [i for i, k in enumerate(m.kind) if k is MarkKind.WAIT]
    ends = m.sim_end[wait]
    assert m.lane[wait] == lanes["person:sofia"] and ends is not None and ends > m.sim[wait]
    assert sum(1 for k in m.kind if k in (MarkKind.CALL, MarkKind.CALL_FAILED)) == len(calls.calls)
    [silence] = [f for f in findings.findings if f.finding.check == "follows_up_when_due"]
    [placed] = [f for f in drawn.findings if f.number == silence.number]
    assert placed.refs == [f"ev:{s}" for s in silence.finding.evidence] and set(placed.refs) <= set(m.ref)
    assert drawn.deadline is not None and drawn.starts < drawn.reached <= drawn.deadline


async def test_one_event_is_read_whole_with_its_call_its_conversation_and_the_findings_citing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    parent, _, _ = await parent_and_fork(state, tmp_path, monkeypatch)
    async with client(state) as c:
        drawn = await read(c, f"/api/runs/{parent}/timeline", TimelineResponse)
        [ref] = [r for r, label in zip(drawn.marks.ref, drawn.marks.label, strict=True) if label == QUESTION][:1]
        seq = int(ref.split(":")[1])
        detail = await read(c, f"/api/runs/{parent}/events/{seq}", EventDetail)
        assert detail.call is not None
        call = await read(c, f"/api/runs/{parent}/calls/{detail.call}", CallDetail)
        findings = await read(c, f"/api/runs/{parent}/findings", FindingsResponse)
        rows = await read(c, f"/api/runs/{parent}/call-rows", CallRowsResponse)

    assert isinstance(detail.event.after, MessageSnapshot) and detail.event.after.text == QUESTION
    assert call.row.first_seq is not None and call.row.last_seq is not None
    assert call.row.first_seq <= seq <= call.row.last_seq and call.call.exchange.host == "slack.com"
    assert call.row.call_id == detail.call and rows.calls[detail.call - 1] == call.row
    assert [line.seq for line in detail.thread][:1] == [seq]
    cited = [f.number for f in findings.findings if seq in f.finding.evidence]
    assert cited and detail.findings == cited


async def test_an_event_or_call_the_run_does_not_hold_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    parent, _, _ = await parent_and_fork(state, tmp_path, monkeypatch)
    async with client(state) as c:
        for path in (
            f"/api/runs/{parent}/events/999999",
            f"/api/runs/{parent}/calls/999999",
            "/api/runs/nosuch/timeline",
        ):
            response = await c.get(path)
            assert response.status_code == 404, path
            assert Refusal.model_validate_json(response.content).error


async def test_a_reply_says_how_it_was_written_and_drawn_and_its_due_entry_holds_the_same_draw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    _, child, _ = await parent_and_fork(state, tmp_path, monkeypatch)
    async with client(state) as c:
        people = await read(c, f"/api/runs/{child}/people", PeopleResponse)
        table = await read(c, f"/api/runs/{child}/dispatch", DispatchResponse)

    [sofia] = [p for p in people.people if p.key == "sofia"]
    [answered] = sofia.replies
    assert answered.reply.text == ANSWER and answered.reply.writing is Writing.VERBATIM
    drawn = answered.reply.drawn
    assert drawn is not None and drawn.source is DrawnFrom.DELAY and drawn.offset == timedelta(hours=36)
    assert answered.asked is not None and sofia.model_calls == []  # her words are the script's own: no model
    said = [(line.from_agent, line.text) for line in sofia.conversation]
    assert said.index((False, ANSWER)) > said.index((True, QUESTION))
    [due] = [r for r in table.entries if r.kind is DueKind.PERSON_REPLY]
    assert due.due_at == drawn.lands_at and due.source is DueSource.REPLY


async def test_each_rule_says_whether_it_held_how_often_it_was_read_and_which_findings_are_its(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    parent, child, _ = await parent_and_fork(state, tmp_path, monkeypatch)
    async with client(state) as c:
        silent = await read(c, f"/api/runs/{parent}/assessments", AssessmentsResponse)
        answered = await read(c, f"/api/runs/{child}/assessments", AssessmentsResponse)
        findings = await read(c, f"/api/runs/{parent}/findings", FindingsResponse)

    [rule] = silent.rules
    assert rule.rule.id == "follows_up_when_due" and rule.status is RuleStatus.FAILED
    assert rule.read == 1 and rule.findings == [f.number for f in findings.findings if f.finding.check == rule.rule.id]
    [unread] = answered.rules  # the fork ends once Sofia answers, before the moment the rule reads it at
    assert (unread.status, unread.read, unread.unread, unread.findings) == (RuleStatus.UNREAD, 0, 1, [])


# -- on a synthetic run of many days --------------------------------------------------------------------------------


async def test_a_person_whose_words_a_model_wrote_carries_each_call_and_its_tokens(days: Path) -> None:
    async with client(days) as c:
        people = await read(c, f"/api/runs/{long_run.RUN_ID}/people", PeopleResponse)
        drawn = await read(c, f"/api/runs/{long_run.RUN_ID}/timeline", TimelineResponse)

    replied = [p for p in people.people if p.replies]
    assert replied
    for person in replied:
        assert len(person.model_calls) == len(person.replies)
        assert all(r.reply.writing is Writing.SCRIPT and r.reply.written_by is not None for r in person.replies)
        assert all((m.call.input_tokens or 0) > 0 for m in person.model_calls)
        theirs = [line for line in person.conversation if not line.from_agent]
        assert [line.reply for line in theirs] == [r.index for r in person.replies]
        assert all(line.written_by == "m-people" for line in theirs)
    lane = next(i for i, lane in enumerate(drawn.lanes) if lane.kind is LaneKind.PEOPLE_MODEL)
    assert drawn.marks.lane.count(lane) == sum(len(p.model_calls) for p in people.people)


async def test_memory_and_stored_items_carry_each_write_with_what_it_replaced(days: Path) -> None:
    async with client(days) as c:
        memory = await read(c, f"/api/runs/{long_run.RUN_ID}/memory", MemoryResponse)
        stored = await read(c, f"/api/runs/{long_run.RUN_ID}/stored", StoredResponse)

    last: dict[str, str | None] = {}
    for change in memory.changes:
        key = f"{change.collection}/{change.key}"
        assert change.before == (last[key] if key in last else None), key
        last[key] = change.value
    for key in memory.keys:
        written = [c for c in memory.changes if c.collection == key.collection and c.key == key.key]
        assert key.writes == len(written) and key.value == (written[-1].value if written else None)
    assert memory.reads == 12 * 40 and len(memory.changes) == 12 * 20
    held: dict[str, str | None] = {}
    for change in stored.changes:
        assert change.before == (held[change.id] if change.id in held else None)
        held[change.id] = change.item
    assert len(stored.changes) == 4 and all(c.operation is Operation.CREATE for c in stored.changes)


async def test_the_dispatch_table_says_which_wake_a_rule_held_back_and_the_timeline_marks_it(days: Path) -> None:
    async with client(days) as c:
        table = await read(c, f"/api/runs/{long_run.RUN_ID}/dispatch", DispatchResponse)
        drawn = await read(c, f"/api/runs/{long_run.RUN_ID}/timeline", TimelineResponse)

    faulted = [r.due_id for r in table.entries if r.fault is not None]
    assert len(faulted) == 1
    marks = {ref: kind for ref, kind in zip(drawn.marks.ref, drawn.marks.kind, strict=True) if ref.startswith("due:")}
    assert marks[f"due:{faulted[0]}"] is MarkKind.DUE_FAULT
    assert {k for r, k in marks.items() if r != f"due:{faulted[0]}"} == {MarkKind.DUE}
    assert len(marks) == len(table.entries)


async def test_the_agents_spans_and_model_calls_have_lanes_and_one_span_is_read_whole(days: Path) -> None:
    async with client(days) as c:
        drawn = await read(c, f"/api/runs/{long_run.RUN_ID}/timeline", TimelineResponse)
        refs = [r for r in drawn.marks.ref if r.startswith("span:")]
        span = await read(c, f"/api/runs/{long_run.RUN_ID}/spans/{refs[0].split(':')[1]}", SpanResponse)
        missing = await c.get(f"/api/runs/{long_run.RUN_ID}/spans/{'f' * 16}")

    kinds = {lane.kind: lane.marks for lane in drawn.lanes}
    assert kinds[LaneKind.SPANS] == kinds[LaneKind.AGENT_MODEL] == 12
    assert span.span.span.name == "wake" and missing.status_code == 404


async def test_the_batches_run_all_kept_are_served_with_each_sample_that_is_still_here(days: Path) -> None:
    kept = run_all.Batch(
        batch_id="b1",
        folder="scenarios",
        samples=2,
        played=[
            run_all.ScenarioPlayed(
                file="scenarios/a.yaml",
                scenario="a",
                expected=ExpectedOutcome.PASSED,
                samples=[
                    run_all.SamplePlayed(
                        seed=7, verdict=VerdictKind.PASSED, words="Passed.", run_id=long_run.RUN_ID, log="l"
                    ),
                    run_all.SamplePlayed(seed=8, verdict=VerdictKind.FAILED, words="Failed.", run_id="gone", log="l"),
                ],
                counts={"passed": 1, "failed": 1},
                failing_seeds=[8],
                matched=False,
            )
        ],
    )
    folder = days / run_all.BATCHES / "b1"
    folder.mkdir(parents=True)
    (folder / run_all.KEPT).write_text(kept.model_dump_json())
    async with client(days) as c:
        served = await read(c, "/api/batches", BatchesResponse)

    [batch] = served.batches
    [played] = batch.scenarios
    assert (played.passed, played.matched, played.failing_seeds, played.expected) == (1, False, [8], "passed")
    assert [s.run_id for s in played.samples] == [long_run.RUN_ID, None], "a run since removed is not linked"


def test_the_fork_points_of_a_long_run_read_its_log_once(days: Path) -> None:
    """The viewer's `/api/runs/{id}` lists every checkpoint; each once read the log from its own seq on, which on a
    run of hundreds of wakes kept the page waiting for a minute."""

    class Counting(SqliteStore):
        reads = 0

        def events(self, *, since: int = 0) -> list[WorldEvent]:
            Counting.reads += 1
            return super().events(since=since)

    world = Counting(session.run_dir(days, long_run.RUN_ID) / session.WORLD, long_run.RUN_ID, RunClock(long_run.T0))
    try:
        points = session.points_in(world)
    finally:
        world.close()
    assert len(points) == 13 and Counting.reads == 1


def test_the_long_run_fixture_writes_what_its_docstring_says(days: Path) -> None:
    outcome = session.load(days, long_run.RUN_ID)
    assert len(outcome.record.wakes) == 12 and outcome.result.findings
    raw = json.loads((session.run_dir(days, long_run.RUN_ID) / session.SCENARIO).read_text())
    assert raw["name"] == long_run.scenario(12).name


async def test_model_use_is_the_read_models_and_costs_only_what_a_declared_price_names(days: Path) -> None:
    """The page's model cost reads the read model's `model_calls`, as `minutehand query` does: the people's model
    is priced here, the agent's is not, so only the people's calls have a cost."""
    priced = Prices(prices=[Price(model="m-people", input_per_million=2.0, output_per_million=8.0, currency="EUR")])
    transport = httpx.ASGITransport(app=create_app(days, priced))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        use = await read(c, f"/api/runs/{long_run.RUN_ID}/model-use", ModelUseResponse)
        rows = await read(c, f"/api/runs/{long_run.RUN_ID}/call-rows", CallRowsResponse)
    async with client(days) as c:
        unpriced = await read(c, f"/api/runs/{long_run.RUN_ID}/model-use", ModelUseResponse)

    db = open_model(days, long_run.RUN_ID, priced)
    assert [r.call_id for r in rows.calls] == [r[0] for r in db.execute("SELECT call_id FROM calls ORDER BY call_id")]
    assert use.priced and not unpriced.priced and len(use.calls) == len(unpriced.calls)
    people = [c for c in use.calls if c.side is ModelSide.PERSON]
    agent = [c for c in use.calls if c.side is ModelSide.AGENT]
    assert people and agent and all(c.cost is None for c in agent) and all(c.cost is None for c in unpriced.calls)
    for c in people:
        assert c.input_tokens is not None and c.output_tokens is not None and c.currency == "EUR"
        assert c.cost == pytest.approx((c.input_tokens * 2.0 + c.output_tokens * 8.0) / 1e6)
