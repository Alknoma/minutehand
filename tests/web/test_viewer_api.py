"""The viewer's JSON API and its page, through httpx's ASGI transport, on real finished runs of the end-to-end
test agent and on a run another connection is still writing.

No browser runs here: what the page draws is not asserted, only that it reaches nothing beyond its own API and
that every API path it calls exists and answers."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from minutehand import session
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.web.app import PAGE, create_app
from minutehand.adapters.web.responses import (
    CallsResponse,
    EventsResponse,
    FindingsResponse,
    ObligationsResponse,
    Refusal,
    RunResponse,
    RunsResponse,
    ScorecardResponse,
    WakesResponse,
)
from minutehand.application.checkpoint import CHECKPOINT, Checkpoint, NoHooks, write_checkpoint
from minutehand.application.run_clock import RunClock
from minutehand.checks.patterns import pattern
from minutehand.domain.checks import FindingKind, ObligationKind
from minutehand.domain.experiment import Fork, PersonChange
from minutehand.domain.scenario import Silent
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation
from tests.e2e.support import QUESTION, SOFIA, T0, agent_under_test, answers, scenario

PATHS = ("", "/wakes", "/events", "/calls", "/obligations", "/findings", "/scorecard")


@asynccontextmanager
async def client(state: Path) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


async def read[M: BaseModel](c: httpx.AsyncClient, path: str, model: type[M]) -> M:
    response = await c.get(path)
    assert response.status_code == 200, response.text
    return model.model_validate_json(response.content)


async def parent_and_fork(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, int]:
    """A forgetful agent whose question Sofia never answers, and a rerun after its first wake where she does."""
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", hooks=True)
    [parent] = await session.play(scenario(Silent()), launched.agent, state=state, command=launched.command)
    point = next(p for p in session.fork_points(state, parent.record.run_id) if p.wake == 1)
    changes = Fork(
        parent_run=parent.record.run_id,
        at_seq=point.seq,
        overrides=[PersonChange(person="sofia", reply=answers(after=timedelta(hours=36)))],
    )
    [child] = await session.fork(parent.record.run_id, changes, state=state, command=launched.command)
    return parent.record.run_id, child.record.run_id, point.seq


async def test_the_runs_carry_the_fork_tree_and_the_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    parent, child, at_seq = await parent_and_fork(state, tmp_path, monkeypatch)
    async with client(state) as c:
        listed = await read(c, "/api/runs", RunsResponse)

    by_id = {r.run_id: r for r in listed.runs}
    assert list(by_id) == [parent, child]
    assert by_id[parent].children == [child] and by_id[parent].parent_run is None
    assert by_id[parent].finished and by_id[parent].failed >= 1
    assert (by_id[child].parent_run, by_id[child].forked_at, by_id[child].forked_after_wake) == (parent, at_seq, 1)
    assert by_id[child].failed < by_id[parent].failed
    assert by_id[parent].goal == scenario(Silent()).goal


async def test_a_finished_run_answers_every_path_with_its_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    parent, _, _ = await parent_and_fork(state, tmp_path, monkeypatch)
    base = f"/api/runs/{parent}"
    async with client(state) as c:
        run = await read(c, base, RunResponse)
        wakes = await read(c, f"{base}/wakes", WakesResponse)
        events = await read(c, f"{base}/events", EventsResponse)
        calls = await read(c, f"{base}/calls", CallsResponse)
        obligations = await read(c, f"{base}/obligations", ObligationsResponse)
        findings = await read(c, f"{base}/findings", FindingsResponse)
        scorecard = await read(c, f"{base}/scorecard", ScorecardResponse)

    assert run.finished and run.record is not None and run.reached == run.record.ended_at
    assert [p.wake for p in run.checkpoints][:2] == [0, 1]
    assert wakes.wakes == run.record.wakes
    assert all(e.entity != CHECKPOINT for e in events.events)
    [asked] = [e for e in events.events if isinstance(e.after, MessageSnapshot) and e.actor is Actor.AGENT]
    assert isinstance(asked.after, MessageSnapshot) and asked.after.text == QUESTION
    assert calls.calls and {c.exchange.host for c in calls.calls} == {"slack.com"}
    [wait] = [o for o in obligations.obligations if o.kind is ObligationKind.ANSWER_FROM_PERSON]
    assert (wait.person, wait.opened_by, wait.settled_at) == ("sofia", asked.seq, None)
    assert findings.finished
    [silence] = [x for x in findings.findings if x.finding.check == "no_follow_up"]
    assert silence.finding.kind is FindingKind.FAIL and silence.pattern == pattern("expiry_on_every_wait")
    assert asked.seq in silence.finding.evidence
    assert scorecard.scorecard is not None and scorecard.scorecard.expectations_total == 2


async def test_an_unknown_run_is_a_404_on_every_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    await parent_and_fork(state, tmp_path, monkeypatch)
    async with client(state) as c:
        for path in PATHS:
            response = await c.get(f"/api/runs/nosuchrun{path}")
            assert response.status_code == 404, path
            assert "no run nosuchrun" in Refusal.model_validate_json(response.content).error


async def test_a_run_still_being_written_is_read_as_of_its_last_commit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    directory = session.run_dir(state, "running")
    directory.mkdir(parents=True)
    (directory / session.SCENARIO).write_text(scenario(Silent()).model_dump_json())
    clock = RunClock(T0)
    writer = SqliteStore(directory / session.WORLD, "running", clock)
    empty = Checkpoint(wake=0, now=T0, replies=0, pending=[], agent=NoHooks())
    write_checkpoint(writer, empty)
    clock.begin_wake()
    question = Change(
        entity=EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id="1.000001"),
        operation=Operation.CREATE,
        actor=Actor.AGENT,
        body="{}",
        after=MessageSnapshot(text=QUESTION, channel="D1", recipient_emails=[SOFIA]),
    )
    asked = writer.apply(question)
    write_checkpoint(writer, empty.model_copy(update={"wake": 1}))
    clock.jump(T0 + timedelta(hours=2))
    clock.begin_wake()
    # Another writer holds the file's write lock mid-transaction: what it is writing is not committed yet.
    holder = sqlite3.connect(directory / session.WORLD)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO reply VALUES('running', 0, 'not committed')")
    try:
        async with client(state) as c:
            listed = await read(c, "/api/runs", RunsResponse)
            run = await read(c, "/api/runs/running", RunResponse)
            wakes = await read(c, "/api/runs/running/wakes", WakesResponse)
            events = await read(c, "/api/runs/running/events", EventsResponse)
            obligations = await read(c, "/api/runs/running/obligations", ObligationsResponse)
            findings = await read(c, "/api/runs/running/findings", FindingsResponse)
            scorecard = await read(c, "/api/runs/running/scorecard", ScorecardResponse)
    finally:
        holder.rollback()
        holder.close()

    [row] = listed.runs
    assert (row.run_id, row.finished, row.stop, row.failed) == ("running", False, None, 0)
    assert (run.finished, run.record, run.reached) == (False, None, T0)
    assert [(w.index, w.world_changes) for w in wakes.wakes] == [(1, 1)]
    assert [e.seq for e in events.events] == [asked.seq]
    [wait] = [o for o in obligations.obligations if o.kind is ObligationKind.ANSWER_FROM_PERSON]
    assert wait.opened_by == asked.seq and wait.settled_at is None
    assert (findings.finished, findings.findings) == (False, [])
    assert scorecard.scorecard is None


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


async def test_the_page_is_served_and_refers_to_nothing_beyond_this_machine(tmp_path: Path) -> None:
    async with client(tmp_path / "state") as c:
        response = await c.get("/")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert response.text == _page()
    assert re.findall(r"https?://", response.text, flags=re.IGNORECASE) == []
    assert re.findall(r"""\b(?:src|href)\s*=\s*["'](?!#)""", response.text) == []
    assert "@import" not in response.text and "url(" not in response.text


async def test_every_api_path_the_page_calls_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    parent, _, _ = await parent_and_fork(state, tmp_path, monkeypatch)
    called = sorted(set(re.findall(r'"(/api/[^"]*)"', _page())))
    assert "/api/runs" in called and "/api/runs/{run}/obligations" in called
    async with client(state) as c:
        for path in called:
            response = await c.get(path.replace("{run}", parent))
            assert response.status_code == 200, (path, response.text)
