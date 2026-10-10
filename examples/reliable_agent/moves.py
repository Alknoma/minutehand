"""The action menu: the moves open to the agent now, worked out from its state and nothing else. The model picks
among them and can pick nothing else, so a move the world does not allow yet is never on offer: no order while the
approval is pending or was refused, no chase before an answer is due, no second ask while one is open.

Reading is not a move: the agent reads a held request whenever the wake planner said to, before it chooses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from state import State
from work import WORK

MOST_CHASES = 2
"""Follow-ups to one person on one question; after them the requester is told what the agent is stuck on."""
MOST_ASKS_BACK = 2
"""Times the agent answers an approver's ask-back with what it holds; asked again after that, it tells the
requester instead of sending the same again."""


@dataclass(frozen=True)
class Move:
    name: str
    purpose: str  # what it is for: the sender sends one message per purpose
    to: str | None = None  # the person it writes to, if it writes
    about: str | None = None  # the fact it asks for or the state it reports
    why: str = ""


def _due(state: State, about: str, now: datetime) -> bool:
    wait = state.waits.get(about)
    return wait is not None and now >= datetime.fromisoformat(wait.expected_by)


def legal(state: State, now: datetime) -> list[Move]:
    moves: list[Move] = []
    facts, request = state.facts, state.request
    if "cost_centre" not in facts and "cost_centre" not in state.waits:
        moves.append(
            Move("ask", "ask:finance:cost_centre", WORK.finance, "cost_centre", "ask them for the cost centre")
        )
    for about, wait in state.waits.items():
        if not _due(state, about, now):
            continue
        if wait.chases < MOST_CHASES:
            moves.append(
                Move(
                    "chase",
                    f"chase:{wait.person}:{about}:{wait.chases + 1}",
                    wait.person,
                    about,
                    f"follow up: their answer on the {about.replace('_', ' ')} was due",
                )
            )
        elif wait.person != WORK.requester and f"tell:requester:stuck:{about}" not in state.sent:
            moves.append(
                Move(
                    "tell_stuck",
                    f"tell:requester:stuck:{about}",
                    WORK.requester,
                    about,
                    f"tell them the purchase waits on the {about.replace('_', ' ')}, which {wait.person} has not given "
                    f"after {wait.chases} follow-ups",
                )
            )
    if "cost_centre" in facts and request is None:
        moves.append(Move("file", "file:request", why="the cost centre is in hand and nothing is filed"))
    if request is not None and request.status == "needs_info":
        missing = [a for a in request.asks_for if a not in facts]
        for about in missing:
            if about not in state.waits and f"ask:requester:{about}" not in state.sent:
                moves.append(
                    Move(
                        "ask",
                        f"ask:requester:{about}",
                        WORK.requester,
                        about,
                        f"ask them for the {about.replace('_', ' ')}, which the approver asked for",
                    )
                )
        if not missing:
            given = ",".join(f"{a}={facts[a]['value']}" for a in request.asks_for)
            if request.asked_back <= MOST_ASKS_BACK and f"resubmit:{request.asked_back}" not in state.sent:
                moves.append(
                    Move(
                        "resubmit",
                        f"resubmit:{request.asked_back}",
                        why=f"the approver asked for {', '.join(request.asks_for)}, which it holds: {given}",
                    )
                )
            elif request.asked_back > MOST_ASKS_BACK and f"tell:requester:asked_again:{request.id}" not in state.sent:
                asked = ", ".join(a.replace("_", " ") for a in request.asks_for)
                moves.append(
                    Move(
                        "tell_stuck",
                        f"tell:requester:asked_again:{request.id}",
                        WORK.requester,
                        asked,
                        f"tell them the approver keeps asking for the {asked}, already sent {MOST_ASKS_BACK} times",
                    )
                )
    if request is not None and request.status == "approved" and state.order is None:
        moves.append(Move("order", "order", why="the request is approved, as read from the service this wake"))
    outcome = "ordered" if state.order else ("rejected" if request and request.status == "rejected" else None)
    if outcome and f"tell:requester:{outcome}" not in state.sent:
        moves.append(
            Move(
                "tell_outcome",
                f"tell:requester:{outcome}",
                WORK.requester,
                outcome,
                f"tell them the purchase was {outcome}",
            )
        )
    return moves
