"""`minutehand`: run a scenario against an agent, read a run's findings, fork a run, list the runs.

    minutehand run <scenario.yaml> --agent <agent.yaml> [--state DIR] [--samples N] [--judge] [--json] [PROXY] [-- <command...>]
    minutehand findings <run_id> [--state DIR] [--json]
    minutehand fork <run_id> --at <seq> --changes <fork.yaml> [--state DIR] [--judge] [--json] [PROXY] [-- <command...>]
    minutehand env --agent <agent.yaml> --proxy-port N [PROXY] [--format shell|compose] [--service NAME...]
                                                 the environment an agent Minutehand does not start needs
    minutehand runs [--state DIR]               every finished run, with what it costs on disk
    minutehand checkpoints <run_id> [--state DIR]
                                                 a run's checkpoints, whether each is restorable, its snapshot's size
    minutehand pin <run_id> <seq> [--state DIR]  keep a checkpoint's snapshot whatever `state: keep` says
    minutehand unpin <run_id> <seq> [--state DIR]
    minutehand gc [--state DIR]                  remove stored bodies and snapshot files nothing refers to
    minutehand doctor [--agent <agent.yaml>] [--model-host HOST]... [--agent-host H] [--no-proxy H]... [--json] -- <command...>
                                                 which HTTP clients in the agent's interpreter would go around the
                                                 proxy, and which declared hosts NO_PROXY would send directly
    minutehand mcp [--state DIR]                 the same over MCP, on stdio, for a coding agent
    minutehand view [--state DIR] [--port N]     the runs in a browser, on 127.0.0.1 only
    minutehand serve [--state DIR] [--host H] [--proxy-port N] [--control-port N] [--telemetry-port N]
                     [--agent-host NAME] [--keep N] [--capture-unknown] [--upstream-ca FILE]
                     [--model-host HOST]... [--record-model-calls]
                                                 a standing proxy with a control API, for test suites (docs/serve.md)

PROXY is where the proxy listens and how the agent reaches it: --proxy-host (default 127.0.0.1; 0.0.0.0 for
an agent in containers), --proxy-port (default: any free port), --agent-proxy-host (the host the agent uses
for it, e.g. host.docker.internal; default the bind host) and --no-proxy HOST, repeated, for hosts the agent
reaches directly. Beside the proxy, on the same host, an OTLP/HTTP receiver keeps the agent's own spans with
the run: --telemetry-port (default: any free port), or --no-receive-telemetry to serve none. Spans the agent
exports are passed on to wherever OTEL_EXPORTER_OTLP_ENDPOINT in Minutehand's own environment points.
--record-model-calls opens the agent's calls to model APIs and keeps each as a span, for an agent that
exports nothing. --model-host HOST, repeated, names a model API besides the three public ones (a self-hosted
model, another provider), so it is tunnelled, edited by a fork's PromptPatch or ModelSwap, or recorded. A host no provider claims is refused unless the agent file declares it under `outbound`
(acknowledge, pass_through or replay); --capture-unknown passes every undeclared one through and keeps it, and
the run ends with the hosts it saw and a declaration for each (docs/capture.md).

A model, for people whose replies it writes and for --judge, is configured by MINUTEHAND_MODEL,
MINUTEHAND_MODEL_API_KEY and MINUTEHAND_MODEL_BASE_URL.

Exit codes of `run`, `fork` and `findings`, which follow the verdict each report starts with:
  0  passed: no check failed, and the agent finished: it reported done, or nothing was left open
  1  failed: a check failed
  2  the run could not be performed
  3  not finished: no check failed, but the run stopped without the agent reporting done (the wake limit, the
     deadline, an agent that asked for no further wake, an agent that failed) while a wait or a commitment
     was still open
With samples: 1 when any sample failed, else 3 when any did not finish, else 0.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

import yaml

from minutehand import serve as standing
from minutehand import session
from minutehand.adapters.model.openai_compatible import from_environment as model_from_environment
from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS
from minutehand.adapters.proxy.trust import BUNDLE
from minutehand.adapters.telemetry.otel import ENDPOINT_VARIABLE, OtelTelemetry, from_environment
from minutehand.application.checkpoint import NoHooks, NotRestorable, Restorable
from minutehand.application.files import FileRefused, load_agent, load_fork, load_scenario
from minutehand.application.forks import ForkAccount, scorecard_lines
from minutehand.application.forks import described as fork_described
from minutehand.application.outbound import described, emulator_described, suggested
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import Restored
from minutehand.checks.patterns import pattern
from minutehand.checks.runner import exit_code, stability
from minutehand.domain.checks import Effectiveness, Finding, FindingKind, Stability
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Model
from minutehand.ports.model import ModelFailed
from minutehand.session import ForkPoint, Outcome

DEFAULT_STATE = Path(".minutehand")
SERVE_IMAGE = "minutehand"
SERVE_CA_VOLUME = "minutehand-ca"
SERVE_CA_DIR = "/etc/minutehand"
IMAGE_STATE = "/var/lib/minutehand"
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
    StopReason.CLOSED: "the standing world was closed by whoever opened it",
    StopReason.ENVIRONMENT_FAILED: "an external emulator the run used was unavailable",
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
        sub.add_argument(
            "--telemetry-port",
            type=int,
            default=0,
            help="the port the OTLP receiver listens on, on the proxy's host (default: any free one)",
        )
        sub.add_argument(
            "--no-receive-telemetry",
            action="store_true",
            help="serve no OTLP receiver and leave the agent's OTLP exporter where it points",
        )
        models(sub)
        capture(sub)

    def models(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--record-model-calls",
            action="store_true",
            help="open the agent's calls to model APIs, send them on unchanged and keep each as a span",
        )
        sub.add_argument(
            "--model-host",
            action="append",
            default=[],
            metavar="HOST",
            help="a host that is a model API, besides api.openai.com, api.anthropic.com and "
            "generativelanguage.googleapis.com: tunnelled, edited by a fork, or recorded with --record-model-calls",
        )

    def capture(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--capture-unknown",
            action="store_true",
            help="pass through and keep every call to a host nobody claims or declares, rather than refusing it; "
            "the run ends with the hosts it saw and a declaration for each",
        )
        sub.add_argument(
            "--upstream-ca",
            type=Path,
            default=None,
            help="the CA file a real host is verified against when a call is passed through (default the system's)",
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
    env.add_argument("--agent", type=Path, default=None, help="the agent file (not needed with --serve-as)")
    env.add_argument(
        "--serve-as",
        default=None,
        metavar="NAME",
        help="--format compose: the services reach a `minutehand serve` container of this Compose service name, "
        "which the override adds, and download nothing: its CA is shared through a volume",
    )
    env.add_argument(
        "--image", default=SERVE_IMAGE, help=f"with --serve-as: the image the service runs (default {SERVE_IMAGE})"
    )
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

    listing = commands.add_parser("runs", help="every finished run, with what it costs on disk")
    state(listing)

    kept = commands.add_parser("checkpoints", help="a run's checkpoints, and the size of each snapshot")
    kept.add_argument("run_id")
    state(kept)

    for verb, said in (("pin", "keep a checkpoint's snapshot whatever the agent's `keep` says"), ("unpin", "undo pin")):
        pinning = commands.add_parser(verb, help=said)
        pinning.add_argument("run_id")
        pinning.add_argument("seq", type=int, help="the checkpoint's seq (listed by `checkpoints`)")
        state(pinning)

    swept = commands.add_parser("gc", help="remove stored bodies and snapshot files nothing refers to")
    state(swept)

    tools = commands.add_parser("mcp", help="serve the tools a coding agent calls, over MCP on stdio")
    state(tools)

    served = commands.add_parser(
        "serve", help="a standing proxy with a control API: worlds a test suite opens, reads and closes"
    )
    served.add_argument("--host", default="127.0.0.1", help="where the proxy, receiver and control API listen")
    served.add_argument("--proxy-port", type=int, default=standing.DEFAULT_PROXY_PORT)
    served.add_argument("--control-port", type=int, default=standing.DEFAULT_CONTROL_PORT)
    served.add_argument("--telemetry-port", type=int, default=standing.DEFAULT_TELEMETRY_PORT)
    served.add_argument("--no-receive-telemetry", action="store_true", help="serve no OTLP receiver")
    served.add_argument(
        "--agent-host", default=None, help="the name services reach this server by (a Compose service name)"
    )
    served.add_argument(
        "--no-proxy", action="append", default=[], metavar="HOST", help="a host services reach directly"
    )
    served.add_argument(
        "--keep", type=int, default=standing.DEFAULT_KEEP, help="closed worlds kept; older ones are removed"
    )
    models(served)
    capture(served)
    state(served)

    doctor = commands.add_parser(
        "doctor", help="which HTTP clients in the agent's interpreter would go around the proxy (-- <command>)"
    )
    doctor.add_argument("--agent", type=Path, default=None, help="the agent file: its outbound hosts are checked too")
    doctor.add_argument("--model-host", action="append", default=[], metavar="HOST")
    doctor.add_argument(
        "--agent-host", default=None, help="the name the agent uses for this machine, as the run will be given it"
    )
    doctor.add_argument(
        "--no-proxy", action="append", default=[], metavar="HOST", help="a host the run will send direct, as given it"
    )
    doctor.add_argument("--json", action="store_true")
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
    if args.command == "doctor":
        if not command:
            print("minutehand doctor: give the agent's command after --", file=sys.stderr)
            return 2
        try:
            return _doctor(args, command)
        except (FileRefused, RuntimeError, OSError) as e:
            print(f"minutehand doctor: {e}", file=sys.stderr)
            return 2
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
        if args.command == "serve":
            return _serve(args, state)
        if args.command == "checkpoints":
            return _checkpoints(state, args.run_id)
        if args.command in ("pin", "unpin"):
            return _pin(state, args.run_id, args.seq, pinned=args.command == "pin")
        if args.command == "gc":
            return _gc(state)
        return _runs(state)
    except (RunRefused, FileRefused, ModelFailed, OSError) as e:
        print(f"minutehand: the run could not be performed: {e}", file=sys.stderr)
        return 2


def _telemetry() -> OtelTelemetry | None:
    """OTLP export when its endpoint is set; otherwise none, rather than spans with nowhere to go."""
    return from_environment() if os.environ.get(ENDPOINT_VARIABLE) else None


def _listen(args: argparse.Namespace) -> session.Listen:
    return session.Listen(
        host=args.proxy_host,
        port=args.proxy_port,
        agent_host=args.agent_proxy_host,
        no_proxy=args.no_proxy,
        telemetry_port=args.telemetry_port,
        receive_telemetry=not args.no_receive_telemetry,
        record_model_calls=args.record_model_calls,
        capture_unknown=args.capture_unknown,
        upstream_ca=args.upstream_ca,
        model_hosts=list(dict.fromkeys([*DEFAULT_MODEL_HOSTS, *args.model_host])),
    )


def _env(args: argparse.Namespace, state: Path) -> int:
    if args.serve_as is not None:
        if EnvFormat(args.format) is not EnvFormat.COMPOSE or not args.service:
            print(
                "minutehand env: --serve-as writes a Compose override: give --format compose and --service",
                file=sys.stderr,
            )
            return 2
        print(yaml.safe_dump(_standing_compose(args), sort_keys=False), end="")
        return 0
    if args.agent is None:
        print(
            "minutehand env: --agent is needed unless the services use a `minutehand serve` (--serve-as)",
            file=sys.stderr,
        )
        return 2
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


def _standing_compose(args: argparse.Namespace) -> dict[str, object]:
    """A Compose override for a stack whose services reach a `minutehand serve` container by service name: the
    container, sharing its CA directory through a named volume and healthy once its control API answers, and
    every named service given the variables, the CA read-only, and a wait for it."""
    name: str = args.serve_as
    listen = session.Listen(
        host="0.0.0.0",
        port=standing.DEFAULT_PROXY_PORT,
        agent_host=name,
        no_proxy=[*args.service, *args.no_proxy, name],
        telemetry_port=standing.DEFAULT_TELEMETRY_PORT,
        receive_telemetry=not args.no_receive_telemetry,
    )
    bundle = f"{SERVE_CA_DIR}/{BUNDLE}"
    telemetry = standing.DEFAULT_TELEMETRY_PORT if listen.receive_telemetry else None
    variables = session.agent_environment(listen, standing.DEFAULT_PROXY_PORT, bundle, {}, telemetry_port=telemetry)
    health = f"http://127.0.0.1:{standing.DEFAULT_CONTROL_PORT}/v1/health"
    server: dict[str, object] = {
        "image": args.image,
        "command": ["serve", "--host", "0.0.0.0", "--agent-host", name],
        "volumes": [f"{SERVE_CA_VOLUME}:{IMAGE_STATE}/ca"],
        "healthcheck": {
            "test": [
                "CMD",
                "/opt/minutehand/bin/python",
                "-c",
                f"import urllib.request; urllib.request.urlopen({health!r})",
            ],
            "interval": "1s",
            "timeout": "2s",
            "retries": 30,
        },
    }
    service: dict[str, object] = {
        "environment": variables,
        "volumes": [f"{SERVE_CA_VOLUME}:{SERVE_CA_DIR}:ro"],
        "depends_on": {name: {"condition": "service_healthy"}},
    }
    return {
        "services": {name: server, **{s: copy.deepcopy(service) for s in args.service}},
        "volumes": {SERVE_CA_VOLUME: {}},
    }


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
                progress=_progress("run"),
            )
        )
    finally:
        if telemetry is not None:
            telemetry.shutdown()
    return _report(outcomes, state, as_json=args.json, sampled=args.samples > 1)


def _progress(command: str) -> Callable[[str], None]:
    """Each step of a restore, as it is taken, on stderr: stdout carries the report, or the JSON."""

    def say(line: str) -> None:
        print(f"minutehand {command}: restore {line}", file=sys.stderr, flush=True)

    return say


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
                progress=_progress("fork"),
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
        print(
            _describe(
                outcome,
                session.fork_points(state, args.run_id),
                session.restore_of(state, args.run_id),
                session.fork_account(state, args.run_id),
            )
        )
    return outcome.result.exit_code


def _runs(state: Path) -> int:
    found = session.runs(state)
    if not found:
        print(f"no runs under {state}")
        return 0
    for outcome in found:
        record = outcome.record
        failed = sum(1 for f in outcome.result.findings if f.kind is FindingKind.FAIL)
        print(f"{record.run_id}  {record.scenario}  {record.stop.value}  {failed} failed")
        account = session.fork_account(state, record.run_id)
        if account is not None:
            print(
                f"  forked from {account.parent_run} at seq {account.at_seq}, after wake {account.after_wake} "
                f"({account.at:%Y-%m-%d %H:%M} UTC simulated): {account.summary}"
            )
        print(f"  {_restorable_summary(session.fork_points(state, record.run_id))}")
        used = session.usage_of(state, record.run_id)
        print(
            f"  on disk: {_size(used.rows)} of rows, {_size(used.bodies)} of bodies it alone holds, "
            f"{_size(used.snapshots)} of snapshots it alone holds"
        )
    return 0


def _size(count: int) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if count >= scale:
            return f"{count / scale:.1f} {unit}"
    return f"{count} bytes"


def _checkpoints(state: Path, run_id: str) -> int:
    found = session.checkpoints_of(state, run_id)
    if not found:
        print(f"run {run_id} has no checkpoints")
        return 0
    for one in found:
        point, snapshot = one.point, one.snapshot
        line = f"seq {point.seq}, after wake {point.wake}: {_point(point)}"
        if snapshot is not None and not snapshot.pruned:
            line += (
                f"; snapshot of {snapshot.files} files, {_size(snapshot.size)}, "
                f"{_size(snapshot.held)} on disk held by it alone"
            )
            if snapshot.pinned:
                line += "; pinned"
        print(line)
    return 0


def _pin(state: Path, run_id: str, seq: int, *, pinned: bool) -> int:
    snapshot = session.pin(state, run_id, seq, pinned=pinned)
    said = "pinned: pruning keeps it" if snapshot.pinned else "unpinned: the agent's `keep` may prune it"
    print(f"the snapshot at seq {seq} of run {run_id} is {said}")
    return 0


def _gc(state: Path) -> int:
    collected = session.collect(state)
    freed = collected.freed
    print(
        f"freed {freed.bodies} stored bodies ({_size(freed.body_bytes)}) and {freed.files} snapshot files "
        f"({_size(freed.file_bytes)}) across {collected.swept} world files"
    )
    for skipped in collected.skipped:
        print(f"  not swept: {skipped}")
    return 0


def _restorable_summary(points: list[ForkPoint]) -> str:
    """One line: the seqs a fork can be taken from, and those it cannot."""
    if not points:
        return "no checkpoints"
    if all(isinstance(p.agent, NoHooks) for p in points):
        return "no checkpoint is restorable: the agent declares no state hooks"
    can = [str(p.seq) for p in points if isinstance(p.agent, Restorable)]
    cannot = [str(p.seq) for p in points if not isinstance(p.agent, Restorable)]
    parts = [f"restorable at seq {', '.join(can)}" if can else "no checkpoint is restorable"]
    if cannot:
        parts.append(f"not restorable at seq {', '.join(cannot)} (`minutehand findings` says why)")
    return "; ".join(parts)


def _doctor(args: argparse.Namespace, command: list[str]) -> int:
    """0 when every client found reaches the proxy, 1 when one would go around it."""
    from minutehand import doctor

    agent = load_agent(args.agent) if args.agent is not None else None
    hosts = list(dict.fromkeys([*DEFAULT_MODEL_HOSTS, *args.model_host]))
    listen = session.Listen(agent_host=args.agent_host, no_proxy=args.no_proxy)
    found = asyncio.run(doctor.diagnose(command, agent, hosts, listen))
    print(found.model_dump_json(indent=2) if args.json else doctor.described(found))
    return 1 if found.bypasses else 0


def _mcp(state: Path) -> int:
    from minutehand.adapters.mcp import server as mcp_server  # loaded only for this command: it is slow to import

    mcp_server.serve(state)
    return 0


def _serve(args: argparse.Namespace, state: Path) -> int:
    options = standing.ServeOptions(
        host=args.host,
        proxy_port=args.proxy_port,
        control_port=args.control_port,
        telemetry_port=args.telemetry_port,
        receive_telemetry=not args.no_receive_telemetry,
        agent_host=args.agent_host,
        no_proxy=args.no_proxy,
        keep=args.keep,
        capture_unknown=args.capture_unknown,
        upstream_ca=args.upstream_ca,
        model_hosts=args.model_host,
        record_model_calls=args.record_model_calls,
    )
    try:
        asyncio.run(standing.serve_forever(state, options))
    except KeyboardInterrupt:
        return 0
    return 0


def _view(state: Path, port: int) -> int:
    from minutehand.adapters.web import app as viewer  # loaded only for this command

    print(f"minutehand: the viewer is at http://127.0.0.1:{port}/ (state {state})", file=sys.stderr)
    viewer.serve(state, port=port)
    return 0


def _report(outcomes: list[Outcome], state: Path, *, as_json: bool, sampled: bool) -> int:
    stable = stability([o.result for o in outcomes]) if sampled else None
    if as_json:
        print(Played(outcomes=outcomes, stability=stable).model_dump_json(indent=2))
    else:
        print(
            "\n\n".join(
                _describe(
                    o,
                    session.fork_points(state, o.record.run_id),
                    session.restore_of(state, o.record.run_id),
                    session.fork_account(state, o.record.run_id),
                )
                for o in outcomes
            )
        )
        if stable is not None:
            print(f"\nstability: passed {stable.passed} of {stable.samples} samples")
    return exit_code([o.result for o in outcomes])


def _describe(outcome: Outcome, points: list[ForkPoint], restored: Restored | None, account: ForkAccount | None) -> str:
    record, result = outcome.record, outcome.result
    lines = [f"run {record.run_id}: {record.scenario}", f"  {result.verdict.words}"]
    if restored is not None:
        # The line scripts read since forks were first restored: kept beside the fuller account below.
        verdict = "verified" if restored.verified else f"NOT verified: {restored.unverified}"
        lines.append(f"  the agent was restored from seq {restored.checkpoint_seq}, {verdict}")
    if account is not None:
        lines += [f"  {line}" for line in fork_described(account)]
    lines.append(f"  stopped at {record.ended_at:%Y-%m-%d %H:%M} UTC (simulated) because {_STOPPED[record.stop]}")
    if record.failure is not None:
        lines.append(f"  {record.failure}")
    lines.append(f"  providers the agent called: {', '.join(record.providers) or 'none'}")
    if record.outbound:
        lines.append("\noutbound calls")
        lines += [f"  {described(use)}" for use in record.outbound]
        declarations = suggested(record.outbound)
        if declarations:
            lines.append("\nto capture the hosts nobody declared, add to the agent file (acknowledge: answered here")
            lines.append("and never sent; pass_through: sent to the real host; replay: answered from a run):")
            lines += [f"  {line}" for line in declarations.rstrip().splitlines()]
    if record.emulators:
        lines.append("\nexternal emulators")
        lines += [f"  {emulator_described(use)}" for use in record.emulators]
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
        lines.append("\ncheckpoints")
        lines += [f"  seq {p.seq}, after wake {p.wake}: {_point(p)}" for p in points]
    return "\n".join(lines)


def _point(point: ForkPoint) -> str:
    agent = point.agent
    if isinstance(agent, Restorable):
        return "restorable" if agent.unconfirmed is None else f"restorable, unconfirmed: {agent.unconfirmed}"
    if isinstance(agent, NotRestorable):
        return f"not restorable: {agent.reason}"
    return "not restorable: the agent declares no state hooks"


def _finding(finding: Finding) -> str:
    where = f" (wake {finding.wake})" if finding.wake is not None else ""
    line = f"  {finding.check}: {finding.message}{where}"
    if finding.pattern is not None:
        known = pattern(finding.pattern)
        line += f"\n    pattern {known.key}: {known.title}. {known.design}"
    return line


def _scorecard(card: Effectiveness) -> list[str]:
    return [f"{line.label}: {line.value}" for line in scorecard_lines(card)]


if __name__ == "__main__":
    sys.exit(main())
