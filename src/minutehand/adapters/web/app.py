"""The viewer: a read-only JSON API over a state directory, and the one page that draws it.

    minutehand view [--state DIR] [--port N]      serves both on 127.0.0.1 only

    GET /                                   viewer.html, which loads its script, its styles and the vendored
                                            libraries from /static/ on this server and nothing from elsewhere
    GET /static/...                         those files (`adapters/web/static/`), each library's licence beside it
    GET /api/runs                           every run, finished or still running, with the fork tree
    GET /api/runs/{run_id}                  the scenario, the record once finished, the checkpoints, and for a
                                            fork what it changed and how it differs from its parent
    GET /api/runs/{run_id}/wakes
    GET /api/runs/{run_id}/obligations      what the world was waiting on (`checks.ledger`), and when each fell due
    GET /api/runs/{run_id}/findings         each with its pattern, once the run is checked
    GET /api/runs/{run_id}/scorecard
    GET /api/runs/{run_id}/model-calls/{span_id}   one model call: what it was asked and answered, what it wrote
    GET /api/runs/{run_id}/steps/{step}/spans      the agent's spans placed in one step, for a waterfall

A case (standing worlds opened under one case label) is one run here: its worlds are read as one log and are not
listed on their own, and a standing world no call reached (a probe) is not listed.

What the page reads to draw a run of any length (`reading.py`); the paths marked (read model) are rows of the run's
read model (`adapters/query`, `docs/querying.md`), so the page and `minutehand query` show the same facts, and the
messages the timeline, an event's conversation and the people view show are the read model's `messages`. The run's
whole log, its calls, its messages and its model calls as lists are `minutehand query`'s, not the page's:
    GET /api/runs/{run_id}/timeline         every mark of the run, compact and sorted, in lanes, with the findings
                                            placed where they happened: the page bins them itself
    GET /api/runs/{run_id}/events/{seq}     one event whole: the version before it, its call, its thread, its writer
    GET /api/runs/{run_id}/call-rows        every HTTP call without its bodies, and who answered it   (read model)
    GET /api/runs/{run_id}/calls/{call_id}  one call whole, its read-model row beside it
    GET /api/runs/{run_id}/dispatch         the run loop's table of what was due: draws and faults    (read model)
    GET /api/runs/{run_id}/memory           the agent's memory: every write with the value it replaced (read model)
    GET /api/runs/{run_id}/stored           every item kept for a host declared `store`               (read model)
    GET /api/runs/{run_id}/model-use        every model call, the agent's and people's: tokens, cost  (read model)
    GET /api/runs/{run_id}/people           per person: the conversation, their replies (how each was written and
                                            drawn) and the model calls that wrote them, with tokens
    GET /api/runs/{run_id}/assessments      each of the team's rules: passed or failed, how often read, its findings
    GET /api/runs/{run_id}/spans/{span_id}  one span of the agent's telemetry, with its attributes
    GET /api/batches                        every `run-all` batch: pass rate per scenario, each sample's seed and run

Every world file is opened read-only (`session.reading`), so a run another process is still writing is
read as of its last commit, and the viewer can never change or lock a run.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from minutehand import session
from minutehand.adapters.query.reader import open_model
from minutehand.adapters.web import reading
from minutehand.adapters.web.responses import (
    AssessmentsResponse,
    BatchesResponse,
    CallRowsResponse,
    DrawnWait,
    FindingsResponse,
    ModelCallResponse,
    ObligationsResponse,
    Refusal,
    RunResponse,
    RunRow,
    RunsResponse,
    ScorecardResponse,
    SpanBar,
    SpanResponse,
    StepSpansResponse,
    TimelineResponse,
    WakesResponse,
    explained,
)
from minutehand.application.model_calls import JoinedBy, is_model_call, model_call, trace_of
from minutehand.application.refusals import RunRefused
from minutehand.checks.items import ItemChecks
from minutehand.checks.runner import RunResult, view_of
from minutehand.domain.checks import FindingKind
from minutehand.domain.items import ItemAssessment
from minutehand.domain.prices import Prices
from minutehand.domain.scenario import Model
from minutehand.domain.telemetry import SpanSource
from minutehand.domain.world import Actor, MessageSnapshot
from minutehand.ports.store import Store
from minutehand.session import Logged

PAGE = Path(__file__).with_name("viewer.html")
STATIC = Path(__file__).with_name("static")
HOST = "127.0.0.1"
KEEP = 8
"""Timelines of finished runs kept built, the latest read first: a finished run's log no longer changes."""


class _Static(StaticFiles):
    """The page's own files, each to be asked again before it is used (`no-cache`, answered 304 by its ETag while it
    is unchanged): with no word on caching a browser guesses how long to keep them, and after an upgrade runs a mix of
    old and new modules, one importing a file that is gone."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        answered = super().file_response(*args, **kwargs)
        answered.headers["Cache-Control"] = "no-cache"
        return answered


def _json(model: Model, status: int = 200) -> Response:
    return Response(model.model_dump_json(), status_code=status, media_type="application/json")


def create_app(state: Path, prices: Prices | None = None) -> Starlette:
    """The viewer over one state directory; `prices` gives each model call a cost, as `minutehand query --prices`."""

    reassessed: dict[str, list[ItemAssessment]] = {}
    lock = threading.Lock()

    def page(_: Request) -> Response:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})

    def runs(_: Request) -> Response:
        hidden = set(session.case_members(state))
        entries = [e for e in session.logged(state) if e.run_id not in hidden]
        children: dict[str, list[str]] = {e.run_id: [] for e in entries}
        for entry in entries:
            if entry.parent_run is not None and entry.parent_run in children:
                children[entry.parent_run].append(entry.run_id)
        rows: list[RunRow] = []
        for entry in entries:
            try:
                if entry.finished and session.probe(session.load(state, entry.run_id)):
                    continue  # a standing world no call reached
                rows.append(_row(state, entry, children[entry.run_id]))
            except RunRefused:
                continue  # its directory exists and its scenario is not written yet: listed on the next refresh
        return _json(RunsResponse(runs=rows))

    def run(run_id: str) -> Response:
        entry = session.find(state, run_id)
        record = session.load(state, run_id).record if entry.finished else None
        with session.reading(state, run_id) as world:
            events = world.events()
            checkpoints = session.points_in(world)
        scenario = session.scenario_of(state, run_id)
        reached = record.ended_at if record is not None else max((e.sim_time for e in events), default=None)
        return _json(
            RunResponse(
                run_id=run_id,
                finished=entry.finished,
                scenario=scenario,
                record=record,
                reached=reached or scenario.starts_at,
                checkpoints=checkpoints,
                driven=session.driven(state, run_id),
                fork=session.fork_account(state, run_id),
                program=session.program_record(state, run_id),
            )
        )

    def wakes(run_id: str) -> Response:
        with session.reading(state, run_id) as world:
            return _json(WakesResponse(wakes=session.wakes_of(state, run_id, world)))

    def obligations(run_id: str) -> Response:
        scenario = session.scenario_of(state, run_id)
        with session.reading(state, run_id) as world:
            view = view_of(
                scenario,
                world.events(),
                session.wakes_of(state, run_id, world),
                world.replies(),
            )
            sim_of = {e.seq: e.sim_time for e in world.events()}
        return _json(
            ObligationsResponse(
                obligations=[
                    DrawnWait(
                        obligation=o,
                        touched_at=[sim_of[s] for s in o.agent_touches if s in sim_of],
                        came_back_at=sim_of[o.first_touch_after_settled]
                        if o.first_touch_after_settled is not None and o.first_touch_after_settled in sim_of
                        else None,
                    )
                    for o in view.obligations
                ]
            )
        )

    def findings(run_id: str) -> Response:
        if not session.find(state, run_id).finished:
            return _json(FindingsResponse(finished=False, verdict=None, findings=[], blocked=[], notes=[]))
        result = session.load(state, run_id).result
        items, again = result.items, False
        if not items:
            # a run checked before each output's result was kept: the item checks read only its record, so they say
            # it again on read, kept in memory and never written into the run
            with lock:
                if run_id not in reassessed:
                    reassessed[run_id] = ItemChecks().run(session.recorded_view(state, run_id)).items
                    while len(reassessed) > KEEP:
                        del reassessed[next(iter(reassessed))]
                items, again = reassessed[run_id], True
        return _json(
            FindingsResponse(
                finished=True,
                verdict=result.verdict,
                findings=explained(result),
                blocked=result.blocked,
                notes=result.notes,
                simulation=result.simulation,
                items=items,
                items_reassessed=again,
            )
        )

    def scorecard(run_id: str) -> Response:
        if not session.find(state, run_id).finished:
            return _json(ScorecardResponse(scorecard=None))
        return _json(ScorecardResponse(scorecard=session.load(state, run_id).result.effectiveness))

    def step_spans(request: Request) -> Response:
        run_id, step = request.path_params["run_id"], request.path_params["step"]
        assert isinstance(run_id, str) and isinstance(step, int)
        try:
            with session.reading(state, run_id) as world:
                placed = [s for s in world.spans(wake=step) if s.source is not SpanSource.LOG]
        except RunRefused as e:
            return _json(Refusal(error=str(e)), status=404)
        return _json(
            StepSpansResponse(
                step=step,
                spans=[
                    SpanBar(
                        span_id=s.span.span_id,
                        parent_span_id=s.span.parent_span_id,
                        trace_id=s.span.trace_id,
                        name=s.span.name,
                        service=s.span.service_name,
                        start=s.span.start,
                        end=s.span.end,
                        status=s.span.status,
                        model_call=is_model_call(s),
                    )
                    for s in sorted(placed, key=lambda s: s.span.start)
                ],
            )
        )

    def one_model_call(request: Request) -> Response:
        run_id, span_id = request.path_params["run_id"], request.path_params["span_id"]
        assert isinstance(run_id, str) and isinstance(span_id, str)
        try:
            with session.reading(state, run_id) as world:
                found = next((s for s in world.spans() if s.span.span_id == span_id.lower() and is_model_call(s)), None)
                if found is None:
                    return _json(Refusal(error=f"run {run_id} holds no model call {span_id}"), status=404)
                wrote = _writers(world)
                return _json(
                    ModelCallResponse(
                        call=model_call(found, world.spans(trace_id=found.span.trace_id)),
                        wrote=[seq for seq, _ in wrote[found.span.span_id]] if found.span.span_id in wrote else [],
                    )
                )
        except RunRefused as e:
            return _json(Refusal(error=str(e)), status=404)

    built: dict[tuple[str, int], TimelineResponse] = {}

    def result_of(run_id: str) -> RunResult | None:
        return session.load(state, run_id).result if session.find(state, run_id).finished else None

    def timeline(run_id: str) -> Response:
        result = result_of(run_id)
        with session.reading(state, run_id) as world:
            key = (run_id, world.head())
            if result is not None and key in built:
                return _json(built[key])
            drawn = reading.timeline(model(run_id), state, run_id, world, result)
        if result is not None:
            built[key] = drawn
            while len(built) > KEEP:
                del built[next(iter(built))]
        return _json(drawn)

    def one_event(request: Request) -> Response:
        run_id, seq = request.path_params["run_id"], request.path_params["seq"]
        assert isinstance(run_id, str) and isinstance(seq, int)
        try:
            scenario = session.scenario_of(state, run_id)
            result = result_of(run_id)
            with session.reading(state, run_id) as world:
                found = reading.event_detail(model(run_id), world, seq, scenario, result)
        except RunRefused as e:
            return _json(Refusal(error=str(e)), status=404)
        if found is None:
            return _json(Refusal(error=f"run {run_id} holds no event {seq}"), status=404)
        return _json(found)

    building = threading.Lock()

    def model(run_id: str) -> sqlite3.Connection:
        """The run's read model (`adapters/query`), the tables `minutehand query` reads, built once per state of its
        files: the page asks for several of its tables at once, and the first builds it while the rest wait."""
        with building:
            return open_model(state, run_id, prices)

    def call_rows(run_id: str) -> Response:
        return _json(CallRowsResponse(calls=reading.call_rows(model(run_id))))

    def one_call(request: Request) -> Response:
        run_id, call_id = request.path_params["run_id"], request.path_params["call_id"]
        assert isinstance(run_id, str) and isinstance(call_id, int)
        try:
            db = model(run_id)
            with session.reading(state, run_id) as world:
                found = reading.call_detail(db, world, call_id)
        except RunRefused as e:
            return _json(Refusal(error=str(e)), status=404)
        if found is None:
            return _json(Refusal(error=f"run {run_id} holds no call {call_id}"), status=404)
        return _json(found)

    def dispatch(run_id: str) -> Response:
        db = model(run_id)
        with session.reading(state, run_id) as world:
            return _json(reading.dispatch(db, world))

    def memory(run_id: str) -> Response:
        return _json(reading.memory(model(run_id)))

    def stored(run_id: str) -> Response:
        return _json(reading.stored(model(run_id)))

    def model_use(run_id: str) -> Response:
        return _json(reading.model_use(model(run_id), priced=prices is not None))

    def people(run_id: str) -> Response:
        scenario = session.scenario_of(state, run_id)
        with session.reading(state, run_id) as world:
            return _json(reading.people(model(run_id), world, scenario))

    def assessments(run_id: str) -> Response:
        answer: AssessmentsResponse = reading.assessments(state, run_id)
        return _json(answer)

    def one_span(request: Request) -> Response:
        run_id, span_id = request.path_params["run_id"], request.path_params["span_id"]
        assert isinstance(run_id, str) and isinstance(span_id, str)
        try:
            with session.reading(state, run_id) as world:
                found = next((s for s in world.spans() if s.span.span_id == span_id.lower()), None)
        except RunRefused as e:
            return _json(Refusal(error=str(e)), status=404)
        if found is None:
            return _json(Refusal(error=f"run {run_id} holds no span {span_id}"), status=404)
        return _json(SpanResponse(span=found))

    def batches(_: Request) -> Response:
        answer: BatchesResponse = reading.batches(state)
        return _json(answer)

    def one_run(handler: Callable[[str], Response]) -> Callable[[Request], Response]:
        def endpoint(request: Request) -> Response:
            run_id = request.path_params["run_id"]
            assert isinstance(run_id, str)
            try:
                return handler(run_id)
            except RunRefused as e:
                return _json(Refusal(error=str(e)), status=404)

        return endpoint

    return Starlette(
        routes=[
            Route("/", page),
            Route("/api/runs", runs),
            Route("/api/runs/{run_id}", one_run(run)),
            Route("/api/runs/{run_id}/wakes", one_run(wakes)),
            Route("/api/runs/{run_id}/obligations", one_run(obligations)),
            Route("/api/runs/{run_id}/findings", one_run(findings)),
            Route("/api/runs/{run_id}/scorecard", one_run(scorecard)),
            Route("/api/runs/{run_id}/model-calls/{span_id}", one_model_call),
            Route("/api/runs/{run_id}/steps/{step:int}/spans", step_spans),
            Route("/api/runs/{run_id}/timeline", one_run(timeline)),
            Route("/api/runs/{run_id}/events/{seq:int}", one_event),
            Route("/api/runs/{run_id}/call-rows", one_run(call_rows)),
            Route("/api/runs/{run_id}/calls/{call_id:int}", one_call),
            Route("/api/runs/{run_id}/model-use", one_run(model_use)),
            Route("/api/runs/{run_id}/dispatch", one_run(dispatch)),
            Route("/api/runs/{run_id}/memory", one_run(memory)),
            Route("/api/runs/{run_id}/stored", one_run(stored)),
            Route("/api/runs/{run_id}/people", one_run(people)),
            Route("/api/runs/{run_id}/assessments", one_run(assessments)),
            Route("/api/runs/{run_id}/spans/{span_id}", one_span),
            Route("/api/batches", batches),
            Mount("/static", _Static(directory=STATIC), name="static"),
        ],
        middleware=[Middleware(GZipMiddleware, minimum_size=2048)],
    )


def _writers(world: Store) -> dict[str, list[tuple[int, JoinedBy]]]:
    """Each model call joined to a message the agent wrote, by its span id: the messages' seqs and how each was
    joined."""
    wrote: dict[str, list[tuple[int, JoinedBy]]] = {}
    for event in world.events():
        if event.actor is Actor.AGENT and isinstance(event.after, MessageSnapshot):
            joined = trace_of(event, world)
            if joined.model_call is not None and joined.joined_by is not None:
                wrote.setdefault(joined.model_call.span_id, []).append((event.seq, joined.joined_by))
    return wrote


def _row(state: Path, entry: Logged, children: list[str]) -> RunRow:
    scenario = session.scenario_of(state, entry.run_id)
    outcome = session.load(state, entry.run_id) if entry.finished else None
    kinds = [f.kind for f in outcome.result.findings] if outcome is not None else []
    after_wake: int | None = None
    changed: str | None = None
    account = session.fork_account(state, entry.run_id) if entry.parent_run is not None else None
    if account is not None and session.fork_of(state, entry.run_id) is not None:
        changed = account.summary
    if entry.forked_at is not None:
        with session.reading(state, entry.run_id) as world:
            after_wake = next(
                (e.wake for e in world.events(since=entry.forked_at - 1) if e.seq == entry.forked_at), None
            )
    case = session.case_of(state, entry.run_id)
    return RunRow(
        case=case.name if case is not None else None,
        worlds=case.worlds if case is not None else [],
        run_id=entry.run_id,
        scenario=scenario.name,
        goal=scenario.goal,
        finished=entry.finished,
        stop=outcome.record.stop if outcome is not None else None,
        verdict=outcome.result.verdict.kind if outcome is not None else None,
        failed=kinds.count(FindingKind.FAIL),
        to_review=kinds.count(FindingKind.REVIEW),
        parent_run=entry.parent_run,
        forked_at=entry.forked_at,
        forked_after_wake=after_wake,
        forked_ran_on=account.ran_on if account is not None else False,
        changed=changed,
        children=children,
    )


def serve(state: Path, *, port: int, host: str = HOST, prices: Prices | None = None) -> None:
    """Serve the viewer on `host` (127.0.0.1 unless told) until interrupted. A viewer in a container listens on
    0.0.0.0, so the port Docker publishes reaches it."""
    uvicorn.run(create_app(state, prices), host=host, port=port, log_level="warning")
