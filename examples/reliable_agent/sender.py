"""The sender: one message per purpose, and only what the ledger can source. A purpose sent once is not sent again,
however many times a wake comes round to it, so a repeated wake, a retried event or a model that picks the same move
twice never sends the same thing twice. A message holding a figure from nowhere is written again once, and failing
that, replaced by plain words built from the ledger alone."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import ledger
from moves import Move
from state import State
from work import WORK

PLAIN = {
    "ask": "Hello, could you tell me the {about} for {po} ({item})?",
    "chase": "Following up ({n}) on my question of {since} about {po}: could you tell me the {about}?",
    "tell_stuck": "{po} is waiting on the {about}: {why}.",
    "tell_outcome": "{po} ({item}): {about}.",
}
"""What the agent says when the model's words cannot be sourced: only configured facts and the move itself."""


def send(
    state: State,
    move: Move,
    written: Callable[[], str],
    deliver: Callable[[str, str], None],
    now: datetime,
) -> bool:
    """Write and deliver the message `move` is for, once. Answers whether it went out."""
    assert move.to is not None
    if move.purpose in state.sent:
        state.blocked.append(f"not sent again: {move.purpose}")
        return False
    before = state.said.get(move.to, [])

    def wrong(text: str) -> str | None:
        if not text.strip():
            return "nothing"
        if ledger.unsourced(text, state):
            return f"{ledger.unsourced(text, state)} with no source"
        if text.strip() in before:
            return "the same words as a message they already have"
        return None

    text = written()
    if (why := wrong(text)) is not None:
        state.blocked.append(f"rewritten: {move.purpose} said {why}")
        text = written()
        if wrong(text) is not None:
            state.blocked.append(f"plain words: {move.purpose}")
            wait = state.waits.get(move.about or "")
            text = PLAIN[move.name].format(
                about=(move.about or "").replace("_", " "),
                po=WORK.po,
                item=WORK.item,
                n=(wait.chases + 1) if wait else 1,
                since=wait.asked_at[:10] if wait else now.date().isoformat(),
                why=move.why.removeprefix("tell them "),
            )
    deliver(move.to, text)
    state.sent[move.purpose] = now.isoformat()
    state.said.setdefault(move.to, []).append(text.strip())
    return True
