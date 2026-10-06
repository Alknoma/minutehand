"""The agent's StateHooks, and the commands that bring its world up and down, for an agent whose state is in a
private Firestore emulator. Standard library only; it drives `docker compose` and the emulator's own HTTP API.

    python hooks.py up            start the emulator (building its image if needed) and wait until it answers
    python hooks.py down          remove the emulator's container
    python hooks.py start-agent   start agent.py in the background and wait until it answers GET /report
    python hooks.py stop-agent    stop it
    python hooks.py snapshot      export the emulator into $MINUTEHAND_SNAPSHOT_DIR/firestore
    python hooks.py restore       restart the emulator importing $MINUTEHAND_SNAPSHOT_DIR/firestore
    python hooks.py busy          exits 1: the agent does all its work inside the request that wakes it
    python hooks.py fingerprint   prints a digest of every document's fields, in no particular order, leaving out
                                  the fields VOLATILE names

Google's emulator exports while it runs (POST /_admin/export on its hub), but cannot import while it runs: it
reads an export only as it starts (`--import`). So `restore` copies the snapshot to the shared directory,
restarts the container (start.sh passes `--import` when a snapshot is there), and waits until it answers.

Environment:
    MINUTEHAND_FIRESTORE_PROJECT  the Compose project, and so the container's name (default minutehand-firestore)
    FIRESTORE_EXPORTS             a directory shared with the container as /exports (default ./exports)
    FIRESTORE_PORT, FIRESTORE_HUB_PORT  the host ports of Firestore's REST API and the hub (8080, 4400)
    FIRESTORE_IMAGE               an image to use instead of building Dockerfile
    PORT, AGENT_PIDFILE, AGENT_LOG      for the agent: its port (8710), its pid file and its log
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = os.environ.get("MINUTEHAND_FIRESTORE_PROJECT", "minutehand-firestore")
EXPORTS = Path(os.environ.get("FIRESTORE_EXPORTS", HERE / "exports")).resolve()
FIRESTORE = f"http://127.0.0.1:{os.environ.get('FIRESTORE_PORT', '8080')}"
HUB = f"http://127.0.0.1:{os.environ.get('FIRESTORE_HUB_PORT', '4400')}"
AGENT = f"http://127.0.0.1:{os.environ.get('PORT', '8710')}"
PIDFILE = Path(os.environ.get("AGENT_PIDFILE", "agent.pid"))
AGENT_LOG = Path(os.environ.get("AGENT_LOG", "agent.log"))
IN_CONTAINER = "/exports"
RESTORE = "restore"
VOLATILE: frozenset[str] = frozenset()
"""Fields a restore need not bring back (a timestamp from the machine's clock); this agent writes none. A
document's createTime and updateTime are the emulator's and never part of the digest."""
DOCUMENTS = "/v1/projects/demo-minutehand/databases/(default)/documents"
READY_WITHIN = 300.0


def compose(*args: str) -> None:
    command = ["docker", "compose", "-f", str(HERE / "compose.yaml"), "-p", PROJECT, *args]
    print("$", " ".join(command), flush=True)
    subprocess.run(command, check=True, env={**os.environ, "FIRESTORE_EXPORTS": str(EXPORTS)})


def answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def wait_for(url: str, what: str) -> None:
    began = time.monotonic()
    while not answers(url):
        if time.monotonic() - began > READY_WITHIN:
            sys.exit(f"{what} did not answer {url} within {READY_WITHIN:.0f} s")
        time.sleep(0.25)
    print(f"{what} answered after {time.monotonic() - began:.1f} s", flush=True)


def up() -> None:
    EXPORTS.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(EXPORTS / RESTORE, ignore_errors=True)  # a fresh emulator imports nothing
    compose("up", "-d", "--wait")
    wait_for(FIRESTORE, "the emulator")


def down() -> None:
    compose("down", "--volumes", "--timeout", "5")


def snapshot() -> None:
    """Export the running emulator into the shared directory, copy it into the snapshot, and remove the export
    through the container that wrote it (on Linux its files belong to the container's user)."""
    name = f"snapshot-{uuid.uuid4().hex}"
    request = urllib.request.Request(
        f"{HUB}/_admin/export",
        data=json.dumps({"path": f"{IN_CONTAINER}/{name}", "initiatedBy": "minutehand"}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        print(f"export answered {response.status}", flush=True)
    into = Path(os.environ["MINUTEHAND_SNAPSHOT_DIR"]) / "firestore"
    shutil.rmtree(into, ignore_errors=True)
    shutil.copytree(EXPORTS / name, into)
    compose("exec", "-T", "firestore", "rm", "-rf", f"{IN_CONTAINER}/{name}")
    print(f"snapshot in {into}", flush=True)


def restore() -> None:
    taken = Path(os.environ["MINUTEHAND_SNAPSHOT_DIR"]) / "firestore"
    if not (taken / "firebase-export-metadata.json").is_file():
        sys.exit(f"there is no emulator export at {taken}")
    began = time.monotonic()
    shutil.rmtree(EXPORTS / RESTORE, ignore_errors=True)
    shutil.copytree(taken, EXPORTS / RESTORE)
    compose("restart", "--timeout", "5", "firestore")
    wait_for(FIRESTORE, "the emulator")
    print(f"restored from {taken} in {time.monotonic() - began:.1f} s", flush=True)


def start_agent() -> None:
    log = AGENT_LOG.open("ab")
    process = subprocess.Popen(
        [sys.executable, str(HERE / "agent.py")],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    PIDFILE.write_text(str(process.pid))
    began = time.monotonic()
    while not answers(f"{AGENT}/report"):
        if process.poll() is not None:
            sys.exit(f"the agent exited {process.returncode}; see {AGENT_LOG}")
        if time.monotonic() - began > 30:
            sys.exit(f"the agent did not answer {AGENT}/report within 30 s; see {AGENT_LOG}")
        time.sleep(0.1)
    print(f"the agent is running as {process.pid}", flush=True)


def stop_agent() -> None:
    if not PIDFILE.is_file():
        print("no agent is running", flush=True)
        return
    pid = int(PIDFILE.read_text())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        PIDFILE.unlink()
        return
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, signal.SIGKILL)
    PIDFILE.unlink()
    print(f"stopped the agent ({pid})", flush=True)


def _rest(method: str, path: str, body: object | None = None) -> dict:
    request = urllib.request.Request(
        f"{FIRESTORE}{DOCUMENTS}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"authorization": "Bearer owner", "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def _documents(parent: str) -> list[dict]:
    """Every document under `parent` (the root, or a document), in every collection, recursively."""
    found: list[dict] = []
    for collection in _rest("POST", f"{parent}:listCollectionIds", {"pageSize": 1000}).get("collectionIds", []):
        token = ""
        while True:
            page = _rest("GET", f"{parent}/{collection}?pageSize=300" + (f"&pageToken={token}" if token else ""))
            for document in page.get("documents", []):
                found.append(document)
                found += _documents(parent + "/" + collection + "/" + document["name"].rsplit("/", 1)[1])
            token = page.get("nextPageToken", "")
            if not token:
                break
    return found


def fingerprint() -> None:
    """The same digest for the same documents, whatever order they were written in."""
    held = sorted(
        json.dumps(
            {
                "name": d["name"].split("/documents", 1)[1],
                "fields": {k: v for k, v in d.get("fields", {}).items() if k not in VOLATILE},
            },
            sort_keys=True,
        )
        for d in _documents("")
    )
    print(hashlib.sha256(json.dumps(held).encode()).hexdigest())


def busy() -> None:
    print("idle: the agent works only inside the request that wakes it, and has answered it")
    sys.exit(1)


COMMANDS = {
    "busy": busy,
    "fingerprint": fingerprint,
    "up": up,
    "down": down,
    "snapshot": snapshot,
    "restore": restore,
    "start-agent": start_agent,
    "stop-agent": stop_agent,
}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: hooks.py {'|'.join(COMMANDS)}")
    COMMANDS[sys.argv[1]]()
