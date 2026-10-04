"""`minutehand`: run a scenario against an agent, read a run's findings, fork a run, list the runs.

    minutehand run <scenario.yaml> --agent <agent.yaml> [--state DIR] [--samples N] [--judge] [--json] [-- <command...>]
    minutehand findings <run_id> [--state DIR] [--json]
    minutehand fork <run_id> --at <seq> --changes <fork.yaml> [--state DIR] [--judge] [--json] [-- <command...>]
    minutehand runs [--state DIR]

A model, for people whose replies it writes and for --judge, is configured by MINUTEHAND_MODEL,
MINUTEHAND_MODEL_API_KEY and MINUTEHAND_MODEL_BASE_URL.

Exit codes: 0 when no finding is a failure, 1 when any is (with samples, when any sample failed), 2 when
the run could not be performed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from minutehand import session
from minutehand.adapters.model.openai_compatible import from_environment as model_from_environment
from minutehand.adapters.telemetry.otel import ENDPOINT_VARIABLE, OtelTelemetry, from_environment
from minutehand.application.files import FileRefused, load_agent, load_fork, load_scenario
from minutehand.application.refusals import RunRefused
from minutehand.checks.patterns import pattern
from minutehand.checks.runner import stability
from minutehand.domain.checks import Effectiveness, Finding, FindingKind, Stability
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Model
from minutehand.ports.model import ModelFailed
from minutehand.session import ForkPoint, Outcome

DEFAULT_STATE = Path(".minutehand")
STATE_VARIABLE = "MINUTEHAND_STATE"

_STOPPED = {
    StopReason.AGENT_DONE: "the agent reported it was done",
    StopReason.WAKE_LIMIT: "the scenario's wake limit was reached",
    StopReason.DEADLINE_PASSED: "the clock reached the scenario's deadline",
    StopReason.NOTHING_PENDING: "nothing more was due and the agent asked for no wake",
    StopReason.AGENT_FAILED: "the agent could not be reached or answered with an error",
}
_KIND_ORDER = (FindingKind.FAIL, FindingKind.REVIEW, FindingKind.INFORMATIONAL)


class Played(Model):
    """What `run` and `fork` print with --json."""

    outcomes: list[Outcome]
    stability: Stability | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minutehand", description="Simulated days for a proactive agent.")
    commands = parser.add_subparsers(dest="command", required=True)

    def state(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--state",
            type=Path,
            default=None,
            help=f"where runs are kept (default ${STATE_VARIABLE} or {DEFAULT_STATE})",
        )

    run = commands.add_parser("run", help="run a scenario against an agent")
    run.add_argument("scenario", type=Path)
    run.add_argument("--agent", type=Path, required=True)
    run.add_argument("--samples", type=int, default=1)
    run.add_argument("--judge", action="store_true", help="also run the checks a model judges")
    run.add_argument("--json", action="store_true")
    state(run)

    findings = commands.add_parser("findings", help="what the checks said about a finished run")
    findings.add_argument("run_id")
    findings.add_argument("--json", action="store_true")
    state(findings)

    fork = commands.add_parser("fork", help="rerun a finished run from a checkpoint with something changed")
    fork.add_argument("run_id")
    fork.add_argument("--at", type=int, required=True, help="the checkpoint's seq (listed by `findings`)")
    fork.add_argument("--changes", type=Path, required=True)
    fork.add_argument("--judge", action="store_true", help="also run the checks a model judges")
    fork.add_argument("--json", action="store_true")
    state(fork)

    listing = commands.add_parser("runs", help="every finished run")
    state(listing)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    command: list[str] | None = None
    if "--" in args_in:
        split = args_in.index("--")
        args_in, command = args_in[:split], args_in[split + 1 :]
        if not command:
            print("minutehand: nothing follows --; give the agent's command or leave -- out", file=sys.stderr)
            return 2
    args = _parser().parse_args(args_in)
    state: Path = args.state or Path(os.environ[STATE_VARIABLE] if STATE_VARIABLE in os.environ else DEFAULT_STATE)
    if command is not None and args.command not in ("run", "fork"):
        print(f"minutehand {args.command}: takes no agent command", file=sys.stderr)
        return 2
    try:
        if args.command == "run":
            return _run(args, state, command)
        if args.command == "fork":
            return _fork(args, state, command)
        if args.command == "findings":
            return _findings(args, state)
        return _runs(state)
    except (RunRefused, FileRefused, ModelFailed, OSError) as e:
        print(f"minutehand: the run could not be performed: {e}", file=sys.stderr)
        return 2


def _telemetry() -> OtelTelemetry | None:
    """OTLP export when its endpoint is set; otherwise none, rather than spans with nowhere to go."""
    return from_environment() if os.environ.get(ENDPOINT_VARIABLE) else None


def _run(args: argparse.Namespace, state: Path, command: list[str] | None) -> int:
    scenario = load_scenario(args.scenario)
    agent = load_agent(args.agent)
    telemetry = _telemetry()
    try:
        outcomes = asyncio.run(
            session.play(
                scenario,
                agent,
                state=state,
                samples=args.samples,
                command=command,
                telemetry=telemetry,
                model=model_from_environment(),
                judge=args.judge,
            )
        )
    finally:
        if telemetry is not None:
            telemetry.shutdown()
    return _report(outcomes, state, as_json=args.json, sampled=args.samples > 1)


def _fork(args: argparse.Namespace, state: Path, command: list[str] | None) -> int:
    changes = load_fork(args.changes, parent_run=args.run_id, at_seq=args.at)
    telemetry = _telemetry()
    try:
        outcomes = asyncio.run(
            session.fork(
                args.run_id,
                changes,
                state=state,
                command=command,
                telemetry=telemetry,
                model=model_from_environment(),
                judge=args.judge,
            )
        )
    finally:
        if telemetry is not None:
            telemetry.shutdown()
    return _report(outcomes, state, as_json=args.json, sampled=changes.samples > 1)


def _findings(args: argparse.Namespace, state: Path) -> int:
    outcome = session.load(state, args.run_id)
    if args.json:
        print(outcome.model_dump_json(indent=2))
    else:
        print(_describe(outcome, session.fork_points(state, args.run_id)))
    return outcome.result.exit_code


def _runs(state: Path) -> int:
    found = session.runs(state)
    if not found:
        print(f"no runs under {state}")
        return 0
    for outcome in found:
        record = outcome.record
        failed = sum(1 for f in outcome.result.findings if f.kind is FindingKind.FAIL)
        parent = f"  forked from {record.parent_run} at seq {record.forked_at}" if record.parent_run else ""
        print(f"{record.run_id}  {record.scenario}  {record.stop.value}  {failed} failed{parent}")
    return 0


def _report(outcomes: list[Outcome], state: Path, *, as_json: bool, sampled: bool) -> int:
    stable = stability([o.result for o in outcomes]) if sampled else None
    if as_json:
        print(Played(outcomes=outcomes, stability=stable).model_dump_json(indent=2))
    else:
        print("\n\n".join(_describe(o, session.fork_points(state, o.record.run_id)) for o in outcomes))
        if stable is not None:
            print(f"\nstability: passed {stable.passed} of {stable.samples} samples")
    if stable is not None:
        return 0 if stable.passed == stable.samples else 1
    return max(o.result.exit_code for o in outcomes)


def _describe(outcome: Outcome, points: list[ForkPoint]) -> str:
    record, result = outcome.record, outcome.result
    lines = [f"run {record.run_id}: {record.scenario}"]
    if record.parent_run is not None:
        lines.append(f"  forked from {record.parent_run} at seq {record.forked_at}")
    lines.append(f"  stopped at {record.ended_at:%Y-%m-%d %H:%M} UTC (simulated) because {_STOPPED[record.stop]}")
    for kind in _KIND_ORDER:
        found = [f for f in result.findings if f.kind is kind]
        if found:
            lines.append(f"\n{kind.value} ({len(found)})")
            lines += [_finding(f) for f in found]
    if not result.findings:
        lines.append("\nno findings")
    if result.blocked:
        lines.append(f"\nblocked: {len(result.blocked)} check(s) could not read their input and did not run")
        lines += [f"  {b}" for b in result.blocked]
    lines.append("\nscorecard")
    lines += [f"  {line}" for line in _scorecard(result.effectiveness)]
    if points:
        lines.append("\ncheckpoints to fork from: " + ", ".join(f"after wake {p.wake} at seq {p.seq}" for p in points))
    return "\n".join(lines)


def _finding(finding: Finding) -> str:
    where = f" (wake {finding.wake})" if finding.wake is not None else ""
    line = f"  {finding.check}: {finding.message}{where}"
    if finding.pattern is not None:
        known = pattern(finding.pattern)
        line += f"\n    pattern {known.key}: {known.title}. {known.design}"
    return line


def _span(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.0f} hours"


def _scorecard(card: Effectiveness) -> list[str]:
    lines = [
        f"expectations met: {card.expectations_met} of {card.expectations_total}",
        f"waits opened: {card.waits_opened}, still open at the end: {card.waits_open_at_end}",
        f"follow-ups due: {card.follow_ups_due}, made: {card.follow_ups_made}, late: {card.follow_ups_late}",
        f"time the agent lost: {_span(card.time_lost)}",
        f"wakes: {card.wakes}, of which changed nothing: {card.idle_wakes}",
        f"messages to people: {card.messages_to_people}",
        f"failed checks: {card.failed_checks}",
    ]
    if card.slowest_follow_up is not None:
        lines.insert(4, f"slowest follow-up: {_span(card.slowest_follow_up)} after its wait expired")
    return lines


if __name__ == "__main__":
    sys.exit(main())
