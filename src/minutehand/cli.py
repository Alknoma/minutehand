"""`minutehand`: run a scenario against an agent, read a run's findings, fork a run, list the runs.

    minutehand run <scenario.yaml> --agent <agent.yaml> [--state DIR] [--samples N] [--judge] [--json] [PROXY] [-- <command...>]
    minutehand findings <run_id> [--state DIR] [--json]
    minutehand fork <run_id> --at <seq> --changes <fork.yaml> [--state DIR] [--judge] [--json] [PROXY] [-- <command...>]
    minutehand env --agent <agent.yaml> --proxy-port N [PROXY] [--format shell|compose] [--service NAME...]
                                                 the environment an agent Minutehand does not start needs
    minutehand runs [--state DIR]
    minutehand mcp [--state DIR]                 the same over MCP, on stdio, for a coding agent
    minutehand view [--state DIR] [--port N]     the runs in a browser, on 127.0.0.1 only

PROXY is where the proxy listens and how the agent reaches it: --proxy-host (default 127.0.0.1; 0.0.0.0 for
an agent in containers), --proxy-port (default: any free port), --agent-proxy-host (the host the agent uses
for it, e.g. host.docker.internal; default the bind host) and --no-proxy HOST, repeated, for hosts the agent
reaches directly.

A model, for people whose replies it writes and for --judge, is configured by MINUTEHAND_MODEL,
MINUTEHAND_MODEL_API_KEY and MINUTEHAND_MODEL_BASE_URL.

Exit codes: 0 when no finding is a failure, 1 when any is (with samples, when any sample failed), 2 when
the run could not be performed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
from collections.abc import Sequence
from datetime import timedelta
from enum import StrEnum
from pathlib import Path

import yaml

from minutehand import session
from minutehand.adapters.mcp import server as mcp_server
from minutehand.adapters.model.openai_compatible import from_environment as model_from_environment
from minutehand.adapters.proxy.trust import BUNDLE
from minutehand.adapters.telemetry.otel import ENDPOINT_VARIABLE, OtelTelemetry, from_environment
from minutehand.adapters.web import app as viewer
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
COMPOSE_CA_PATH = "/etc/minutehand/ca-bundle.pem"
DOCKER_HOST = "host.docker.internal"
VIEW_PORT = 8081
STATE_VARIABLE = "MINUTEHAND_STATE"

_STOPPED = {
    StopReason.AGENT_DONE: "the agent reported it was done",
    StopReason.WAKE_LIMIT: "the scenario's wake limit was reached",
    StopReason.DEADLINE_PASSED: "the clock reached the scenario's deadline",
    StopReason.NOTHING_PENDING: "nothing more was due and the agent asked for no wake",
    StopReason.AGENT_FAILED: "the agent could not be reached or answered with an error",
}
_KIND_ORDER = (FindingKind.FAIL, FindingKind.REVIEW, FindingKind.INFORMATIONAL)


class EnvFormat(StrEnum):
    SHELL = "shell"
    COMPOSE = "compose"


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

    def proxy(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--proxy-host", default="127.0.0.1", help="the address the proxy listens on")
        sub.add_argument("--proxy-port", type=int, default=0, help="the port it listens on (default: any free one)")
        sub.add_argument(
            "--agent-proxy-host", default=None, help="the host the agent reaches the proxy at (default the bind host)"
        )
        sub.add_argument(
            "--no-proxy", action="append", default=[], metavar="HOST", help="a host the agent reaches directly"
        )

    run = commands.add_parser("run", help="run a scenario against an agent")
    run.add_argument("scenario", type=Path)
    run.add_argument("--agent", type=Path, required=True)
    run.add_argument("--samples", type=int, default=1)
    run.add_argument("--judge", action="store_true", help="also run the checks a model judges")
    run.add_argument("--json", action="store_true")
    proxy(run)
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
    proxy(fork)
    state(fork)

    env = commands.add_parser(
        "env", help="print the environment an agent needs when Minutehand does not start it, then run without --"
    )
    env.add_argument("--agent", type=Path, required=True)
    env.add_argument("--format", choices=[f.value for f in EnvFormat], default=EnvFormat.SHELL.value)
    env.add_argument(
        "--service",
        action="append",
        default=[],
        metavar="NAME",
        help="a Compose service the agent runs in (--format compose); repeat for each",
    )
    env.add_argument(
        "--ca-path",
        default=COMPOSE_CA_PATH,
        help=f"where the CA bundle is mounted in each service (--format compose; default {COMPOSE_CA_PATH})",
    )
    proxy(env)
    state(env)

    listing = commands.add_parser("runs", help="every finished run")
    state(listing)

    tools = commands.add_parser("mcp", help="serve the tools a coding agent calls, over MCP on stdio")
    state(tools)

    view = commands.add_parser("view", help="serve the run viewer on 127.0.0.1")
    view.add_argument("--port", type=int, default=VIEW_PORT)
    state(view)
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
        if args.command == "env":
            return _env(args, state)
        if args.command == "mcp":
            return _mcp(state)
        if args.command == "view":
            return _view(state, args.port)
        return _runs(state)
    except (RunRefused, FileRefused, ModelFailed, OSError) as e:
        print(f"minutehand: the run could not be performed: {e}", file=sys.stderr)
        return 2


def _telemetry() -> OtelTelemetry | None:
    """OTLP export when its endpoint is set; otherwise none, rather than spans with nowhere to go."""
    return from_environment() if os.environ.get(ENDPOINT_VARIABLE) else None


def _listen(args: argparse.Namespace) -> session.Listen:
    return session.Listen(
        host=args.proxy_host, port=args.proxy_port, agent_host=args.agent_proxy_host, no_proxy=args.no_proxy
    )


def _env(args: argparse.Namespace, state: Path) -> int:
    agent = load_agent(args.agent)
    listen = _listen(args)
    if EnvFormat(args.format) is EnvFormat.SHELL:
        if args.service:
            print("minutehand env: --service is for --format compose", file=sys.stderr)
            return 2
        variables = session.environment(agent, state=state, listen=listen)
        print("\n".join(f"export {name}={shlex.quote(value)}" for name, value in variables.items()))
        return 0
    if not args.service:
        print("minutehand env: --format compose needs --service for each service the agent runs in", file=sys.stderr)
        return 2
    services = [*args.service, *listen.no_proxy]
    in_container = listen.model_copy(update={"no_proxy": services})
    variables = session.environment(agent, state=state, listen=in_container, ca_bundle=args.ca_path)
    bundle = (state / "ca" / BUNDLE).resolve()
    print(
        yaml.safe_dump(_compose(args.service, variables, bundle, args.ca_path, in_container), sort_keys=False), end=""
    )
    return 0


def _compose(
    services: list[str], variables: dict[str, str], bundle: Path, ca_path: str, listen: session.Listen
) -> dict[str, object]:
    """A Compose override file: every named service gets the variables and the CA bundle mounted read-only.
    A service reaching the host as host.docker.internal is given that name on Linux too, where Docker does not
    define it by itself."""
    service: dict[str, object] = {"environment": variables, "volumes": [f"{bundle}:{ca_path}:ro"]}
    if listen.agent_host == DOCKER_HOST:
        service["extra_hosts"] = [f"{DOCKER_HOST}:host-gateway"]
    return {"services": {name: service for name in services}}


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
                listen=_listen(args),
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
                listen=_listen(args),
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


def _mcp(state: Path) -> int:
    mcp_server.serve(state)
    return 0


def _view(state: Path, port: int) -> int:
    print(f"minutehand: the viewer is at http://127.0.0.1:{port}/ (state {state})", file=sys.stderr)
    viewer.serve(state, port=port)
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
    if record.failure is not None:
        lines.append(f"  {record.failure}")
    lines.append(f"  providers the agent called: {', '.join(record.providers) or 'none'}")
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
