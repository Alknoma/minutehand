"""What the architecture tests share: the reference agent (examples/reference_agent), its outside world, and the
`minutehand` command they drive it through.

Every test here runs the agent as its own two processes, started by the agent's own `run.py`, and Minutehand as
the installed command, never by calling its internals; what a test reads afterwards it reads from the run's state
directory, as `minutehand findings` would.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import yaml

from minutehand import session
from minutehand.application.memory import memory_of
from minutehand.ports.store import Store
from tests.ports import free_port

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "examples" / "reference_agent"
MINUTEHAND = Path(sys.executable).parent / "minutehand"
PY = sys.executable
RUN = [PY, str(AGENT_DIR / "run.py")]


def outside_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reference_outside", AGENT_DIR / "outside.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["reference_outside"] = module
    spec.loader.exec_module(module)
    return module


MAIL = {
    "host": "api.mail.example",
    "name": "mail",
    "kind": "acknowledge",
    "answer": {"status": 202, "json_body": {"id": "{message_id}"}},
    "message": {
        "recipients": ["personalizations[*].to[*].email"],
        "text": ["content[0].value"],
        "subject": ["subject"],
    },
}
SEARCH = {"host": "search.localhost", "name": "search", "kind": "pass_through"}


GENERATED = {"kind": "generated", "env": "REFERENCE_MAIL_SECRET"}

APPROVER_TOKEN_VARIABLE = "NADIA_APPROVER_TOKEN"
APPROVER_TOKEN = "nadia-signs-in-with-this-3e1f"
"""Nadia's credential in the reference agent's web app for a standing world: the server reads it from its own
environment, and the agent is configured with the same."""


def approvals(port: int) -> dict[str, object]:
    """The reference agent's approvals in its own web app, as `agent.yaml` declares them, on `port`."""
    decide = f"http://127.0.0.1:{port}/approvals/{{item.id}}/decision"
    return {
        "name": "approvals",
        "kind": "http",
        "as_person": {"headers": {"Authorization": "Bearer {person.credential}"}},
        "pending": {
            "request": {"kind": "template", "url": f"http://127.0.0.1:{port}/approvals?approver={{person.email}}"},
            "items": "$.items[*]",
            "id": "$.id",
            "summary": "$.summary",
            "decisions": "$.actions",
            "gates": "$.operation",
            "paging": {"next": "$.next", "param": "cursor"},
        },
        "decisions": [
            {
                "name": "approve",
                "reads": "approved",
                "permits": True,
                "request": {"kind": "template", "method": "POST", "url": decide, "body": {"decision": "approve"}},
            },
            {
                "name": "reject",
                "reads": "rejected",
                "permits": False,
                "request": {
                    "kind": "template",
                    "method": "POST",
                    "url": decide,
                    "body": {"decision": "reject", "reason": "{input.reason}"},
                },
                "inputs": [{"name": "reason", "description": "Why the booking is turned down"}],
            },
        ],
    }


def replies(port: int, secret: Mapping[str, object] = GENERATED) -> dict[str, object]:
    """How a person's answer to the agent's email reaches its inbound webhook, signed as it checks."""
    return {
        "url": f"http://127.0.0.1:{port}/inbound/email",
        "body": {"id": "{reply_id}", "from": "{from}", "text": "{text}", "in_reply_to": "{in_reply_to}"},
        "thread": "id",
        "signing": {
            "secret": dict(secret),
            "header": "X-Mail-Signature",
            "format": "sha256={hex}",
        },
    }


def agent_file(
    path: Path,
    port: int,
    *,
    answered: bool = True,
    secret: Mapping[str, object] = GENERATED,
    wakes: Mapping[str, object] | None = None,
    inbox: bool = False,
    policy: bool = True,
) -> Path:
    """The reference agent's file, on `port`; with `policy`, judged by its team's rules as
    examples/reference_agent/agent.yaml writes them; with `answered`, people can answer its email. No hooks: its
    memory is the run's (`minutehand.agent.store`)."""
    mail = {**MAIL, "replies": replies(port, secret)} if answered else dict(MAIL)
    doc = {
        "name": "venue_booker",
        "goal": {"kind": "by_wake"},
        "wakes": [
            {
                "kind": "reported",
                "wake_url": f"http://127.0.0.1:{port}/wake",
                "report_url": f"http://127.0.0.1:{port}/report",
                "wake_timeout": "PT2M",
                **(wakes or {}),
            }
        ],
        "outbound": [mail, SEARCH],
        "assess": yaml.safe_load((AGENT_DIR / "agent.yaml").read_text())["assess"] if policy else [],
    }
    if inbox:
        doc["inboxes"] = [approvals(port)]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


@dataclass
class Rig:
    """One agent home, one state directory, the outside world's addresses."""

    base: Path
    model: str
    search: str
    ca: str
    port: int = field(default_factory=free_port)

    @property
    def state(self) -> Path:
        return self.base / "state"

    @property
    def home(self) -> Path:
        return self.base / "home"

    def env(self, **more: str) -> dict[str, str]:
        self.home.mkdir(parents=True, exist_ok=True)
        clean = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy") and not k.startswith("OTEL_")}
        return {
            **clean,
            "REFERENCE_PORT": str(self.port),
            "REFERENCE_HOME": str(self.home),
            "REFERENCE_MODEL_URL": self.model,
            "REFERENCE_SEARCH_URL": self.search,
            "REFERENCE_EXTRA_CA": self.ca,
            **more,
        }

    def common(self) -> list[str]:
        return ["--state", str(self.state), "--model-host", "model.localhost", "--upstream-ca", self.ca]

    def agent(self, **options: object) -> Path:
        self.base.mkdir(parents=True, exist_ok=True)
        return agent_file(self.base / "agent.yaml", self.port, **options)  # type: ignore[arg-type]

    def run(self, scenario: str, *extra: str, env: Mapping[str, str] | None = None, **options: object) -> Done:
        """`minutehand run` on one of the example's scenarios, the agent started as `python run.py`."""
        agent = self.agent(**options)
        args = ["run", str(AGENT_DIR / scenario), "--agent", str(agent), *self.common(), *extra, "--", *RUN]
        return minutehand(args, {**self.env(), **(env or {})})

    def fork(
        self, parent: str, at: int, overrides: list[dict[str, object]], *, env: Mapping[str, str] | None = None
    ) -> Done:
        changes = fork_file(self.base / f"fork-{at}-{len(list(self.base.glob('fork-*')))}.yaml", overrides)
        args = ["fork", parent, "--at", str(at), "--changes", str(changes), *self.common(), "--", *RUN]
        return minutehand(args, {**self.env(), **(env or {})})

    def world(self, run_id: str) -> AbstractContextManager[Store]:
        """The run's world, read as `minutehand findings` reads it."""
        return session.reading(self.state, run_id)

    def memory(self, run_id: str, collection: str, *, until: int | None = None) -> dict[str, dict[str, object]]:
        """One collection of the agent's memory in a run, as its log holds it (as of seq `until`), by key."""
        with self.world(run_id) as world:
            held = memory_of(world.events(), until=until)
        return {k: json.loads(v) for (c, k), v in sorted(held.items()) if c == collection}


@dataclass(frozen=True)
class Done:
    code: int
    out: str
    err: str
    seconds: float

    @property
    def run_id(self) -> str:
        first = self.out.splitlines()[0] if self.out else ""
        assert first.startswith("run "), f"no report (exit {self.code}):\n{self.out}\n{self.err[-3000:]}"
        return first.split()[1].rstrip(":")


def minutehand(args: list[str], env: Mapping[str, str], *, timeout: float = 600) -> Done:
    began = time.monotonic()
    done = subprocess.run(
        [str(MINUTEHAND), *args], env=dict(env), cwd=AGENT_DIR, capture_output=True, text=True, timeout=timeout
    )
    return Done(done.returncode, done.stdout, done.stderr, time.monotonic() - began)


def fork_file(path: Path, overrides: list[dict[str, object]]) -> Path:
    path.write_text(yaml.safe_dump({"overrides": overrides}))
    return path


# -- an agent this process starts and drives itself (the standing mode) --------------------------------------------


def get(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=10) as answer:
        return json.loads(answer.read())


def post(url: str, body: Mapping[str, object]) -> None:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


@contextmanager
def started(env: Mapping[str, str], port: int) -> Iterator[subprocess.Popen[bytes]]:
    """The agent's own `run.py`, until it answers /healthz; stopped after."""
    process = subprocess.Popen(RUN, env=dict(env), cwd=AGENT_DIR)
    try:
        give_up = time.monotonic() + 90
        while True:
            try:
                get(f"http://127.0.0.1:{port}/healthz")
                break
            except OSError:
                assert process.poll() is None, "the agent exited before it answered"
                assert time.monotonic() < give_up, "the agent did not answer within 90 s"
                time.sleep(0.05)
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def settled(port: int, *, within: float = 90) -> dict[str, object]:
    give_up = time.monotonic() + within
    while True:
        report = get(f"http://127.0.0.1:{port}/report")
        if report["status"] != "working":
            return report
        assert time.monotonic() < give_up, "the agent was still working"
        time.sleep(0.05)
