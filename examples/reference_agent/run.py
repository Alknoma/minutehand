"""Start the reference agent: its API and its worker, two long-running processes, with one command.

    python run.py

Stopped with SIGTERM, it stops both and waits for them (each flushes its telemetry as it goes). If either exits
on its own, the other is stopped and this exits with an error.
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


def _start(name: str) -> subprocess.Popen[bytes]:
    env = {**os.environ, "REFERENCE_HOME": str(HOME)}
    if "REFERENCE_DB" in env:
        env["REFERENCE_DB"] = str(Path(env["REFERENCE_DB"]).resolve())
    return subprocess.Popen([sys.executable, str(HERE / f"{name}.py")], cwd=HERE, env=env)


def main() -> int:
    children = [_start("api"), _start("worker")]
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
