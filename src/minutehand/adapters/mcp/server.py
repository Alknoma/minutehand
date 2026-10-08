"""The tools a coding agent calls: play a scenario against its agent, read what went wrong with the design
that fixes it, and rerun from a moment with something changed.

    minutehand mcp [--state DIR]       serves them over stdio

Every tool reads and writes the state directory through `session`, the same as the command line, so a run
started here is listed by `minutehand runs` and the reverse. One proxy runs per process, so one run at a
time: a second `run_scenario` or `rerun_from` while one plays is refused, not queued.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from minutehand import session
from minutehand.adapters.mcp.results import (
    CitedEvent,
    Evidence,
    FindingCounts,
    FindingList,
    ListedRun,
    NotAScenario,
    NumberedFinding,
    OutboundCall,
    OutboundCalls,
    PlayedRun,
    QueryAnswer,
    ReadModelSchema,
    RecordedHttp,
    RunListing,
    RunsPlayed,
    ScenarioFile,
    ScenarioListing,
)
from minutehand.adapters.query import reader as read_model
from minutehand.adapters.query.reader import QueryRefused
from minutehand.adapters.query.schema import VERSION as READ_MODEL_VERSION
from minutehand.adapters.query.schema import VIEWS
from minutehand.adapters.query.trace import Explained, Traced, TraceFilter
from minutehand.adapters.query.trace import explain as explained
from minutehand.adapters.query.trace import trace as traced
from minutehand.application.files import FileRefused, load_agent, load_prices, load_scenario
from minutehand.application.model_calls import EventTrace, caller_of, trace_of
from minutehand.application.refusals import RunRefused
from minutehand.checks.patterns import pattern
from minutehand.checks.runner import RunResult, stability
from minutehand.domain.checks import FindingKind
from minutehand.domain.experiment import Fork, Override
from minutehand.domain.world import Exchange, WorldEvent
from minutehand.session import Outcome

SCENARIO_SUFFIXES = (".yaml", ".yml", ".json")

INSTRUCTIONS = """\
Minutehand runs an AI agent through simulated days against fake Slack, trackers and document stores, with
simulated people who answer late or never, then checks how well the agent followed through.

The loop: list_scenarios to find a scenario file; run_scenario with it, the agent file and the command that
starts the agent; list_findings for what went wrong; show_evidence for one finding's world events, HTTP calls
and the design pattern that fixes it; list_outbound_calls for what it called beyond the fakes (an email API, a
search), and what to declare for a host it was refused; change the agent's code; rerun_from a checkpoint (or run_scenario again)
to see whether the finding is gone. list_runs shows every run and which were forked from which.

To see everything the agent did, not only what a finding cites: trace lists its acts in order (messages, reads,
memory, calls, model calls); explain takes one event's seq and gives the chain around it; query_run runs your own
read-only SQL over the run's views, which schema lists with every column.
"""


class OneRunAtATime:
    """The process holds one proxy, so it plays one run at a time; a second is refused while the first plays."""

    def __init__(self) -> None:
        self._playing: str | None = None

    def claim(self, what: str) -> None:
        if self._playing is not None:
            raise ToolError(
                f"a run is already playing in this server ({self._playing}); only one can run at a time. "
                "Wait for it to return, then call again."
            )
        self._playing = what

    def release(self) -> None:
        self._playing = None


def build(state: Path) -> FastMCP:
    """The MCP server over one state directory."""
    server = FastMCP("minutehand", instructions=INSTRUCTIONS)
    runs = OneRunAtATime()

    @server.tool(
        description=(
            "List the scenario files under a directory (searched recursively; .yaml, .yml, .json). A scenario "
            "is a goal handed to the agent, the simulated people (who answers, how late, who never does) and "
            "the expectations a run is graded on. Files that are not scenarios, such as agent files, are listed "
            "separately with the reason. Relative paths are resolved from the server's working directory."
        )
    )
    def list_scenarios(directory: str) -> ScenarioListing:
        root = Path(directory)
        if not root.is_dir():
            raise ToolError(f"{directory} is not a directory")
        scenarios: list[ScenarioFile] = []
        others: list[NotAScenario] = []
        for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SCENARIO_SUFFIXES):
            try:
                scenario = load_scenario(path)
            except FileRefused as e:
                others.append(NotAScenario(path=str(path), reason=_first_lines(str(e))))
                continue
            scenarios.append(
                ScenarioFile(
                    path=str(path),
                    name=scenario.name,
                    goal=scenario.goal,
                    people=scenario.people,
                    expectations=scenario.expect,
                )
            )
        return ScenarioListing(directory=str(root), scenarios=scenarios, not_scenarios=others)

    @server.tool(
        description=(
            "Play a scenario against an agent from the start, on a simulated clock, and grade the run. "
            "`scenario` and `agent` are paths to the scenario file and the agent file (how Minutehand reaches "
            "the agent). `command`, when given, is the argv that starts the agent's own program; Minutehand "
            "starts it with HTTPS_PROXY and CA variables set, so its calls to Slack and the trackers reach the "
            "fakes, and stops it when the run ends. Without `command` the agent must already be running. "
            "`samples` > 1 plays the scenario that many times and reports how many passed. Returns each run's "
            "id, why it stopped, its verdict (passed, failed, or unfinished: no check failed but the agent did not finish), counts of findings by kind, the scorecard, and the checkpoints rerun_from can "
            "restart it from. Takes seconds to minutes; only one run plays at a time."
        )
    )
    async def run_scenario(scenario: str, agent: str, command: list[str] | None = None, samples: int = 1) -> RunsPlayed:
        runs.claim(f"scenario {scenario}")
        try:
            loaded = load_scenario(Path(scenario))
            agent_file = load_agent(Path(agent))
            outcomes = await session.play(loaded, agent_file, state=state, samples=samples, command=command)
        except (RunRefused, FileRefused, OSError) as e:
            raise ToolError(f"the run could not be performed: {e}") from e
        finally:
            runs.release()
        return _played(state, outcomes, sampled=samples > 1)

    @server.tool(
        description=(
            "The run's verdict and every finding the checks raised on it, numbered for show_evidence. kind 'fail' means "
            "the agent did something wrong; 'review' means it may have. Each names the pattern: the known "
            "failure of proactive agents it is an instance of. `blocked` lists checks that could not run."
        )
    )
    def list_findings(run_id: str) -> FindingList:
        outcome = _load(state, run_id)
        return FindingList(
            run_id=run_id,
            verdict=outcome.result.verdict,
            findings=_numbered(outcome.result),
            blocked=outcome.result.blocked,
        )

    @server.tool(
        description=(
            "The evidence for one finding (its number from list_findings): every world event it cites, what was "
            "created or changed, by whom (agent, person, scenario) and when on the simulated clock; the HTTP "
            "call behind each event (method, host, path, status, and bodies when they were stored) with the "
            "caller's trace id when the call carried a traceparent; the agent's own model call that led to "
            "each event, when the run received its telemetry (the model, the messages it was sent and answered, "
            "token counts); the wake it happened in; and the full pattern: what goes wrong and the design a "
            "proactive agent uses to avoid it. Use the pattern's design as the fix to make in the agent."
        )
    )
    def show_evidence(run_id: str, finding: int) -> Evidence:
        outcome = _load(state, run_id)
        numbered = _numbered(outcome.result)
        if not 1 <= finding <= len(numbered):
            raise ToolError(f"run {run_id} has {len(numbered)} finding(s), numbered from 1; there is no {finding}")
        chosen = numbered[finding - 1]
        with session.reading(state, run_id) as world:
            cited = [e for e in world.events() if e.seq in set(chosen.evidence)]
            traces = [trace_of(e, world) for e in cited]
            received = len(world.spans())
        wake_numbers = {e.wake for e in cited} | ({chosen.wake} if chosen.wake is not None else set())
        return Evidence(
            run_id=run_id,
            finding=chosen,
            events=[_cited(e, t) for e, t in zip(cited, traces, strict=True)],
            wakes=[w for w in outcome.record.wakes if w.index in wake_numbers],
            telemetry=_received(received),
            pattern=pattern(chosen.pattern) if chosen.pattern is not None else None,
        )

    @server.tool(
        description=(
            "Rerun a finished run from one of its checkpoints with something changed, and grade the new run. "
            "The world up to `at_seq` is shared with the original, which is left untouched. `at_seq` must be "
            "one of the run's checkpoints (returned by run_scenario; a wrong one is answered with the list). "
            "`changes` is a list of overrides, each with a `kind`: person_change (a person replies differently "
            "from here on), ticket_edit (a ticket is in another state or with another person), deadline_shift, "
            "prompt_patch (text appended to or replaced in the agent's prompt on the wire), model_swap (the "
            "agent's model calls go to another model). The agent needs a way to restore its own state (state "
            "hooks in its agent file) to resume from a checkpoint. `command` starts the agent as in "
            "run_scenario."
        )
    )
    async def rerun_from(
        run_id: str,
        at_seq: int,
        changes: list[Override],
        command: list[str] | None = None,
        samples: int = 1,
    ) -> RunsPlayed:
        _load(state, run_id)
        points = session.fork_points(state, run_id)
        if at_seq not in {p.seq for p in points}:
            listed = ", ".join(f"{p.seq} (after wake {p.wake})" for p in points)
            raise ToolError(f"seq {at_seq} is not a checkpoint of run {run_id}; its checkpoints are: {listed}")
        runs.claim(f"a rerun of {run_id}")
        try:
            fork = Fork(parent_run=run_id, at_seq=at_seq, overrides=changes, samples=samples)
            outcomes = await session.fork(run_id, fork, state=state, command=command)
        except (RunRefused, OSError) as e:
            raise ToolError(f"the rerun could not be performed: {e}") from e
        finally:
            runs.release()
        return _played(state, outcomes, sampled=samples > 1)

    @server.tool(
        description=(
            "What the run called beyond the faked services: every host no provider claims, with how it was "
            "declared in the agent file (acknowledge: answered here and never sent; pass_through: sent to the real "
            "host; replay: answered from an earlier run's recording) or that nobody declared it and it was "
            "refused, and each captured call with its redacted request and answer. A replayed answer says so "
            "(`captured.answered_by` recording, `replayed_from`). A send read as a message lists the events it wrote."
        )
    )
    def list_outbound_calls(run_id: str) -> OutboundCalls:
        outcome = _load(state, run_id)
        with session.reading(state, run_id) as world:
            recorded = [c for c in world.calls() if c.exchange.captured is not None]
        return OutboundCalls(
            run_id=run_id,
            hosts=outcome.record.outbound,
            calls=[
                OutboundCall(
                    wake=c.wake,
                    at=c.sim_time,
                    call=_http(c.exchange),
                    events=list(range(c.first_seq, c.last_seq + 1)),
                )
                for c in recorded
            ],
        )

    @server.tool(
        description=(
            "Every finished run in the state directory, oldest first, with why it stopped, its verdict, "
            "and its parent and children: a rerun is a child of the run it was forked from. A case (standing "
            "worlds opened under one case label) is one run, its worlds named in `worlds`; a standing world no "
            "call reached is not listed."
        )
    )
    def list_runs() -> RunListing:
        finished = session.listed(state)
        children: dict[str, list[str]] = {o.record.run_id: [] for o in finished}
        for outcome in finished:
            parent = outcome.record.parent_run
            if parent is not None and parent in children:
                children[parent].append(outcome.record.run_id)
        return RunListing(
            runs=[
                ListedRun(
                    run_id=o.record.run_id,
                    scenario=o.record.scenario,
                    stop=o.record.stop,
                    stopped_at=o.record.ended_at,
                    verdict=o.result.verdict,
                    findings=_counts(o.result),
                    parent_run=o.record.parent_run,
                    forked_at=o.record.forked_at,
                    children=children[o.record.run_id],
                    fork=session.fork_account(state, o.record.run_id),
                    worlds=o.record.worlds,
                )
                for o in finished
            ]
        )

    @server.tool(
        description=(
            "The views of a run's read model, each with every column, its type and what it holds, and the read model's "
            "version. Read it before writing SQL for query_run. The views: run (the run read; a fork sees its parent's "
            "record up to its checkpoint), people, events (the whole log), actions (every act of the agent in order), "
            "messages (with is_ask, is_follow_up, ask_seq, answers_seq from the ledger of waits), recipients (each "
            "person a message reached, in their local time and working hours), calls (every HTTP/gRPC/WebSocket "
            "exchange, bodies decoded), wakes, dispatch (what fell due and how it left), memory (the agent's store over "
            "time), stored, replies (what people said and decided, and how it was written), model_calls (tokens, cost "
            "only from declared prices), findings and evidence, spans (the agent's telemetry)."
        )
    )
    def schema() -> ReadModelSchema:
        return ReadModelSchema(version=READ_MODEL_VERSION, views=list(VIEWS))

    @server.tool(
        description=(
            "Run one read-only SQL SELECT (or WITH ... SELECT) over a run's read model and answer its rows. `run_id` "
            "is a run's or a fork's id (or the start of one). Every time is UTC text like 2026-08-24T10:00:00.000Z, "
            "so compare as text or with julianday(); JSON columns read with json_each() and json_extract(). At most "
            "`limit` rows (1 to 1000, default 100) after `offset`; `more` says rows follow, and `next_offset` is the "
            "offset to ask for next. A write, a PRAGMA or more than one statement is refused. `prices` is a path to a "
            "prices file, for model_calls.cost. Call schema for the views and columns."
        )
    )
    def query_run(run_id: str, sql: str, limit: int = 100, offset: int = 0, prices: str | None = None) -> QueryAnswer:
        with _model(state, run_id, prices) as (found, db):
            page = read_model.query(db, sql, limit=limit, offset=offset)
        return QueryAnswer(
            run_id=found,
            columns=page.columns,
            rows=page.rows,
            offset=page.offset,
            more=page.more,
            next_offset=page.offset + len(page.rows) if page.more else None,
        )

    @server.tool(
        description=(
            "Everything the agent under test did in a run, in the order it did it: each message it sent (to whom, the "
            "words), each read, each write to the world, its memory gets and puts, items it stored, the next wake it "
            "marked, calls that wrote nothing (a refused host, a declared hook) and its model calls with tokens. "
            "Rows of the `actions` view. Narrow with `person` (a people key: messages to them), `provider`, `kind` "
            "(message, write, read, memory, stored, next_wake, call, model_call), `since` and `until` (simulated "
            "time, ISO 8601, inclusive) and `wake`. Each row's seq, call_id or span_id leads to the whole of it: "
            "explain(seq), or query_run over messages, calls, model_calls."
        )
    )
    def trace(
        run_id: str,
        person: str | None = None,
        provider: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        wake: int | None = None,
        prices: str | None = None,
    ) -> Traced:
        wanted = TraceFilter(person=person, provider=provider, kind=kind, since=since, until=until, wake=wake)
        with _model(state, run_id, prices) as (found, db):
            return traced(db, found, wanted)

    @server.tool(
        description=(
            "Why one event happened and what came of it, by its seq (from trace, show_evidence or a finding's "
            "evidence). Before it: the wake it happened in, why that wake began (reason, and what fell due), the "
            "people's replies that landed as it began, what the agent read in that wake first (reads, memory gets, "
            "GET calls), the earlier message it answers (the agent's ask a person's reply answers, the ask a follow-up "
            "chases, or the last person's message in the same conversation), the model call that wrote it and the "
            "HTTP call that made it. After it: replies people gave to it, the agent's follow-ups on it, findings "
            "citing it, and the agent's next acts in the same wake. Every part is a row of a view schema lists."
        )
    )
    def explain(run_id: str, seq: int) -> Explained:
        with _model(state, run_id, None) as (found, db):
            return explained(db, found, seq)

    return server


@contextmanager
def _model(state: Path, run: str, prices: str | None) -> Iterator[tuple[str, sqlite3.Connection]]:
    try:
        run_id = read_model.resolve(state, run)
        db = read_model.open_model(state, run_id, load_prices(Path(prices)) if prices is not None else None)
    except (RunRefused, FileRefused) as e:
        raise ToolError(f"{e}; list_runs shows the runs") from e
    try:
        yield run_id, db
    except QueryRefused as e:
        raise ToolError(str(e)) from e
    finally:
        db.close()


def _load(state: Path, run_id: str) -> Outcome:
    try:
        return session.load(state, run_id)
    except RunRefused as e:
        raise ToolError(f"{e}; list_runs shows the finished runs") from e


def _played(state: Path, outcomes: Sequence[Outcome], *, sampled: bool) -> RunsPlayed:
    return RunsPlayed(
        runs=[
            PlayedRun(
                run_id=o.record.run_id,
                scenario=o.record.scenario,
                parent_run=o.record.parent_run,
                forked_at=o.record.forked_at,
                stop=o.record.stop,
                stopped_at=o.record.ended_at,
                verdict=o.result.verdict,
                findings=_counts(o.result),
                blocked=o.result.blocked,
                scorecard=o.result.effectiveness,
                checkpoints=session.fork_points(state, o.record.run_id),
                fork=session.fork_account(state, o.record.run_id),
            )
            for o in outcomes
        ],
        stability=stability([o.result for o in outcomes]) if sampled else None,
    )


def _counts(result: RunResult) -> FindingCounts:
    kinds = [f.kind for f in result.findings]
    return FindingCounts(
        fail=kinds.count(FindingKind.FAIL),
        review=kinds.count(FindingKind.REVIEW),
        informational=kinds.count(FindingKind.INFORMATIONAL),
    )


def _numbered(result: RunResult) -> list[NumberedFinding]:
    return [
        NumberedFinding(
            number=i,
            check=f.check,
            kind=f.kind,
            severity=f.severity,
            message=f.message,
            at=f.at,
            wake=f.wake,
            evidence=f.evidence,
            pattern=f.pattern,
            pattern_title=pattern(f.pattern).title if f.pattern is not None else None,
        )
        for i, f in enumerate(result.findings, start=1)
    ]


def _received(count: int) -> str:
    if count == 0:
        return (
            "No telemetry was received from the agent in this run, so no event is joined to a model call. An agent "
            "exporting OpenTelemetry over OTLP/HTTP to the endpoint Minutehand hands it, or a run with "
            "--record-model-calls, gives one."
        )
    return f"{count} span(s) of the agent's own telemetry were received in this run."


def _cited(event: WorldEvent, trace: EventTrace) -> CitedEvent:
    return CitedEvent(
        seq=event.seq,
        wake=event.wake,
        at=event.sim_time,
        actor=event.actor,
        operation=event.operation,
        entity=event.entity,
        after=event.after,
        call=_http(event.exchange) if event.exchange is not None else None,
        model_call=trace.model_call,
        joined_by=trace.joined_by,
        agent_spans=[s.span.name for s in ([trace.caller] if trace.caller is not None else []) + trace.ancestors],
    )


def _http(exchange: Exchange) -> RecordedHttp:
    return RecordedHttp(
        method=exchange.method,
        host=exchange.host,
        path=exchange.path,
        status=exchange.status,
        request_body=exchange.request_body,
        response_body=exchange.response_body,
        trace_id=caller[0] if (caller := caller_of(exchange.traceparent)) is not None else None,
        captured=exchange.captured,
    )


def _first_lines(text: str, lines: int = 3) -> str:
    return "\n".join(text.splitlines()[:lines])


def serve(state: Path) -> None:
    """Serve the tools over stdio until the client closes the connection."""
    build(state).run("stdio")
