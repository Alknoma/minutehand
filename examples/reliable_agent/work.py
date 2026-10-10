"""The agent's work, as its own configuration states it. Nothing hands it this at run time: it is what this agent is
for, as a production agent's prompt and settings are. Every value here is a fact the agent may state (`ledger`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class Work:
    po: str = "PO-7731"
    item: str = "40 laptops for the new starters"
    quantity: int = 40
    budget: int = 48000
    requester: str = "owen@example.com"  # who asked for it, and is told how it went
    finance: str = "sam@example.com"  # who knows the cost centre
    approvals: str = "https://api.approvals.example"
    orders: str = "https://api.orders.example"
    timezone: str = "Europe/London"  # the hours it works in, and expects people to answer in
    opens: time = time(9, 0)
    closes: time = time(17, 30)


WORK = Work()

INSTRUCTIONS = f"""\
You run one purchase for {WORK.requester}: {WORK.item} ({WORK.po}), budget ${WORK.budget:,}. You need the cost \
centre from {WORK.finance}, then approval through the approvals service, then the order, then you tell \
{WORK.requester} how it went. Each time, you are shown the moves open to you now and the facts you hold. Pick one \
move by its name. When you write to someone, write only facts you are shown, briefly and plainly."""
"""What the agent tells its model: its work and how it works. The moves and facts arrive with each call."""
