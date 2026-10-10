"""The agent's model: an OpenAI-compatible chat-completions API (AGENT_MODEL_BASE_URL, AGENT_MODEL, AGENT_MODEL_API_KEY).
It does three things, each answered as JSON: pick one move from the menu, write a message from the facts it is given,
and read a value out of what someone wrote. It decides nothing the structure has not offered it."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from moves import Move
from state import State
from work import INSTRUCTIONS

BASE = os.environ.get("AGENT_MODEL_BASE_URL", "https://api.openai.com/v1")
MODEL = os.environ.get("AGENT_MODEL", "gpt-4o-mini")
KEY = os.environ.get("AGENT_MODEL_API_KEY", "")


def _ask(task: str, shown: str) -> dict[str, Any]:
    body = {
        "model": MODEL,
        "temperature": 0,
        "messages": [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": f"{task}\n\n{shown}"}],
    }
    answer = httpx.post(f"{BASE}/chat/completions", json=body, headers={"authorization": f"Bearer {KEY}"}, timeout=120)
    answer.raise_for_status()
    text = answer.json()["choices"][0]["message"]["content"] or "{}"
    found = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(found.group(0)) if found else {}
    except json.JSONDecodeError:
        return {}


def _facts(state: State) -> str:
    return "\n".join(f"- {name}: {fact['value']}" for name, fact in sorted(state.facts.items())) or "- none yet"


def choose(menu: list[Move], state: State, now: str) -> str | None:
    shown = f"It is now {now}.\nFacts you hold:\n{_facts(state)}\nMoves open to you now:\n" + "\n".join(
        f"- {m.name} ({m.purpose}): {m.why}" for m in menu
    )
    return _ask('Pick one move. Answer {"move": "<its purpose, exactly as listed>"}.', shown).get("move")


def write(move: Move, state: State, now: str) -> str:
    shown = f"It is now {now}.\nFacts you hold:\n{_facts(state)}\nYou are writing to {move.to} to: {move.why}."
    return str(_ask('Write the message. Use only the facts listed. Answer {"text": "..."}.', shown).get("text", ""))


def extract(about: str, said: str) -> str | None:
    shown = f"They wrote:\n{said}"
    value = _ask(f'Give the {about} they state, exactly as written, or null. Answer {{"value": ...}}.', shown).get(
        "value"
    )
    return value if isinstance(value, str) else None
