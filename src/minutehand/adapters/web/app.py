"""The viewer: a read-only JSON API over a state directory, and the one page that draws it.

    minutehand view [--state DIR] [--port N]      serves both on 127.0.0.1 only

    GET /                                   viewer.html, which loads its script, its styles and the vendored
                                            libraries from /static/ on this server and nothing from elsewhere
    GET /static/...                         those files (`adapters/web/static/`), each library's licence beside it
    GET /api/runs                           every run, finished or still running, with the fork tree
    GET /api/runs/{run_id}                  the scenario, the record once finished, the checkpoints, and for a
                                            fork what it changed and how it differs from its parent
    GET /api/runs/{run_id}/wakes
    GET /api/runs/{run_id}/events           the world's log, without the run loop's own checkpoints
    GET /api/runs/{run_id}/calls            every HTTP call the proxy recorded
    GET /api/runs/{run_id}/obligations      what the world was waiting on (`checks.ledger`), and when each fell due
    GET /api/runs/{run_id}/findings         each with its pattern, once the run is checked
    GET /api/runs/{run_id}/scorecard
    GET /api/runs/{run_id}/model-calls      for each event a finding cites: the agent's spans and model call
    GET /api/runs/{run_id}/messages         every message sent, rewritten or deleted, to whom, what it said
    GET /api/runs/{run_id}/model-traffic    every model call by step, what message each wrote, and the model
                                            hosts reached on tunnels never opened, one line per host
    GET /api/runs/{run_id}/model-calls/{span_id}   one model call: what it was asked and answered, what it wrote
    GET /api/runs/{run_id}/steps            each wake or step's real-time extent and how much it holds
    GET /api/runs/{run_id}/steps/{step}/spans      the agent's spans placed in one step, for a waterfall

A case (standing worlds opened under one case label) is one run here: its worlds are read as one log and are not
listed on their own, and a standing world no call reached (a probe) is not listed.
    GET /api/runs/{run_id}/traces/{trace_id}   the agent's spans of one trace, as the run received them

What the page reads to draw a run of any length (`reading.py`); the paths marked (read model) are rows of the run's
read model (`adapters/query`, `docs/querying.md`), so the page and `minutehand query` show the same facts:
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
from datetime import datetime
from pathlib import Path

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
    CallsResponse,
    DrawnWait,
    EventsResponse,
    FindingsResponse,
    HostTraffic,
    MessagesResponse,
    ModelCallResponse,
    ModelCallsResponse,
    ModelTrafficResponse,
    ObligationsResponse,
    Refusal,
    RunResponse,
    RunRow,
    RunsResponse,
    ScorecardResponse,
    SpanBar,
    SpanResponse,
    StepActivity,
    StepSpansResponse,
    StepsResponse,
    TimelineResponse,
    TraceResponse,
    TrafficCall,
    WakesResponse,
    explained,
)
from minutehand.application.checkpoint import CHECKPOINT
from minutehand.application.forks import summary
from minutehand.application.model_calls import JoinedBy, is_model_call, model_call, trace_of
from minutehand.application.refusals import RunRefused
from minutehand.application.steps import STEP
from minutehand.checks.runner import RunResult, view_of
from minutehand.domain.checks import FindingKind
from minutehand.domain.prices import Prices
from minutehand.domain.scenario import Model
from minutehand.domain.telemetry import SpanSource
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot
from minutehand.ports.store import Store
from minutehand.session import Logged

PAGE = Path(__file__).with_name("viewer.html")
STATIC = Path(__file__).with_name("static")
HOST = "127.0.0.1"
KEEP = 8
"""Timelines of finished runs kept built, the latest read first: a finished run's log no longer changes."""


def _json(model: Model, status: int = 200) -> Response:
    return Response(model.model_dump_json(), status_code=status, media_type="application/json")


def create_app(state: Path, prices: Prices | None = None) -> Starlette:
    """The viewer over one state directory; `prices` gives each model call a cost, as `minutehand query --prices`."""

    def page(_: Request) -> Response:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

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
            )
        )

    def wakes(run_id: str) -> Response:
        with session.reading(state, run_id) as world:
            return _json(WakesResponse(wakes=session.wakes_of(state, run_id, world)))

    def events(run_id: str) -> Response:
        with session.reading(state, run_id) as world:
            return _json(
                EventsResponse(
                    events=[
                        e
                        for e in world.events()
                        if e.entity not in (CHECKPOINT, STEP)
                        and e.entity.kind
                        not in (EntityKind.DUE, EntityKind.PENDING, EntityKind.MEMORY, EntityKind.NEXT_WAKE)
                    ]
                )
            )

    def calls(run_id: str) -> Response:
        with session.reading(state, run_id) as world:
            return _json(CallsResponse(calls=world.calls()))

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
        return _json(
            FindingsResponse(
                finished=True,
                verdict=result.verdict,
                findings=explained(result),
                blocked=result.blocked,
                notes=result.notes,
                simulation=result.simulation,
            )
        )

    def scorecard(run_id: str) -> Response:
        if not session.find(state, run_id).finished:
            return _json(ScorecardResponse(scorecard=None))
        return _json(ScorecardResponse(scorecard=session.load(state, run_id).result.effectiveness))

    def model_calls(run_id: str) -> Response:
        cited = (
            {seq for f in session.load(state, run_id).result.findings for seq in f.evidence}
            if session.find(state, run_id).finished
            else set()
        )
        with session.reading(state, run_id) as world:
            return _json(
                ModelCallsResponse(
                    received=len(world.spans()),
                    forward_failures=world.forward_failures(),
                    events=[trace_of(e, world) for e in world.events() if e.seq in cited],
                )
            )

    def messages(run_id: str) -> Response:
        scenario = session.scenario_of(state, run_id)
        with session.reading(state, run_id) as world:
            return _json(MessagesResponse(messages=reading.message_lines(world.events(), scenario, world)))

    def model_traffic(run_id: str) -> Response:
        with session.reading(state, run_id) as world:
            wrote = _writers(world)
            calls = [
                TrafficCall(
                    span_id=c.span_id,
                    trace_id=c.trace_id,
                    step=s.wake,
                    started=c.started,
                    ended=c.ended,
                    model=c.model,
                    input_tokens=c.input_tokens,
                    output_tokens=c.output_tokens,
                    wrote=[seq for seq, _ in wrote[c.span_id]] if c.span_id in wrote else [],
                    joined_by=wrote[c.span_id][0][1] if c.span_id in wrote else None,
                )
                for s in sorted(world.spans(), key=lambda s: s.span.start)
                if is_model_call(s)
                for c in [model_call(s)]
            ]
            hosts: dict[str, HostTraffic] = {}
            connections: dict[str, set[str]] = {}
            for call in world.calls():
                tunnel = call.exchange.tunnelled
                if tunnel is None:
                    continue
                host = call.exchange.host
                had = (
                    hosts[host]
                    if host in hosts
                    else HostTraffic(host=host, calls=0, connections=0, bytes_sent=0, bytes_received=0)
                )
                connections.setdefault(host, set()).add(tunnel.connection)
                hosts[host] = had.model_copy(
                    update={
                        "calls": had.calls + 1,
                        "connections": len(connections[host]),
                        "bytes_sent": had.bytes_sent + tunnel.bytes_sent,
                        "bytes_received": had.bytes_received + tunnel.bytes_received,
                    }
                )
        return _json(ModelTrafficResponse(calls=calls, hosts=list(hosts.values())))

    def steps(run_id: str) -> Response:
        with session.reading(state, run_id) as world:
            events = [e for e in world.events() if e.entity not in (CHECKPOINT, STEP)]
            spans = world.spans()
        moments: dict[int, list[datetime]] = {}
        for event in events:
            moments.setdefault(event.wake, []).append(event.wall_time)
        for stored in spans:
            moments.setdefault(stored.wake, []).extend((stored.span.start, stored.span.end))
        return _json(
            StepsResponse(
                steps=[
                    StepActivity(
                        step=step,
                        began=min(moments[step]),
                        ended=max(moments[step]),
                        events=sum(1 for e in events if e.wake == step),
                        spans=sum(1 for s in spans if s.wake == step),
                        model_calls=sum(1 for s in spans if s.wake == step and is_model_call(s)),
                    )
                    for step in sorted(moments)
                ]
            )
        )

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

    def trace(request: Request) -> Response:
        run_id, trace_id = request.path_params["run_id"], request.path_params["trace_id"]
        assert isinstance(run_id, str) and isinstance(trace_id, str)
        try:
            with session.reading(state, run_id) as world:
                return _json(TraceResponse(trace_id=trace_id, spans=world.spans(trace_id=trace_id.lower())))
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
            drawn = reading.timeline(state, run_id, world, result)
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
                found = reading.event_detail(world, seq, scenario, result)
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
            return _json(reading.people(world, scenario))

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
            Route("/api/runs/{run_id}/events", one_run(events)),
            Route("/api/runs/{run_id}/calls", one_run(calls)),
            Route("/api/runs/{run_id}/obligations", one_run(obligations)),
            Route("/api/runs/{run_id}/findings", one_run(findings)),
            Route("/api/runs/{run_id}/scorecard", one_run(scorecard)),
            Route("/api/runs/{run_id}/model-calls", one_run(model_calls)),
            Route("/api/runs/{run_id}/messages", one_run(messages)),
            Route("/api/runs/{run_id}/model-traffic", one_run(model_traffic)),
            Route("/api/runs/{run_id}/model-calls/{span_id}", one_model_call),
            Route("/api/runs/{run_id}/steps", one_run(steps)),
            Route("/api/runs/{run_id}/steps/{step:int}/spans", step_spans),
            Route("/api/runs/{run_id}/traces/{trace_id}", trace),
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
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
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
    if entry.parent_run is not None:
        asked = session.fork_of(state, entry.run_id)
        changed = summary(asked, session.scenario_of(state, entry.parent_run)) if asked is not None else None
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


def serve(state: Path, *, port: int, prices: Prices | None = None) -> None:
    """Serve the viewer on 127.0.0.1 until interrupted."""
    uvicorn.run(create_app(state, prices), host=HOST, port=port, log_level="warning")
