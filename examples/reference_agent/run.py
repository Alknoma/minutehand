"""Start the reference agent: its API and its worker, two long-running processes, with one command.

    python run.py

Stopped with SIGTERM, it stops both and waits for them (each flushes its telemetry as it goes). If either exits
on its own, the other is stopped and this exits with an error.

REFERENCE_WORKER=detached starts the worker in a session of its own, records its pid in worker.pid, and leaves it
running when this is stopped; a later start finds it alive and does not start another. That is a deployment in
which the worker is a separate service that a restart of the API does not touch, and the reason a restore that
does not restart every process is caught only by a fingerprint that covers what each process holds.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("REFERENCE_HOME", ".")).resolve()
DETACHED = os.environ.get("REFERENCE_WORKER") == "detached"
PIDFILE = HOME / "worker.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _start(name: str, *, detached: bool = False) -> subprocess.Popen[bytes]:
    env = {**os.environ, "REFERENCE_HOME": str(HOME)}
    if "REFERENCE_DB" in env:
        env["REFERENCE_DB"] = str(Path(env["REFERENCE_DB"]).resolve())
    return subprocess.Popen([sys.executable, str(HERE / f"{name}.py")], cwd=HERE, env=env, start_new_session=detached)


def main() -> int:
    api = _start("api")
    worker: subprocess.Popen[bytes] | None = None
    if DETACHED:
        if not (PIDFILE.is_file() and _alive(int(PIDFILE.read_text()))):
            PIDFILE.write_text(str(_start("worker", detached=True).pid))
    else:
        worker = _start("worker")
    children = [p for p in (api, worker) if p is not None]
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        if any(p.poll() is not None for p in children):
            break
        time.sleep(0.1)
    for process in children:
        if process.poll() is None:
            process.terminate()
    for process in children:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    return 0 if stopping else 1


if __name__ == "__main__":
    sys.exit(main())
