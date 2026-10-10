"""The facts ledger: every value the agent states to anyone has a source. Its own configuration is one; a person's
words are one (a value it heard is kept only if it appears in what they wrote); a service it read is one. Before
anything goes out, every figure and code in it is matched against the ledger: one that comes from nowhere is not
sent. An agent cannot invent a quote it was never given."""

from __future__ import annotations

import re

from state import State
from work import WORK

FIGURES = re.compile(r"[A-Z]{2,}-\d+|\$?\d[\d,]*(?:\.\d+)?")
"""What a figure or code looks like in text: PO-7731, CC-4410, $48,000, 40, 1200."""


def configured() -> dict[str, dict[str, str]]:
    """The facts the agent starts with: its own configuration."""
    said = {"po": WORK.po, "item": WORK.item, "quantity": str(WORK.quantity), "budget": str(WORK.budget)}
    return {name: {"value": value, "source": "configuration"} for name, value in said.items()}


def heard(state: State, name: str, value: str | None, said: str, source: str) -> bool:
    """Keep a value the model read out of someone's words, only if those words hold it. Answers whether it was kept."""
    if not value or value.strip().lower() not in said.lower():
        state.blocked.append(f"not kept: {name}={value!r} is not in what {source} wrote")
        return False
    state.facts[name] = {"value": value.strip(), "source": source}
    return True


def _norm(token: str) -> str:
    return token.replace("$", "").replace(",", "")


def unsourced(text: str, state: State) -> list[str]:
    """Every figure or code in `text` that no fact in the ledger holds."""
    known = {_norm(t) for fact in state.facts.values() for t in FIGURES.findall(fact["value"])}
    known |= {_norm(f["value"]) for f in state.facts.values()}
    return [t for t in FIGURES.findall(text) if _norm(t) not in known]
