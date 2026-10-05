"""What an outsider has to drive Minutehand with: the installed `minutehand` command, the control API's client,
the viewer's JSON API, and files written the way the documentation writes them.

Every process started here is reaped by the context that started it, every port is free when picked, and every
state directory is the test's own, so tests run in parallel on one machine.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from minutehand.testing.client import MinutehandClient

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EXAMPLES = REPO / "examples"
STANDIN = HERE / "agents" / "standin.py"
PYTHON = sys.executable
MINUTEHAND = str(Path(sys.executable).parent / "minutehand")

EXIT = {"passed": 0, "failed": 1, "environment_failed": 2, "unfinished": 3, "tool_failed": 4, "not_judged": 5}
"""The exit code of each verdict, as `docs/design.md` ("The verdict") states it."""

WORDS = {
    "passed": "Passed:",
    "failed": "Failed:",
    "environment_failed": "Environment failed:",
    "unfinished": "Not finished:",
    "tool_failed": "Not scored:",
    "not_judged": "Not judged",
}
"""How each verdict's sentence begins, as the documentation prints it."""

PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "no_grpc_proxy")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def clean_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """This process's environment without anything a previous Minutehand or proxy left in it, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINUTEHAND") and k.lower() not in PROXY_VARIABLES}
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra or {})
    return env


def write_yaml(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


@dataclass(frozen=True)
class Finished:
    """One finished `minutehand` command: its exit code, its output, and its JSON when it printed one."""

    exit: int
    stdout: str
    stderr: str

    def json(self) -> dict[str, object]:
        loaded = json.loads(self.stdout)
        assert isinstance(loaded, dict), self.stdout[:2000]
        return loaded

    @property
    def outcome(self) -> dict[str, object]:
        """The first (only) outcome of a `run --json` or `fork --json`."""
        outcomes = self.json()["outcomes"]
        assert isinstance(outcomes, list) and outcomes, self.stdout[:2000]
        first = outcomes[0]
        assert isinstance(first, dict)
        return first

    @property
    def run_id(self) -> str:
        record = self.outcome["record"]
        assert isinstance(record, dict)
        return str(record["run_id"])

    @property
    def result(self) -> dict[str, object]:
        result = self.outcome["result"]
        assert isinstance(result, dict)
        return result

    @property
    def verdict(self) -> dict[str, object]:
        verdict = self.result["verdict"]
        assert isinstance(verdict, dict)
        return verdict

    def findings(self, kind: str | None = None) -> list[dict[str, object]]:
        found = self.result["findings"]
        assert isinstance(found, list)
        return [f for f in found if isinstance(f, dict) and (kind is None or f["kind"] == kind)]

    def explain(self) -> str:
        return f"exit {self.exit}\n--- stdout\n{self.stdout[-6000:]}\n--- stderr\n{self.stderr[-6000:]}"


def minutehand(
    *args: str, env: Mapping[str, str] | None = None, cwd: Path | None = None, timeout: float = 50
) -> Finished:
    """Run the installed command to its end."""
    done = subprocess.run(
        [MINUTEHAND, *args],
        env=clean_environment(env),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return Finished(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class Standin:
    """The stand-in agent's file and command, on a port of its own."""

    agent_file: Path
    command: list[str]
    env: dict[str, str]


def standin(tmp_path: Path, *, outbound: Sequence[object] = (), **behaviour: str) -> Standin:
    """The stand-in agent (`agents/standin.py`), with its behaviour as `STANDIN_<NAME>` variables."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    agent: dict[str, object] = {
        "name": "standin",
        "goal": {"kind": "by_wake"},
        "wakes": [{"kind": "reported", "wake_url": f"{base}/wake", "report_url": f"{base}/report"}],
        "inbound": [
            {
                "provider": "slack",
                "url": f"{base}/slack/events",
                "secret": {"kind": "generated", "env": "STANDIN_SECRET"},
            }
        ],
    }
    if outbound:
        agent["outbound"] = list(outbound)
    env = {"STANDIN_PORT": str(port), **{f"STANDIN_{k.upper()}": v for k, v in behaviour.items()}}
    return Standin(write_yaml(tmp_path / "agent.yaml", agent), [PYTHON, str(STANDIN)], env)


def run_standin(
    tmp_path: Path, scenario: Mapping[str, object], agent: Standin, *extra: str, timeout: float = 50
) -> Finished:
    """`minutehand run <scenario> --agent <agent> --json -- python standin.py`, in the test's own state directory."""
    written = write_yaml(tmp_path / "scenario.yaml", dict(scenario))
    return minutehand(
        "run",
        str(written),
        "--agent",
        str(agent.agent_file),
        "--state",
        str(tmp_path / "state"),
        "--json",
        *extra,
        "--",
        *agent.command,
        env=agent.env,
        cwd=tmp_path,
        timeout=timeout,
    )


def person(key: str, email: str, reply: Mapping[str, object], **more: object) -> dict[str, object]:
    return {"key": key, "name": key.title() + " Example", "email": email, "reply": dict(reply), **more}


def scripted(*replies: Mapping[str, object], hours: float | None = None, **more: object) -> dict[str, object]:
    written: dict[str, object] = {"kind": "scripted", "replies": [dict(r) for r in replies], **more}
    if hours is not None:
        written["delay"] = {"shortest": f"PT{hours}H", "longest": f"PT{hours}H"}
    return written


SILENT = {"kind": "silent"}
TOLD_ONLY = {"kind": "scripted", "replies": []}
"""Someone who is told things and owes no answer."""

ROSA = "rosa@example.com"
OWEN = "owen@example.com"
START = "2026-08-24T09:00:00Z"  # a Monday


def scenario(
    name: str, *people: Mapping[str, object], expect: Sequence[object] = (), **more: object
) -> dict[str, object]:
    return {
        "name": name,
        "goal": "Confirm the venue for the offsite with Rosa and tell Owen.",
        "owner": "owen",
        "starts_at": START,
        "deadline_after": "P14D",
        "people": [dict(p) for p in people],
        "expect": list(expect),
        **more,
    }


def _reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _wait_until_answers(url: str, process: subprocess.Popen[bytes], log: Path, within: float = 30) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"{url} exited {process.returncode} before answering:\n{log.read_text()[-4000:]}")
        try:
            if httpx.get(url, timeout=1, trust_env=False).status_code < 500:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"{url} did not answer within {within}s:\n{log.read_text()[-4000:]}")


@dataclass(frozen=True)
class Served:
    """A `minutehand serve` process: its control API's client, its proxy, its state directory and its log."""

    client: MinutehandClient
    control: str
    proxy_port: int
    state: Path
    log: Path

    def environment(self, ca_path: Path) -> dict[str, str]:
        """What a service started now needs to reach the fakes: the server's own variables, its CA written out."""
        ca_path.write_bytes(self.client.ca())
        return self.client.environment(str(ca_path))


@contextmanager
def served(tmp_path: Path, *options: str) -> Iterator[Served]:
    """`minutehand serve` on ports of its own, stopped and reaped afterwards."""
    state = tmp_path / "served"
    state.mkdir(parents=True, exist_ok=True)
    proxy, control, telemetry = free_port(), free_port(), free_port()
    log = tmp_path / "serve.log"
    with log.open("wb") as out:
        process = subprocess.Popen(
            [
                MINUTEHAND,
                "serve",
                "--state",
                str(state),
                "--host",
                "127.0.0.1",
                "--proxy-port",
                str(proxy),
                "--control-port",
                str(control),
                "--telemetry-port",
                str(telemetry),
                *options,
            ],
            env=clean_environment(),
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        url = f"http://127.0.0.1:{control}"
        _wait_until_answers(f"{url}/v1/health", process, log)
        with MinutehandClient(url, timeout=60) as client:
            yield Served(client, url, proxy, state, log)
    finally:
        _reap(process)


@dataclass(frozen=True)
class Viewer:
    """`minutehand view` over a state directory: its JSON API, read as an outsider reads it."""

    url: str

    def get(self, path: str) -> dict[str, object]:
        answered = httpx.get(self.url + path, timeout=30, trust_env=False)
        assert answered.status_code == 200, f"GET {path}: {answered.status_code} {answered.text[:500]}"
        loaded = answered.json()
        assert isinstance(loaded, dict)
        return loaded


@contextmanager
def viewer(tmp_path: Path, state: Path) -> Iterator[Viewer]:
    port = free_port()
    log = tmp_path / f"view-{port}.log"
    with log.open("wb") as out:
        process = subprocess.Popen(
            [MINUTEHAND, "view", "--port", str(port), "--state", str(state)],
            env=clean_environment(),
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        url = f"http://127.0.0.1:{port}"
        _wait_until_answers(url + "/api/runs", process, log)
        yield Viewer(url)
    finally:
        _reap(process)


@contextmanager
def started(command: Sequence[str], env: Mapping[str, str], log: Path, *, ready: str | None = None) -> Iterator[None]:
    """A program of the test's own (an agent the test drives itself), reaped afterwards."""
    with log.open("wb") as out:
        process = subprocess.Popen(list(command), env=clean_environment(env), stdout=out, stderr=subprocess.STDOUT)
    try:
        if ready is not None:
            _wait_until_answers(ready, process, log)
        yield
    finally:
        _reap(process)


def through_proxy(server: Served, ca: Path) -> httpx.Client:
    """An HTTP client configured as a service under `serve` is: the proxy, and the CA bundle the server hands out."""
    ca.write_bytes(server.client.ca())
    trusted = ssl.create_default_context(cafile=str(ca))
    return httpx.Client(proxy=f"http://127.0.0.1:{server.proxy_port}", verify=trusted, trust_env=False, timeout=30)


COMMAND_AGENT = HERE / "agents" / "command_agent.py"
EMULATOR = HERE / "agents" / "emulator.py"
PLUGIN = HERE / "agents" / "plugin"


def installed_ledger_fake(tmp_path: Path) -> dict[str, str]:
    """The environment under which `acceptance_ledger_fake` is an installed provider package: its distribution's
    metadata names it under the entry-point group `minutehand.providers`, as a package installed by pip would."""
    site = tmp_path / "site"
    info = site / "acceptance_ledger_fake-1.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: acceptance-ledger-fake\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text("[minutehand.providers]\nledger = acceptance_ledger_fake\n")
    return {"PYTHONPATH": os.pathsep.join([str(site), str(PLUGIN)])}


def command_agent(
    tmp_path: Path, *calls: str, outbound: Sequence[object] = (), emulators: Sequence[object] = ()
) -> tuple[Path, dict[str, str]]:
    """The agent file of `agents/command_agent.py`, and the environment that makes `calls` on every wake."""
    agent: dict[str, object] = {
        "name": "command_agent",
        "wakes": [{"kind": "command", "argv": [PYTHON, str(COMMAND_AGENT)]}],
    }
    if outbound:
        agent["outbound"] = list(outbound)
    if emulators:
        agent["emulators"] = list(emulators)
    return write_yaml(tmp_path / "agent.yaml", agent), {
        "AGENT_CALLS": ",".join(calls),
        "AGENT_SEEN": str(tmp_path / "seen.jsonl"),
    }


def seen(tmp_path: Path) -> list[dict[str, object]]:
    """What `agents/command_agent.py` was answered, call by call."""
    return [json.loads(line) for line in (tmp_path / "seen.jsonl").read_text().splitlines()]
