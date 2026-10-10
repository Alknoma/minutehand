"""Where the reference agent keeps everything it knows: `minutehand.agent.store`, shared by its API and its worker.

    REFERENCE_DB=path/to/agent.db       where the memory lives in production: a SQLite file both processes open
                                        (the default, `agent.db` in REFERENCE_HOME)

Under Minutehand (MINUTEHAND_ON set) the file is never opened: the memory is the run's, recorded in its log, so a fork
starts from the agent's memory as it stood at the checkpoint with no hooks. Any other database is another adapter
of three methods (`minutehand.agent.store.Backend`); a Firestore one is a few lines over its client.

Six collections, each holding JSON objects of strings:

    facts     what the job is and where it stands: the goal, the owner, the venue, whether it is done, the next
              moment it wants to be woken, and the job queue's counters (`job_counter`, `job_claimed`, `job_done`)
    jobs      the queue the worker polls: one job per wake, queued by the API, run by the worker, by number
    sent      every email the agent sent, with the message id the email API gave it
    replies   every email answer that reached the inbound webhook
    notes     what the owner said along the way
    approvals what waits on a person's approval in the agent's own web app before the agent may go ahead

The API alone queues jobs and moves `job_counter`; the worker alone claims and finishes them and moves the other two.
So "is any job in flight" is one read of the counters, which the API's report and the worker's poll both make.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from minutehand.agent import store


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, object]


def _row(value: object) -> dict[str, str]:
    assert isinstance(value, dict)
    return {str(k): str(v) for k, v in value.items()}


def _number(found: object) -> int:
    return int(str(found)) if found is not None else 0


class Store:
    """The agent's six collections over `minutehand.agent.store`."""

    def __init__(self) -> None:
        self._facts = store.collection("facts")
        self._jobs = store.collection("jobs")
        self._sent = store.collection("sent")
        self._replies = store.collection("replies")
        self._notes = store.collection("notes")
        self._approvals = store.collection("approvals")
        self._lock = threading.Lock()

    def fact(self, key: str) -> str | None:
        value = self._facts.get(key)
        return value if isinstance(value, str) else None

    def set_facts(self, values: dict[str, str | None]) -> None:
        with self._facts.batch() as b:
            for key, value in values.items():
                if value is None:
                    b.delete(key)
                else:
                    b.put(key, value)

    def enqueue(self, kind: str, payload: dict[str, object]) -> int:
        with self._lock:
            number = _number(self.fact("job_counter")) + 1
            with self._facts.batch() as b:
                b.put(f"{number:08d}", {"kind": kind, "payload": payload, "state": "queued"}, collection="jobs")
                b.put("job_counter", str(number))
        return number

    def claim(self) -> Job | None:
        """The oldest queued job, now running; None when none is queued."""
        claimed = _number(self.fact("job_claimed"))
        if _number(self.fact("job_counter")) <= claimed:
            return None
        number = claimed + 1
        job = self._jobs.get(f"{number:08d}")
        assert isinstance(job, dict)
        with self._facts.batch() as b:
            b.put(f"{number:08d}", {**job, "state": "running"}, collection="jobs")
            b.put("job_claimed", str(number))
        payload = job["payload"]
        assert isinstance(payload, dict)
        return Job(number, str(job["kind"]), {str(k): v for k, v in payload.items()})

    def finish(self, job: Job) -> None:
        held = self._jobs.get(f"{job.id:08d}")
        assert isinstance(held, dict)
        with self._facts.batch() as b:
            b.put(f"{job.id:08d}", {**held, "state": "done"}, collection="jobs")
            b.put("job_done", str(job.id))

    def in_flight(self) -> int:
        """Jobs queued or running."""
        return _number(self.fact("job_counter")) - _number(self.fact("job_done"))

    def queued(self) -> int:
        """Jobs queued and not yet picked up."""
        return _number(self.fact("job_counter")) - _number(self.fact("job_claimed"))

    def add_sent(self, row: dict[str, str]) -> None:
        self._sent.put(row["id"], row)

    def sent(self) -> list[dict[str, str]]:
        return sorted((_row(v) for _, v in self._sent.list()), key=lambda r: (r["at"], r["id"]))

    def add_reply(self, row: dict[str, str]) -> bool:
        """Keep a reply once: False when it was already taken (a webhook delivered twice)."""
        with self._lock:
            if self._replies.get(row["id"]) is not None:
                return False
            self._replies.put(row["id"], {**row, "read": "0"})
        return True

    def replies(self) -> list[dict[str, str]]:
        return [_row(v) for _, v in self._replies.list()]

    def mark_read(self, reply_id: str) -> None:
        held = self._replies.get(reply_id)
        if held is not None:
            self._replies.put(reply_id, {**_row(held), "read": "1"})

    def add_note(self, row: dict[str, str]) -> None:
        if self._notes.get(row["id"]) is None:
            self._notes.put(row["id"], row)

    def add_approval(self, row: dict[str, str]) -> None:
        if self._approvals.get(row["id"]) is None:
            self._approvals.put(row["id"], {**row, "state": "pending", "reason": ""})

    def approvals(self) -> list[dict[str, str]]:
        return [_row(v) for _, v in self._approvals.list()]

    def decide_approval(self, approval_id: str, state: str, reason: str) -> bool:
        with self._lock:
            held = self._approvals.get(approval_id)
            if held is None or _row(held)["state"] != "pending":
                return False
            self._approvals.put(approval_id, {**_row(held), "state": state, "reason": reason})
        return True


def open_store() -> Store:
    home = Path(os.environ.get("REFERENCE_HOME", "."))
    store.configure(store.SqliteBackend(Path(os.environ.get("REFERENCE_DB", str(home / "agent.db")))))
    return Store()
