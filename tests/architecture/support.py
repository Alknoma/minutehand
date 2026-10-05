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
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import yaml

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "examples" / "reference_agent"
MINUTEHAND = Path(sys.executable).parent / "minutehand"
PY = sys.executable
RUN = [PY, str(AGENT_DIR / "run.py")]
HOOKS = str(AGENT_DIR / "hooks.py")


def outside_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reference_outside", AGENT_DIR / "outside.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["reference_outside"] = module
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


MAIL = {
    "host": "api.mail.example",
    "name": "mail",
    "kind": "acknowledge",
    "answer": {"status": 202, "json_body": {"id": "queued"}},
    "message": {
        "recipients": ["personalizations[*].to[*].email"],
        "text": ["content[0].value"],
        "subject": ["subject"],
    },
}
SEARCH = {"host": "search.localhost", "name": "search", "kind": "pass_through"}


def agent_file(
    path: Path, port: int, *, state: Mapping[str, object] | None = None, mail: Mapping[str, object] = MAIL
) -> Path:
    """The reference agent's file, on `port`, its hooks run by this interpreter."""
    hooks: dict[str, object] = {
        "snapshot": [PY, HOOKS, "snapshot"],
        "restore": [PY, HOOKS, "restore"],
        "quiet": "PT0.5S",
        "settle_limit": "PT30S",
        "answer_limit": "PT30S",
    }
    hooks.update(state or {})
    doc = {
        "name": "venue_booker",
        "goal": {"kind": "by_wake"},
        "wakes": [
            {
                "kind": "reported",
                "wake_url": f"http://127.0.0.1:{port}/wake",
                "report_url": f"http://127.0.0.1:{port}/report",
            }
        ],
        "outbound": [dict(mail), SEARCH],
        "state": hooks,
    }
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
        give_up = time.monotonic() + 30
        while True:
            try:
                get(f"http://127.0.0.1:{port}/healthz")
                break
            except OSError:
                assert process.poll() is None, "the agent exited before it answered"
                assert time.monotonic() < give_up, "the agent did not answer within 30 s"
                time.sleep(0.05)
        yield process
    finally:
        process.terminate()
        process.wait(timeout=15)


def settled(port: int, *, within: float = 30) -> dict[str, object]:
    give_up = time.monotonic() + within
    while True:
        report = get(f"http://127.0.0.1:{port}/report")
        if report["status"] != "working":
            return report
        assert time.monotonic() < give_up, "the agent was still working"
        time.sleep(0.05)
