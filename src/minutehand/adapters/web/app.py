"""The viewer: a read-only JSON API over a state directory, and the one page that draws it.

    minutehand view [--state DIR] [--port N]      serves both on 127.0.0.1 only

    GET /                                   viewer.html: inline CSS, JS and SVG, nothing fetched from elsewhere
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

A case (standing worlds opened under one case label) is one run here: its worlds are read as one log and are not
listed on their own, and a standing world no call reached (a probe) is not listed.
    GET /api/runs/{run_id}/traces/{trace_id}   the agent's spans of one trace, as the run received them

Every world file is opened read-only (`session.reading`), so a run another process is still writing is
read as of its last commit, and the viewer can never change or lock a run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from minutehand import session
from minutehand.adapters.web.responses import (
    CallsResponse,
    DrawnWait,
    EventsResponse,
    FellDue,
    FindingsResponse,
    HostTraffic,
    MessageChange,
    MessageLine,
    MessagesResponse,
    ModelCallsResponse,
    ModelTrafficResponse,
    ObligationsResponse,
    Refusal,
    RunResponse,
    RunRow,
    RunsResponse,
    ScorecardResponse,
    TraceResponse,
    TrafficCall,
    WakesResponse,
    WrittenBy,
    explained,
)
from minutehand.application.checkpoint import CHECKPOINT, read_checkpoint
from minutehand.application.forks import summary
from minutehand.application.model_calls import JoinedBy, is_model_call, model_call, trace_of
from minutehand.application.refusals import RunRefused
from minutehand.application.steps import STEP
from minutehand.checks._waits import chases, ended_at
from minutehand.checks.runner import view_of
from minutehand.domain.checks import FindingKind
from minutehand.domain.inboxes import item_words
from minutehand.domain.scenario import Model
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    ItemStatus,
    MessageSnapshot,
    Operation,
    WorldEvent,
)
from minutehand.session import Logged

PAGE = Path(__file__).with_name("viewer.html")
WRITES = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})
HOST = "127.0.0.1"


def _json(model: Model, status: int = 200) -> Response:
    return Response(model.model_dump_json(), status_code=status, media_type="application/json")


def create_app(state: Path) -> Starlette:
    """The viewer over one state directory."""

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
                        if e.entity not in (CHECKPOINT, STEP) and e.entity.kind is not EntityKind.DUE
                    ]
                )
            )

    def calls(run_id: str) -> Response:
        with session.reading(state, run_id) as world:
            return _json(CallsResponse(calls=world.calls()))

    def obligations(run_id: str) -> Response:
        scenario = session.scenario_of(state, run_id)
        with session.reading(state, run_id) as world:
            last = read_checkpoint(world)
            view = view_of(
                scenario,
                world.events(),
                session.wakes_of(state, run_id, world),
                world.replies(),
                withdrawn=last.withdrawn if last is not None else [],
            )
        due = {c.obligation.key: c.expiries for c in chases(view, ended_at(view))}
        return _json(
            ObligationsResponse(
                obligations=[
                    DrawnWait(
                        obligation=o,
                        fell_due=[
                            FellDue(
                                at=e.expired,
                                until=e.touched_at or e.closes,
                                followed_up=e.touch is not None,
                                late=e.late,
                            )
                            for e in due.get(o.key, [])
                        ],
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
        names = {p.email: p.name for p in scenario.people}
        with session.reading(state, run_id) as world:
            said: dict[EntityRef, str] = {}
            lines: list[MessageLine] = []
            for event in world.events():
                after = event.after
                if isinstance(after, InboxItemSnapshot):
                    lines.append(_item_line(event, after, names))
                    continue
                if not isinstance(after, MessageSnapshot) or event.operation not in WRITES:
                    continue
                before = said.get(event.entity)
                said[event.entity] = after.text
                if event.operation is Operation.UPDATE and before == after.text:
                    continue  # a change that left the text as it was: a reaction, a button redrawn
                change = (
                    MessageChange.DELETED
                    if event.operation is Operation.DELETE
                    else MessageChange.EDITED
                    if event.operation is Operation.UPDATE
                    else MessageChange.SENT
                )
                joined = trace_of(event, world) if event.actor is Actor.AGENT else None
                lines.append(
                    MessageLine(
                        seq=event.seq,
                        at=event.sim_time,
                        wake=event.wake,
                        actor=event.actor,
                        change=change,
                        to=[names[e] if e in names else e for e in after.recipient_emails],
                        text=after.text,
                        before=before if change is MessageChange.EDITED else None,
                        thread=after.thread_of is not None,
                        written_by=WrittenBy(
                            span_id=joined.model_call.span_id, model=joined.model_call.model, joined_by=joined.joined_by
                        )
                        if joined is not None and joined.model_call is not None and joined.joined_by is not None
                        else None,
                    )
                )
        return _json(MessagesResponse(messages=lines))

    def model_traffic(run_id: str) -> Response:
        with session.reading(state, run_id) as world:
            wrote: dict[str, list[tuple[int, JoinedBy]]] = {}
            for event in world.events():
                if event.actor is Actor.AGENT and isinstance(event.after, MessageSnapshot):
                    joined = trace_of(event, world)
                    if joined.model_call is not None and joined.joined_by is not None:
                        wrote.setdefault(joined.model_call.span_id, []).append((event.seq, joined.joined_by))
            calls = [
                TrafficCall(
                    span_id=c.span_id,
                    trace_id=c.trace_id,
                    step=s.wake,
                    started=c.started,
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

    def trace(request: Request) -> Response:
        run_id, trace_id = request.path_params["run_id"], request.path_params["trace_id"]
        assert isinstance(run_id, str) and isinstance(trace_id, str)
        try:
            with session.reading(state, run_id) as world:
                return _json(TraceResponse(trace_id=trace_id, spans=world.spans(trace_id=trace_id.lower())))
        except RunRefused as e:
            return _json(Refusal(error=str(e)), status=404)

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
            Route("/api/runs/{run_id}/traces/{trace_id}", trace),
        ]
    )


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


def serve(state: Path, *, port: int) -> None:
    """Serve the viewer on 127.0.0.1 until interrupted."""
    uvicorn.run(create_app(state), host=HOST, port=port, log_level="warning")


def _item_line(event: WorldEvent, item: InboxItemSnapshot, names: dict[str, str]) -> MessageLine:
    """An item in the agent's own product as a line of what was said: the ask, the decision, a refusal of it, or the
    agent taking it back."""
    who = names[item.waits_on] if item.waits_on in names else item.waits_on
    if event.actor is Actor.PERSON:
        change = MessageChange.DECIDED if item.status is ItemStatus.DECIDED else MessageChange.REFUSED
    else:
        change = MessageChange.WITHDRAWN if item.status is ItemStatus.WITHDRAWN else MessageChange.ASKED
    return MessageLine(
        seq=event.seq,
        at=event.sim_time,
        wake=event.wake,
        actor=event.actor,
        change=change,
        to=[who],
        text=item.summary,
        before=None,
        thread=False,
        words=item_words(item, event.actor, who),
    )
