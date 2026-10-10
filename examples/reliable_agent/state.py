"""What the agent knows between wakes, kept in `minutehand.agent.store` (in production a SQLite file; under a run, the
run's memory, so a fork restarts it from what it knew there). Read at the start of every wake and every event, and
written back at the end: nothing lives in the process."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from minutehand.agent import store

KEY = "state"


@dataclass
class Wait:
    """Someone owes the agent an answer."""

    person: str
    about: str  # the fact the answer brings: "cost_centre", "quote"
    asked_at: str
    expected_by: str  # when the agent will look again: the wake planner wakes it then
    chases: int = 0


@dataclass
class Request:
    """The approval request, as the agent last read it from the service."""

    id: str
    status: str
    read_at: str
    next_check: str  # the wake planner wakes it then to read the request again
    unchanged_reads: int = 0
    asks_for: list[str] = field(default_factory=list)  # what an ask-back wants: "quote", "cost_centre"
    asked_back: int = 0  # how many times the approver has asked back


@dataclass
class State:
    facts: dict[str, dict[str, str]] = field(default_factory=dict)  # name -> {value, source}: the ledger
    waits: dict[str, Wait] = field(default_factory=dict)  # by the fact it waits for
    request: Request | None = None
    order: str | None = None
    sent: dict[str, str] = field(default_factory=dict)  # purpose -> when: the sender's memory
    said: dict[str, list[str]] = field(default_factory=dict)  # person -> every message sent them
    inbox: list[dict[str, str]] = field(default_factory=list)  # replies heard, not yet read by a wake
    blocked: list[str] = field(default_factory=list)  # what the structure stopped, for anyone looking
    retry_at: str | None = None  # a wake its model failed in is tried again then

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> State:
        request = raw.get("request")
        return cls(
            facts=raw.get("facts", {}),
            waits={k: Wait(**v) for k, v in raw.get("waits", {}).items()},
            request=Request(**request) if request else None,
            order=raw.get("order"),
            sent=raw.get("sent", {}),
            said=raw.get("said", {}),
            inbox=raw.get("inbox", []),
            blocked=raw.get("blocked", []),
            retry_at=raw.get("retry_at"),
        )


def load() -> State:
    raw = store.get(KEY)
    return State.from_json(raw) if isinstance(raw, dict) else State()


def save(state: State) -> None:
    store.put(KEY, state.to_json())
