"""The wake decider: a Pydantic AI agent that proposes when to wake next, from what is open.

It is told each open thing (what the agent waits for, since when, and when it may next act on it) and proposes one
moment and why. It can weigh what code cannot: a person who usually answers in the afternoon, two things due close
together. It decides nothing alone: `guard.guarded` holds its proposal to the rules.

The model is AGENT_MODEL, any model Pydantic AI names ("openai:gpt-6-luna", with OPENAI_API_KEY and
OPENAI_BASE_URL), or "test" for a stand-in that proposes nothing, so the guard's fallback decides."""

from __future__ import annotations

import os
from datetime import datetime

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from wake.guard import guarded


class Open(BaseModel):
    """One thing the agent is waiting on."""

    what: str = Field(description="What it waits for: someone's answer to a question")
    since: datetime = Field(description="When it began to wait")
    due: datetime = Field(description="The earliest it may act on it again: chase, look again, give up")


class Proposal(BaseModel):
    wake_at: datetime | None = Field(description="When to wake next; null for no wake")
    why: str = Field(description="Why then, in one sentence")


INSTRUCTIONS = """\
You decide when a proactive agent wakes next. You are shown the time now and each thing it is waiting on: since when,
and the earliest it may act on it again. Propose one moment to wake, never before the earliest due moment, and say
why in one sentence. Nothing open, no wake."""

_model_name = os.environ.get("AGENT_MODEL", "test")
decider = Agent(
    TestModel(custom_output_args={"wake_at": None, "why": "offline: the guard decides"})
    if _model_name == "test"
    else _model_name,
    output_type=Proposal,
    instructions=INSTRUCTIONS,
)


def next_wake(open_items: list[Open], now: datetime) -> datetime | None:
    """When to wake next: the decider's proposal, guarded. A decider that fails is no reason not to come back: the
    guard falls back to the earliest due moment."""
    if not open_items:
        return None
    shown = f"It is now {now.isoformat()}.\nWaiting on:\n" + "\n".join(
        f"- {o.what} (since {o.since.isoformat()}; due {o.due.isoformat()})" for o in open_items
    )
    try:
        proposed = decider.run_sync(shown).output.wake_at
    except Exception:
        proposed = None
    return guarded(proposed, [o.due for o in open_items], now)
