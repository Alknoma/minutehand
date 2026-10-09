"""`minutehand`: run a scenario against an agent, read a run's findings, fork a run, list the runs.

    minutehand run <scenario.yaml> --agent <agent.yaml> [--state DIR] [--samples N] [--seed S] [--judge] [--json] [PROXY] [-- <command...>]
                                                 {run.port} and {run.dir} in the agent file and the command are
                                                 filled once, as run-all fills them, when a command is given
    minutehand run-all <folder> --agent <agent.yaml> [--jobs N] [--samples N] [--seed S] [--state DIR] [--judge] [--json]
                     [--record-model-calls] [--model-host HOST]... [--capture-unknown [MODE]] [--upstream-ca FILE]
                     [--proxy-host H] [--agent-proxy-host H] [--no-proxy H]... [--no-receive-telemetry] [-- <command...>]
                                                 every scenario in the folder, in parallel, each in a run of its own,
                                                 N times under seeds S, S+1, ...; {run.port} and {run.dir} in the agent
                                                 file and the command are filled per run; exits 1 when a scenario's
                                                 verdicts miss its expect_outcome, 2 when a run could not be performed
    minutehand findings <run_id> [--state DIR] [--json]
    minutehand fork <run_id> --at <seq> --changes <fork.yaml> [--seed S] [--state DIR] [--judge] [--json] [PROXY] [-- <command...>]
    minutehand env --agent <agent.yaml> --proxy-port N [PROXY] [--format shell|compose|redirect] [--service NAME...]
                                                 the environment an agent Minutehand does not start needs
    minutehand runs [--state DIR]               every finished run, with what it costs on disk
    minutehand checkpoints <run_id> [--state DIR]
                                                 a run's checkpoints, and whether a fork can start at each
    minutehand gc [--state DIR]                  remove stored bodies nothing refers to
    minutehand query <run> "SELECT ..." [--format table|json|csv] [--prices FILE] [--export FILE] [--state DIR]
    minutehand query --schema                    read-only SQL over a run's read model, and its views (docs/querying.md)
    minutehand trace <run> [--person KEY] [--provider P] [--kind K] [--from T] [--to T] [--wake N] [--json]
                                                 the agent's actions in order
    minutehand explain <run> <seq> [--json]      one event: the wake, what woke it, what the agent read first, what
                                                 it answers or follows up, and what followed ("action N" is a place
                                                 among the agent's acts, never a seq)
    minutehand rm <run_id>... [--state DIR]      remove runs with their forks
    minutehand doctor [--agent <agent.yaml>] [--model-host HOST]... [--agent-host H] [--no-proxy H]... [--json] -- <command...>
                                                 which HTTP clients in the agent's interpreter would go around the
                                                 proxy, and which declared hosts NO_PROXY would send directly
    minutehand mcp [--state DIR]                 the same over MCP, on stdio, for a coding agent
    minutehand view [--state DIR] [--port N] [--prices FILE]   the runs in a browser, on 127.0.0.1 only
    minutehand scenarios                         the scenario library: each scenario's name and situation
    minutehand scenarios show <name>             what one is for, its checks and patterns, the values it takes
    minutehand scenarios new <name>...|--all --goal TEXT --owner 'Name <email>' --ask 'Name <email>'
                     [--answer TEXT --tell PHRASE] [--other 'Name <email>'] [--credential-env VAR]
                     [--provider KEY] [--wakes reported|booked|polled] [--out DIR] [--force]
                                                 write library scenarios out with the team's values (docs/scenarios.md)
    minutehand serve [--state DIR] [--host H] [--proxy-port N] [--control-port N] [--telemetry-port N]
                     [--agent-host NAME] [--keep N] [--capture-unknown] [--upstream-ca FILE]
                     [--model-host HOST]... [--record-model-calls]
                                                 a standing proxy with a control API, for test suites (docs/serve.md)

PROXY is where the proxy listens and how the agent reaches it: --proxy-host (default 127.0.0.1; 0.0.0.0 for
an agent in containers), --proxy-port (default: any free port), --agent-proxy-host (the host the agent uses
for it, e.g. host.docker.internal; default the bind host) and --no-proxy HOST, repeated, for hosts the agent
reaches directly. --transparent-port N also listens for connections the agent's container sends to the proxy with
iptables (`env --format redirect` prints the script), for a client that ignores HTTPS_PROXY. Beside the proxy, on the same host, an OTLP/HTTP receiver keeps the agent's own spans with
the run: --telemetry-port (default: any free port), or --no-receive-telemetry to serve none. Spans the agent
exports are passed on to wherever OTEL_EXPORTER_OTLP_ENDPOINT in Minutehand's own environment points.
--record-model-calls opens the agent's calls to model APIs and keeps each as a span, for an agent that
exports nothing. --model-host HOST, repeated, names a model API besides the three public ones (a self-hosted
model, another provider), so it is tunnelled, edited by a fork's PromptPatch or ModelSwap, or recorded. A host no provider claims is refused unless the agent file declares it under `outbound`
(acknowledge, pass_through or replay); --capture-unknown passes every undeclared one through and keeps it, and
the run ends with the hosts it saw and a declaration for each (docs/capture.md).

A model, for people whose replies it writes and for --judge, is configured by MINUTEHAND_MODEL,
MINUTEHAND_MODEL_API_KEY and MINUTEHAND_MODEL_BASE_URL, and MINUTEHAND_MODEL_API: openai (the default, any
OpenAI-compatible chat-completions API) or anthropic (Anthropic's Messages API, base URL https://api.anthropic.com).

A run is judged only by what its files declare: the team's rules (`assess:` in the agent file and the scenario,
docs/assessments.md), the scenario's `expect:` and `protected_names`, and the agent's own `checks:`. With --json,
`run`, `fork` and `findings` print one shape: {"outcomes": [...], "stability": ...}.

Exit codes of `run`, `fork` and `findings`, which follow the verdict each report starts with:
  0  passed: nothing failed, and the agent finished: it reported done, or nothing was left open
  1  failed: a rule, an expectation or a check failed
  2  the run could not be performed
  3  not finished: nothing failed, but the run stopped without the agent reporting done (the wake limit, the
     deadline, an agent that asked for no further wake, an agent that failed) while a wait or a commitment
     was still open
  4  not scored: Minutehand itself failed while answering one of the agent's calls, so the run says nothing about
     the agent; any command exits 4 too when Minutehand fails, naming where its traceback was written (--debug
     prints it as well)
  5  not judged: nothing was assessed (the files declare no rule, expectation, protected name or check of the
     agent's own; the facts are still reported), or a check that needs wakes had none
With samples: 2 when an external emulator was unavailable in any sample, else 4 when Minutehand failed in any,
else 1 when any failed, else 5 when any was not judged, else 3 when any did not finish, else 0.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import shlex
import sys
import tempfile
import textwrap
import traceback
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import ValidationError

import minutehand
from minutehand import agent_api, mcp_relay, run_all, session
from minutehand import serve as standing
from minutehand.adapters.agent.inboxes import HttpInboxReach
from minutehand.adapters.agent.openapi import OperationUnresolved
from minutehand.adapters.model.environment import from_environment as model_from_environment
from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS
from minutehand.adapters.proxy.trust import BUNDLE
from minutehand.adapters.query import reader as read_model
from minutehand.adapters.query.reader import Format as QueryFormat
from minutehand.adapters.query.reader import QueryRefused
from minutehand.adapters.query.schema import described as read_model_schema
from minutehand.adapters.query.trace import KINDS, TraceFilter, explain, explanation, trace
from minutehand.adapters.query.trace import described as traced_lines
from minutehand.adapters.telemetry.otel import ENDPOINT_VARIABLE, OtelTelemetry, from_environment
from minutehand.application.checkpoint import NotRestorable, Remembered
from minutehand.application.files import (
    FileKind,
    FileRefused,
    load_agent,
    load_fork,
    load_prices,
    load_scenario,
    problems,
    schema,
)
from minutehand.application.forks import ForkAccount, scorecard_lines
from minutehand.application.forks import described as fork_described
from minutehand.application.library import NotInLibrary, entries, entry, write
from minutehand.application.migrate import MigrationRefused, migrate
from minutehand.application.outbound import described, emulator_described, suggested
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import Restored
from minutehand.checks.patterns import PATTERNS, pattern
from minutehand.checks.runner import ChecksRefused, exit_code, load_checks, stability
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.assessments import merged, refuse_unknown_people
from minutehand.domain.checks import Effectiveness, Finding, FindingKind, RuleRead
from minutehand.domain.library import DEFAULT_ANSWER, DEFAULT_TELL, OTHER, LibraryScenario, TeamValues, Who, WhoRefused
from minutehand.domain.outbound import UnknownHosts
from minutehand.domain.prices import Prices
from minutehand.domain.run import EXIT_CODES, StopReason, VerdictKind
from minutehand.domain.scenario import PlannedBy, WrittenScenario
from minutehand.ports.model import ModelFailed
from minutehand.session import ForkPoint, Outcome

DEFAULT_STATE = Path(".minutehand")
DEBUG = "--debug"
INTERNAL_EXIT = EXIT_CODES[VerdictKind.TOOL_FAILED]
"""What the command exits with when Minutehand itself failed: the exit of a run whose fake broke."""
SERVE_IMAGE = "minutehand"
SERVE_CA_VOLUME = "minutehand-ca"
SERVE_CA_DIR = "/etc/minutehand"
IMAGE_STATE = "/var/lib/minutehand"
COMPOSE_CA_PATH = "/etc/minutehand/ca-bundle.pem"
COMPOSE_REDIRECT_PATH = "/etc/minutehand/redirect.sh"
REDIRECT_SCRIPT = "redirect.sh"
DOCKER_HOST = "host.docker.internal"
VIEW_PORT = 8081
STATE_VARIABLE = "MINUTEHAND_STATE"

_STOPPED = {
    StopReason.AGENT_DONE: "the agent reported it was done",
    StopReason.WAKE_LIMIT: "its wake limit was reached",
    StopReason.DEADLINE_PASSED: "the clock reached the scenario's deadline",
    StopReason.NOTHING_PENDING: "nothing more was due and the agent asked for no wake",
    StopReason.AGENT_FAILED: "the agent could not be reached or answered with an error",
    StopReason.CLOSED: "the standing world, or the last world of its case, was closed by whoever opened it",
    StopReason.ENVIRONMENT_FAILED: "an external emulator the run used was unavailable",
}
_KIND_ORDER = (FindingKind.FAIL, FindingKind.REVIEW, FindingKind.INFORMATIONAL)


class EnvFormat(StrEnum):
    SHELL = "shell"
    COMPOSE = "compose"
    REDIRECT = "redirect"  # the iptables script that sends the agent's container's connections to --transparent-port


class LibraryAction(StrEnum):
    """What `minutehand scenarios` does besides listing the library."""

    SHOW = "show"
    NEW = "new"


class _Parser(argparse.ArgumentParser):
    """The command line's parser, which takes a command's positionals anywhere among its options.

    argparse matches positionals greedily in the run of words before the first option, so `query RUN --state X SQL`
    gives `sql` nothing and refuses SQL as unrecognized on Python before 3.12.7 (CI's 3.12.3, Ubuntu 24.04's own),
    and `rm A --state X B` on every version. A command left with such words is read again on its own, intermixed,
    which places each positional wherever it was written."""

    commands: Mapping[str, argparse.ArgumentParser]

    def parse_anywhere(self, args: list[str]) -> argparse.Namespace:
        parsed, left = self.parse_known_args(args)
        if not left:
            return parsed
        command: str = parsed.command
        words = args[args.index(command) + 1 :]
        try:
            own = self.commands[command].parse_intermixed_args(words)
        except TypeError:  # a command whose arguments cannot be intermixed: refused as argparse refuses them
            self.error(f"unrecognized arguments: {' '.join(left)}")
        return argparse.Namespace(**{**vars(parsed), **vars(own)})


def _parser() -> _Parser:
    parser = _Parser(prog="minutehand", description="Simulated days for a proactive agent.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {minutehand.__version__}")
    parser.add_argument(
        DEBUG,
        action="store_true",
        help="on an internal error, print its traceback as well (anywhere before --, with any command)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    parser.commands = commands.choices

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
            "--transparent-port",
            type=int,
            default=None,
            metavar="PORT",
            help="also listen here for connections the agent's container redirects to the proxy with iptables, so a "
            "client that ignores HTTPS_PROXY is captured too (docs/containers.md, `env --format redirect`)",
        )
        sub.add_argument(
            "--no-receive-telemetry",
            action="store_true",
            help="receive none of the agent's telemetry and leave its OTLP exporter where it points (the receiver "
            "still holds the agent's memory)",
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
            nargs="?",
            const=UnknownHosts.ALL.value,
            default=UnknownHosts.REFUSE.value,
            choices=[u.value for u in UnknownHosts],
            help="pass through and keep calls to a host nobody claims or declares, rather than refusing them: 'all' "
            "(the default when the flag is given), only 'reads' (GET, HEAD, OPTIONS; a write is refused, so nothing "
            "is sent anywhere real), or 'model' (a write, and every call after it, answered as a service nobody "
            "declared, from its state: docs/services.md); the run ends with the hosts it saw and a declaration for each",
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
    run.add_argument(
        "--seed",
        type=int,
        default=None,
        help="where every draw of the run comes from (people's reply moments); default the scenario's own, or one "
        "derived from its name; printed in the report",
    )
    run.add_argument("--judge", action="store_true", help="also run the checks a model judges")
    run.add_argument("--json", action="store_true")
    proxy(run)
    state(run)

    run_all = commands.add_parser(
        "run-all", help="run every scenario in a folder against one agent, in parallel, and compare each verdict"
    )
    run_all.add_argument("folder", type=Path)
    run_all.add_argument("--agent", type=Path, required=True)
    run_all.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="runs at once (default 4)")
    run_all.add_argument("--judge", action="store_true", help="also run the checks a model judges")
    run_all.add_argument(
        "--samples",
        type=int,
        default=1,
        help="run each scenario this many times, each under its own seed counted up from --seed (default 1)",
    )
    run_all.add_argument(
        "--seed",
        type=int,
        default=None,
        help="the first seed of each scenario's samples; default each scenario's own seed",
    )
    run_all.add_argument("--json", action="store_true")
    run_all.add_argument(
        "--proxy-host", default=None, help="the address each run's proxy listens on (each takes a free port)"
    )
    run_all.add_argument("--agent-proxy-host", default=None, help="the host the agent reaches each run's proxy at")
    run_all.add_argument(
        "--no-proxy", action="append", default=[], metavar="HOST", help="a host the agent reaches directly"
    )
    run_all.add_argument(
        "--no-receive-telemetry", action="store_true", help="receive none of the agent's telemetry in any run"
    )
    models(run_all)
    capture(run_all)
    state(run_all)

    findings = commands.add_parser("findings", help="what the checks said about a finished run")
    findings.add_argument("run_id")
    findings.add_argument("--json", action="store_true")
    state(findings)

    fork = commands.add_parser("fork", help="rerun a finished run from a checkpoint with something changed")
    fork.add_argument("run_id")
    fork.add_argument("--at", type=int, required=True, help="the checkpoint's seq (listed by `findings`)")
    fork.add_argument("--changes", type=Path, required=True)
    fork.add_argument(
        "--seed",
        type=int,
        default=None,
        help="where the fork's draws for asks after the checkpoint come from; default the changes file's, else the "
        "parent's. Draws made before the checkpoint are kept",
    )
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

    kept = commands.add_parser("checkpoints", help="a run's checkpoints, and whether a fork can start at each")
    kept.add_argument("run_id")
    state(kept)

    swept = commands.add_parser("gc", help="remove stored bodies nothing refers to")
    state(swept)

    def priced(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--prices",
            type=Path,
            default=None,
            metavar="FILE",
            help="what each model costs per million tokens (docs/querying.md); without it no cost is given",
        )

    asked = commands.add_parser(
        "query", help="read-only SQL over a run's read model (docs/querying.md); --schema lists its views"
    )
    asked.add_argument("run", nargs="?", help="a run's or a fork's id, or the start of one")
    asked.add_argument("sql", nargs="?", help="one SELECT (or WITH ... SELECT)")
    asked.add_argument("--format", choices=[f.value for f in QueryFormat], default=QueryFormat.TABLE.value)
    asked.add_argument("--schema", action="store_true", help="every view with its columns, and stop")
    asked.add_argument(
        "--export", type=Path, default=None, metavar="FILE", help="write the read model to a new SQLite file"
    )
    priced(asked)
    state(asked)

    traced = commands.add_parser("trace", help="the agent's actions in a run, in order")
    traced.add_argument("run", help="a run's or a fork's id, or the start of one")
    traced.add_argument("--person", default=None, help="only messages to this person (their key)")
    traced.add_argument("--provider", default=None)
    traced.add_argument("--kind", default=None, choices=KINDS)
    traced.add_argument("--from", dest="since", default=None, metavar="TIME", help="simulated time, ISO 8601")
    traced.add_argument("--to", dest="until", default=None, metavar="TIME", help="simulated time, ISO 8601")
    traced.add_argument("--wake", type=int, default=None)
    traced.add_argument("--json", action="store_true")
    priced(traced)
    state(traced)

    told = commands.add_parser("explain", help="one event: what led to it and what followed")
    told.add_argument("run", help="a run's or a fork's id, or the start of one")
    told.add_argument("seq", type=int)
    told.add_argument("--json", action="store_true")
    state(told)

    removing = commands.add_parser("rm", help="remove runs with every fork of each")
    removing.add_argument("run_ids", nargs="+", metavar="run_id")
    state(removing)

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

    relayed = commands.add_parser(
        "mcp-relay",
        help="run an MCP server on standard input and output (-- <command>), passing every line through and "
        "reporting each tool call to the run (MINUTEHAND_MCP_URL)",
    )
    relayed.add_argument("--name", required=True, help="the server's name in the run's record")

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
    schema_of = commands.add_parser(
        "schema", help="print the JSON Schema of an agent, scenario or seed file, or the agent API's OpenAPI document"
    )
    schema_of.add_argument("kind", choices=[*(k.value for k in FileKind), AGENT_API])
    checking = commands.add_parser(
        "validate", help="load agent, scenario and seed files with every load-time check, naming each problem"
    )
    checking.add_argument("files", type=Path, nargs="+")
    checking.add_argument(
        "--kind", choices=[k.value for k in FileKind], default=None, help="default: from what it holds"
    )
    migrating = commands.add_parser(
        "migrate",
        help="rewrite a scenario's retired keys (ticket_fates, press, presses_every, decisions) as takes; comments in "
        "what it rewrites are not kept",
    )
    migrating.add_argument("file", type=Path)
    migrating.add_argument("--write", action="store_true", help="write it back to the file; default: print it")
    view = commands.add_parser("view", help="serve the run viewer on 127.0.0.1")
    view.add_argument("--port", type=int, default=VIEW_PORT)
    priced(view)
    state(view)
    _library_parser(commands.add_parser("scenarios", help="the scenario library: list it, or write scenarios out"))
    return parser


def _library_parser(library: argparse.ArgumentParser) -> None:
    actions = library.add_subparsers(dest="library_action")
    shown = actions.add_parser(
        LibraryAction.SHOW.value, help="what one library scenario is for, and the values it takes"
    )
    shown.add_argument("name")
    new = actions.add_parser(LibraryAction.NEW.value, help="write library scenarios out, filled with the team's values")
    new.add_argument("names", nargs="*", metavar="name", help="the library scenarios to write (or --all)")
    new.add_argument("--all", action="store_true", help="write every library scenario")
    new.add_argument("--goal", required=True, help="the goal handed to the agent, verbatim")
    new.add_argument(
        "--owner", required=True, metavar="'NAME <EMAIL>'", help="who gives the goal and is told the outcome"
    )
    new.add_argument("--ask", required=True, metavar="'NAME <EMAIL>'", help="the person the agent must ask")
    new.add_argument(
        "--answer", default=None, help=f"what that person answers (default {DEFAULT_ANSWER!r}); give --tell with it"
    )
    new.add_argument(
        "--tell", default=None, help=f"a phrase of the answer that must reach the owner (default {DEFAULT_TELL!r})"
    )
    new.add_argument(
        "--other",
        default=f"{OTHER.name} <{OTHER.email}>",
        metavar="'NAME <EMAIL>'",
        help="a second person: the delegate, the approver, someone who writes in (default %(default)s)",
    )
    new.add_argument(
        "--credential-env",
        default=TeamValues.model_fields["credential_env"].default,
        metavar="VAR",
        help="the variable the agent reads the approver's sign-in to its own product from (default %(default)s)",
    )
    new.add_argument(
        "--provider",
        default=TeamValues.model_fields["provider"].default,
        help="the messaging provider someone writes in on, unprompted (default %(default)s)",
    )
    new.add_argument(
        "--wakes",
        choices=[p.value for p in PlannedBy],
        default=PlannedBy.REPORTED.value,
        help="how the agent asks for its own wakes, for the scenarios whose scheduler goes wrong (default %(default)s)",
    )
    new.add_argument("--out", type=Path, default=Path("."), help="the folder to write into (default: this one)")
    new.add_argument("--force", action="store_true", help="replace a file of the same name")


def main(argv: Sequence[str] | None = None) -> int:
    """THE converter of the command line: every refusal the commands know keeps its message and exit code; anything
    else is Minutehand's own error, said in one line naming where its traceback was written, and exits 4."""
    args_in = list(sys.argv[1:] if argv is None else argv)
    ours = args_in[: args_in.index("--")] if "--" in args_in else args_in
    debug = DEBUG in ours
    if debug:
        args_in = [a for a in ours if a != DEBUG] + args_in[len(ours) :]
    try:
        return _main(args_in)
    except Exception as error:
        return _internal(error, debug=debug)


def _internal(error: Exception, *, debug: bool) -> int:
    """Minutehand's own error: its traceback written to a file, one line naming it, and the traceback on screen
    too with --debug."""
    written = "".join(traceback.format_exception(error))
    with tempfile.NamedTemporaryFile(
        "w", prefix="minutehand-internal-error-", suffix=".txt", delete=False, encoding="utf-8"
    ) as kept:
        kept.write(written)
    if debug:
        print(written, file=sys.stderr, end="")
    said = str(error).splitlines()[0] if str(error) else ""
    print(
        f"minutehand: internal error (a bug in minutehand, not in the agent or the scenario): "
        f"{type(error).__name__}: {said}; the traceback is in {kept.name}",
        file=sys.stderr,
    )
    return INTERNAL_EXIT


def _main(args_in: list[str]) -> int:
    command: list[str] | None = None
    if "--" in args_in:
        split = args_in.index("--")
        args_in, command = args_in[:split], args_in[split + 1 :]
        if not command:
            print("minutehand: nothing follows --; give the agent's command or leave -- out", file=sys.stderr)
            return 2
    args = _parser().parse_anywhere(args_in)
    if args.command == "mcp-relay":
        if not command:
            print("minutehand mcp-relay: give the MCP server's command after --", file=sys.stderr)
            return 2
        return mcp_relay.main(args.name, command)
    if args.command == "doctor":
        if not command:
            print("minutehand doctor: give the agent's command after --", file=sys.stderr)
            return 2
        try:
            return _doctor(args, command)
        except (FileRefused, RuntimeError, OSError) as e:
            print(f"minutehand doctor: {e}", file=sys.stderr)
            return 2
    if args.command == "schema":
        return _schema(args.kind)
    if args.command == "validate":
        return _validate(args.files, FileKind(args.kind) if args.kind else None)
    if args.command == "scenarios":
        return _scenarios(args)
    if args.command == "migrate":
        return _migrate(args.file, write=args.write)
    state: Path = args.state or Path(os.environ[STATE_VARIABLE] if STATE_VARIABLE in os.environ else DEFAULT_STATE)
    if command is not None and args.command not in ("run", "fork", "run-all"):
        print(f"minutehand {args.command}: takes no agent command", file=sys.stderr)
        return 2
    try:
        if args.command == "run":
            return _run(args, state, command)
        if args.command == "fork":
            return _fork(args, state, command)
        if args.command == "run-all":
            return _run_all(args, state, command)
        if args.command == "findings":
            return _findings(args, state)
        if args.command == "env":
            return _env(args, state)
        if args.command == "mcp":
            return _mcp(state)
        if args.command == "view":
            return _view(state, args.port, load_prices(args.prices) if args.prices is not None else None)
        if args.command == "serve":
            return _serve(args, state)
        if args.command == "checkpoints":
            return _checkpoints(state, args.run_id)
        if args.command == "gc":
            return _gc(state)
        if args.command in ("query", "trace", "explain"):
            return _read(args, state)
        if args.command == "rm":
            return _rm(state, args.run_ids)
        return _runs(state)
    except (RunRefused, FileRefused, ModelFailed, OSError) as e:
        print(f"minutehand: the run could not be performed: {e}", file=sys.stderr)
        return 2


AGENT_API = "agent-api"


def _schema(kind: str) -> int:
    """The JSON Schema of one kind of file, or the OpenAPI document of what an agent may implement, on stdout."""
    found = agent_api.document() if kind == AGENT_API else schema(FileKind(kind))
    print(json.dumps(found, indent=2, sort_keys=True))
    return 0


def _migrate(path: Path, *, write: bool) -> int:
    """The scenario at `path` with its retired keys rewritten (`application.migrate`): printed, or with `write` put
    back in the file; each change is said on stderr. Exit 2 when it cannot be rewritten."""
    text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        print(f"minutehand migrate: {path} holds no scenario", file=sys.stderr)
        return 2
    try:
        done = migrate(loaded)
    except MigrationRefused as e:
        print(f"minutehand migrate: {path}: {e}", file=sys.stderr)
        return 2
    for note in done.notes:
        print(f"{path}: {note}", file=sys.stderr)
    if not done.changed:
        print(f"{path}: nothing to migrate", file=sys.stderr)
        return 0
    header = [line for line in text.splitlines()[:1] if line.startswith("# yaml-language-server")]
    body = yaml.safe_dump(done.document, sort_keys=False, allow_unicode=True).rstrip()
    rewritten = "\n".join([*header, body]) + "\n"
    if write:
        path.write_text(rewritten, encoding="utf-8")
    else:
        print(rewritten, end="")
    return 0


def _validate(paths: Sequence[Path], kind: FileKind | None) -> int:
    """Each file loaded as a run would load it, and each inbox operation found in its OpenAPI document; given an agent
    file with scenarios, the rules each run would be judged by (`assess`): every problem on its own line naming the
    file and the place in it; exit 1 when there is any."""
    found: list[str] = []
    agents: list[tuple[Path, AgentUnderTest]] = []
    scenarios: list[tuple[Path, WrittenScenario]] = []
    for path in paths:
        read_as, model, said = problems(path, kind)
        if isinstance(model, WrittenScenario):
            scenarios.append((path, model))
        if isinstance(model, (WrittenScenario, AgentUnderTest)):
            known = {p.key for p in PATTERNS}
            said += [
                f"{path}: assess[{n}].pattern: no pattern {rule.pattern!r}; the patterns are in docs/patterns/"
                for n, rule in enumerate(model.assess)
                if rule.pattern is not None and rule.pattern not in known
            ]
        if isinstance(model, AgentUnderTest):
            agents.append((path, model))
            try:
                load_checks(model.checks)
            except ChecksRefused as e:
                said.append(f"{path}: checks: {e}")
            for n, declared in enumerate(model.inboxes):
                try:
                    HttpInboxReach(declared, {})
                except OperationUnresolved as e:
                    said.append(f"{path}: inboxes[{n}]: {e}")
        found += said
        if not said and read_as is not None:
            print(f"{path}: a valid {read_as.value} file")
    for agent_path, agent in agents:
        for scenario_path, scenario in scenarios:
            try:
                rules = merged(agent.assess, scenario.assess, scenario.assess_off)
                refuse_unknown_people(rules, [p.key for p in scenario.people])
            except ValueError as e:
                found.append(f"{scenario_path} with {agent_path}: assess: {e}")
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


def _scenarios(args: argparse.Namespace) -> int:
    """The library listed, one scenario shown, or scenarios written out; a refusal says why and exits 2."""
    try:
        if args.library_action == LibraryAction.SHOW:
            print(_shown(entry(args.name)))
            return 0
        if args.library_action == LibraryAction.NEW:
            return _new(args)
    except (NotInLibrary, WhoRefused, FileRefused, FileExistsError, ValidationError) as e:
        print(f"minutehand scenarios: {e}", file=sys.stderr)
        return 2
    for found in entries():
        print(f"{found.name}\n  {_first_sentence(found.situation)}")
    print("\nminutehand scenarios show <name> says what one is for; minutehand scenarios new <name> writes it out.")
    return 0


def _first_sentence(text: str) -> str:
    end = text.find(". ")
    return text if end < 0 else text[: end + 1]


def _shown(found: LibraryScenario) -> str:
    takes = {
        "goal": "--goal",
        "owner": "--owner",
        "ask": "--ask",
        "other": "--other",
        "answer": "--answer",
        "tell": "--tell",
        "credential_env": "--credential-env",
        "provider": "--provider",
        "wakes": "--wakes",
    }
    return "\n".join(
        [
            found.name,
            "",
            *textwrap.wrap(f"Situation: {found.situation}", 116),
            "",
            *textwrap.wrap(f"A good agent: {found.good_agent}", 116),
            "",
            f"rules: {', '.join(found.rules)} (in the scenario's `assess`, yours to edit once written)",
            f"patterns: {', '.join(found.patterns)}",
            f"takes: {' '.join(takes[u] for u in found.uses)}",
        ]
    )


def _new(args: argparse.Namespace) -> int:
    if args.all == bool(args.names):
        print("minutehand scenarios new: name the scenarios to write, or give --all", file=sys.stderr)
        return 2
    if (args.answer is None) != (args.tell is None):
        print(
            "minutehand scenarios new: --answer and --tell go together: the tell is a phrase of the answer",
            file=sys.stderr,
        )
        return 2
    answered = {} if args.answer is None else {"answer": args.answer, "tell": args.tell}
    team = TeamValues(
        goal=args.goal,
        owner=Who.written(args.owner),
        ask=Who.written(args.ask),
        other=Who.written(args.other),
        credential_env=args.credential_env,
        provider=args.provider,
        wakes=PlannedBy(args.wakes),
        **answered,
    )
    chosen = entries() if args.all else [entry(n) for n in args.names]
    for found in chosen:
        print(write(found, team, args.out, replace=args.force))
    print(
        "\nrun one with: minutehand run <file> --agent <agent.yaml> -- <the agent's command>; "
        "minutehand validate <file> checks one without a run"
    )
    return 0


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
        transparent_port=args.transparent_port,
        record_model_calls=args.record_model_calls,
        capture_unknown=UnknownHosts(args.capture_unknown),
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
    from minutehand import doctor  # loaded only here and for `doctor`: it starts nothing, but imports the proxy

    for warning in doctor.docker_warnings(doctor.claimed_hosts(agent), os.environ):
        print(f"minutehand env: warning: {warning}", file=sys.stderr)
    if EnvFormat(args.format) is EnvFormat.REDIRECT:
        if listen.transparent_port is None:
            print("minutehand env: --format redirect needs --transparent-port", file=sys.stderr)
            return 2
        session.environment(agent, state=state, listen=listen)  # refused as the run would be
        print(_redirect_script(listen), end="")
        return 0
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
    script = None
    if listen.transparent_port is not None:
        script = (state / REDIRECT_SCRIPT).resolve()
        script.write_text(_redirect_script(listen), encoding="utf-8")
    print(
        yaml.safe_dump(_compose(args.service, variables, bundle, args.ca_path, in_container, script), sort_keys=False),
        end="",
    )
    return 0


def _redirect_script(listen: session.Listen) -> str:
    from minutehand.adapters.proxy.redirected import redirect_script  # imports mitmproxy, which nothing else here needs

    assert listen.transparent_port is not None
    return redirect_script(listen.reached_at(), listen.transparent_port)


def _compose(
    services: list[str],
    variables: dict[str, str],
    bundle: Path,
    ca_path: str,
    listen: session.Listen,
    script: Path | None,
) -> dict[str, object]:
    """A Compose override file: every named service gets the variables and the CA bundle mounted read-only.
    A service reaching the host as host.docker.internal is given that name on Linux too, where Docker does not
    define it by itself. With `script` (`--transparent-port`), each also gets the redirect script mounted read-only
    and the `NET_ADMIN` capability it needs; the service's own entrypoint runs it, as root, before the agent."""
    volumes = [f"{bundle}:{ca_path}:ro"]
    if script is not None:
        volumes.append(f"{script}:{COMPOSE_REDIRECT_PATH}:ro")
    service: dict[str, object] = {"environment": variables, "volumes": volumes}
    if script is not None:
        service["cap_add"] = ["NET_ADMIN"]
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
    filled = run_all.filled_for_run(args.agent, command, state=state)
    os.environ.update(filled.environment)  # the agent's command is started from this process's environment
    telemetry = _telemetry()
    try:
        outcomes = asyncio.run(
            session.play(
                scenario,
                filled.agent,
                state=state,
                samples=args.samples,
                command=filled.command,
                telemetry=telemetry,
                model=model_from_environment(),
                judge=args.judge,
                listen=_listen(args),
                progress=_progress("run"),
                seed=args.seed,
            )
        )
    finally:
        if telemetry is not None:
            telemetry.shutdown()
    return _report(outcomes, state, as_json=args.json, sampled=args.samples > 1)


def _run_all(args: argparse.Namespace, state: Path, command: list[str] | None) -> int:
    run_all.checked_agent(args.agent)
    batch = asyncio.run(
        run_all.play_all(
            args.folder,
            args.agent,
            state=state,
            command=command,
            jobs=args.jobs,
            judge=args.judge,
            samples=args.samples,
            seed=args.seed,
            passed_on=_passed_on(args),
        )
    )
    print(batch.model_dump_json(indent=2) if args.json else run_all.described(batch))
    return batch.exit_code


def _passed_on(args: argparse.Namespace) -> list[str]:
    """The flags of `run-all` that each of its `minutehand run`s takes as they are. The ports are not among them:
    runs played at once each take free ones."""
    flags = [*(["--proxy-host", args.proxy_host] if args.proxy_host else [])]
    flags += ["--agent-proxy-host", args.agent_proxy_host] if args.agent_proxy_host else []
    flags += [word for host in args.no_proxy for word in ("--no-proxy", host)]
    flags += ["--no-receive-telemetry"] if args.no_receive_telemetry else []
    flags += ["--record-model-calls"] if args.record_model_calls else []
    flags += [word for host in args.model_host for word in ("--model-host", host)]
    flags += [] if args.capture_unknown == UnknownHosts.REFUSE.value else [f"--capture-unknown={args.capture_unknown}"]
    flags += ["--upstream-ca", str(args.upstream_ca)] if args.upstream_ca is not None else []
    return flags


def _progress(command: str) -> Callable[[str], None]:
    """Each step of a restore, as it is taken, on stderr: stdout carries the report, or the JSON."""

    def say(line: str) -> None:
        print(f"minutehand {command}: restore {line}", file=sys.stderr, flush=True)

    return say


def _fork(args: argparse.Namespace, state: Path, command: list[str] | None) -> int:
    changes = load_fork(args.changes, parent_run=args.run_id, at_seq=args.at)
    if args.seed is not None:
        changes = changes.model_copy(update={"seed": args.seed})
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
        print(session.Played(outcomes=[outcome]).model_dump_json(indent=2))
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
    found = session.listed(state)
    if not found:
        print(f"no runs under {state}")
        return 0
    for outcome in found:
        record = outcome.record
        failed = sum(1 for f in outcome.result.findings if f.kind is FindingKind.FAIL)
        print(f"{record.run_id}  {record.scenario}  {record.stop.value}  {failed} failed")
        if record.worlds:
            print(f"  a case of {len(record.worlds)} worlds, scored as one run: {', '.join(record.worlds)}")
        account = session.fork_account(state, record.run_id)
        if account is not None:
            print(
                f"  forked from {account.parent_run} at seq {account.at_seq}, after wake {account.after_wake} "
                f"({account.at:%Y-%m-%d %H:%M} UTC simulated): {account.summary}"
            )
        print(f"  {_restorable_summary(session.fork_points(state, record.run_id))}")
        used = session.usage_of(state, record.run_id)
        print(f"  on disk: {_size(used.rows)} of rows, {_size(used.bodies)} of bodies it alone holds")
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
    for point in found:
        print(f"seq {point.seq}, after wake {point.wake}: {_point(point)}")
    return 0


def _gc(state: Path) -> int:
    collected = session.collect(state)
    freed = collected.freed
    print(f"freed {freed.bodies} stored bodies ({_size(freed.body_bytes)}) across {collected.swept} world files")
    for skipped in collected.skipped:
        print(f"  not swept: {skipped}")
    return 0


def _rm(state: Path, run_ids: list[str]) -> int:
    collected = session.remove(state, run_ids)
    print(f"removed {len(collected.removed)} runs ({_size(collected.removed_bytes)}): {', '.join(collected.removed)}")
    return 0


def _read(args: argparse.Namespace, state: Path) -> int:
    """`query`, `trace` and `explain`: each reads the run's read model, never the run itself (docs/querying.md)."""
    if args.command == "query" and args.schema:
        print(read_model_schema(), end="")
        return 0
    if args.command == "query" and args.run is None:
        print("minutehand query: give a run (and a SELECT, or --export FILE), or --schema", file=sys.stderr)
        return 2
    try:
        prices = load_prices(args.prices) if args.command != "explain" and args.prices is not None else None
        run_id = read_model.resolve(state, args.run)
        db = read_model.open_model(state, run_id, prices)
        try:
            if args.command == "trace":
                wanted = TraceFilter(
                    person=args.person,
                    provider=args.provider,
                    kind=args.kind,
                    since=args.since,
                    until=args.until,
                    wake=args.wake,
                )
                found = trace(db, run_id, wanted)
                print(
                    found.model_dump_json(indent=2) if args.json else traced_lines(found), end="\n" if args.json else ""
                )
                return 0
            if args.command == "explain":
                why = explain(db, run_id, args.seq)
                print(why.model_dump_json(indent=2) if args.json else explanation(why), end="\n" if args.json else "")
                return 0
            if args.export is not None:
                read_model.export(db, args.export)
                print(f"wrote the read model of run {run_id} to {args.export}")
                if args.sql is None:
                    return 0
            if args.sql is None:
                print("minutehand query: give a SELECT after the run", file=sys.stderr)
                return 2
            print(read_model.formatted(read_model.query(db, args.sql), QueryFormat(args.format)), end="")
            return 0
        finally:
            db.close()
    except QueryRefused as e:
        print(f"minutehand {args.command}: {e}", file=sys.stderr)
        return 2


def _restorable_summary(points: list[ForkPoint]) -> str:
    """One line: the seqs a fork can be taken from, and those it cannot."""
    if not points:
        return "no checkpoints"
    can = [str(p.seq) for p in points if isinstance(p.agent, Remembered)]
    cannot = [str(p.seq) for p in points if isinstance(p.agent, NotRestorable)]
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
        capture_unknown=UnknownHosts(args.capture_unknown),
        upstream_ca=args.upstream_ca,
        model_hosts=args.model_host,
        record_model_calls=args.record_model_calls,
    )
    try:
        asyncio.run(standing.serve_forever(state, options))
    except KeyboardInterrupt:
        return 0
    return 0


def _view(state: Path, port: int, prices: Prices | None) -> int:
    from minutehand.adapters.web import app as viewer  # loaded only for this command

    print(f"minutehand: the viewer is at http://127.0.0.1:{port}/ (state {state})", file=sys.stderr)
    viewer.serve(state, port=port, prices=prices)
    return 0


def _report(outcomes: list[Outcome], state: Path, *, as_json: bool, sampled: bool) -> int:
    stable = stability([o.result for o in outcomes]) if sampled else None
    if as_json:
        print(session.Played(outcomes=outcomes, stability=stable).model_dump_json(indent=2))
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
    lines = [f"run {record.run_id}: {record.scenario} (seed {record.seed})", f"  {result.verdict.words}"]
    if restored is not None:
        # The line scripts read since forks were first restored: kept beside the fuller account below.
        verdict = "verified" if restored.verified else f"NOT verified: {restored.unverified}"
        lines.append(f"  the agent was restored from seq {restored.checkpoint_seq}, {verdict}")
    if account is not None:
        lines += [f"  {line}" for line in fork_described(account)]
    lines.append(f"  stopped at {record.ended_at:%Y-%m-%d %H:%M} UTC (simulated) because {_STOPPED[record.stop]}")
    if record.failure is not None:
        lines.append(f"  {record.failure}")
    if record.stop is StopReason.WAKE_LIMIT and record.wake_limit is not None:
        lines.append(
            f"  the wake limit was {record.wake_limit.wakes}: {record.wake_limit.why}; set `max_wakes` in the "
            "scenario, or declare the agent's rhythm (`tick` in the agent file) so the deadline sizes it"
        )
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
    if result.rules_read:
        lines.append("\nrules read")
        lines += [_rule_read(r, result.findings) for r in result.rules_read]
    if result.notes:
        lines.append("\nnotes")
        lines += [f"  {note}" for note in result.notes]
    lines.append("\nscorecard")
    lines += [f"  {line}" for line in _scorecard(result.effectiveness)]
    if points:
        lines.append("\ncheckpoints")
        lines += [f"  seq {p.seq}, after wake {p.wake}: {_point(p)}" for p in points]
    return "\n".join(lines)


def _rule_read(read: RuleRead, findings: list[Finding]) -> str:
    """One of the team's rules: how often it was read, how often it could not be, and how often it broke."""
    broke = sum(1 for f in findings if f.check == read.rule)
    said = f"  {read.rule}: read {read.read} time{'' if read.read == 1 else 's'}"
    said += f", unread {read.unread}" if read.unread else ""
    if broke:
        return said + f", {broke} finding{'' if broke == 1 else 's'}"
    return said + (", held" if read.read else "")


def _point(point: ForkPoint) -> str:
    agent = point.agent
    if isinstance(agent, NotRestorable):
        return f"not restorable: {agent.reason}"
    said = "restorable"
    if agent.outside:
        said += f"; outside its memory, which a fork does not get: {'; '.join(agent.outside)}"
    return said


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
