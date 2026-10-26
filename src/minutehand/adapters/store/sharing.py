"""Who is writing a world file, from which machine: a file being written is opened only from the same one.

SQLite's write-ahead log keeps its index in shared memory beside the file (`world.db-shm`), and that memory is shared
only between processes on one kernel. A file written in a container and opened from the host through a bind mount
(Docker Desktop's file sharing) is opened from two kernels: the opener rebuilds the index from what it can see, the
writer then appends over transactions it had already committed, and the log loses them, its later writes intact.
Observed: a container writing 3000 events while the Mac opened the same file read-only lost 154 of them, its head
going back seven times; a second container reading it on the same Docker VM, and no reader, lost none.

So every store that writes a world file says so beside it (`<file>.writing-<token>`, holding the kernel it runs on),
and keeps saying so while it is open, refreshing it every `BEAT`. Every open of a world file, to read or to write,
is refused while a writer on another kernel says so within `STALE`. A writer that died without closing stops
refreshing, and is taken as gone once `STALE` has passed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from functools import cache
from pathlib import Path

from minutehand.application.refusals import RunRefused

BEAT = 5.0
"""Seconds between refreshes of a writer's mark."""

STALE = 30.0
"""Seconds after which a mark nobody refreshed is a writer gone: well past several beats, so a busy machine's late
beat is not taken as one."""

_MARK = ".writing-"


@cache
def kernel() -> str | None:
    """The kernel this process runs on, as boots of it are told apart: Linux's boot id, which every container on one
    VM shares and the VM's host does not, or macOS's boot time. None where neither can be read; such a process is
    taken to share every kernel."""
    linux = Path("/proc/sys/kernel/random/boot_id")
    if linux.is_file():
        return "linux:" + linux.read_text(encoding="ascii").strip()
    if sys.platform == "darwin":
        found = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True, text=True, check=False)
        if found.returncode == 0 and found.stdout.strip():
            return "darwin:" + found.stdout.split("}")[0].strip(" {")
    return None


def refuse_written_elsewhere(path: Path) -> None:
    """Refuse to open `path` while a store on another kernel is writing it."""
    mine = kernel()
    if mine is None:
        return
    now = time.time()  # clock-lint: exempt a writer's mark is refreshed on the machine's clock, and read against it
    for mark in path.parent.glob(f"{path.name}{_MARK}*"):
        try:
            theirs = mark.read_text(encoding="utf-8").strip()
            fresh = now - mark.stat().st_mtime < STALE
        except FileNotFoundError:
            continue  # its writer closed it as we looked
        if fresh and theirs and theirs != mine:
            raise RunRefused(
                f"{path} is being written by a run on another machine ({theirs}; this is {mine}): opening it from "
                "here would cost that run what it has written, as SQLite's log is shared only on one machine. Read "
                "it from where it is written (a container on the same Docker VM: see docs/containers.md, Reading a "
                f"run while it is written), or once the run is over. A writer that is gone stops being one {STALE:.0f} "
                f"seconds after it last said so ({mark})"
            )


class Writing:
    """This process's mark that it writes `path`, refreshed until `close`, and removed then or when the store holding
    it is collected."""

    def __init__(self, path: Path, holder: object) -> None:
        self.mark = path.with_name(f"{path.name}{_MARK}{os.getpid()}-{os.urandom(4).hex()}")
        self.mark.write_text(kernel() or "", encoding="utf-8")
        _Beat.add(self.mark)
        self._done = weakref.finalize(holder, _Beat.remove, self.mark)

    def close(self) -> None:
        self._done()


class _Beat:
    """The one thread that refreshes every mark this process holds.

    `remove` is a store's finalizer, and a collection runs finalizers in whichever thread it interrupts, the one
    holding `_lock` included, so it never takes the lock: it hands the mark to `_gone` (a deque's append is atomic),
    and whoever next holds the lock forgets it."""

    _lock = threading.Lock()
    _marks: set[Path] = set()
    _gone: deque[Path] = deque()
    _thread: threading.Thread | None = None

    @classmethod
    def add(cls, mark: Path) -> None:
        with cls._lock:
            cls._forget()
            cls._marks.add(mark)
            if cls._thread is None:
                cls._thread = threading.Thread(target=cls._run, name="minutehand-writing", daemon=True)
                cls._thread.start()

    @classmethod
    def remove(cls, mark: Path) -> None:
        mark.unlink(missing_ok=True)
        cls._gone.append(mark)

    @classmethod
    def _forget(cls) -> None:
        while cls._gone:
            cls._marks.discard(cls._gone.popleft())

    @classmethod
    def _run(cls) -> None:
        while True:
            time.sleep(BEAT)
            with cls._lock:
                cls._forget()
                marks = list(cls._marks)
            for mark in marks:
                try:
                    os.utime(mark)
                except FileNotFoundError:
                    continue  # removed as we refreshed: closed, or its directory with it
