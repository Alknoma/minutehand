"""Change an agent's prompt or model in the request it sends to its model API.

Three wire shapes carry a system prompt, all of them the model providers' own:

- OpenAI chat completions: `messages[0]` with role `system` or `developer`, its
  `content` a string or a list of `{"type": "text", "text": ...}` parts.
- OpenAI responses: top-level `instructions`, a string.
- Anthropic messages: top-level `system`, a string or a list of text blocks.

Everything the edits do not touch is left as it was: a body no edit applies to is
returned unchanged byte for byte, and an edited body is the same JSON value apart
from the edited strings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from minutehand.adapters.proxy.policy import ModelEdit
from minutehand.domain.experiment import CallMatch, ModelSwap, PromptPatch

_SYSTEM_ROLES = ("system", "developer")


@dataclass
class _Slot:
    """One string inside the parsed body that is part of the system prompt."""

    holder: dict[str, object]
    key: str

    def read(self) -> str:
        value = self.holder[self.key]
        assert isinstance(value, str)
        return value

    def write(self, value: str) -> None:
        self.holder[self.key] = value


def _member(holder: dict[str, object], key: str) -> object:
    """One field of the model API's own JSON; None when it is absent."""
    return holder[key] if key in holder else None


def _text_slots(value: object, holder: dict[str, object], key: str) -> list[_Slot]:
    """A string field is one slot; a list of text blocks is one slot per block."""
    if isinstance(value, str):
        return [_Slot(holder, key)]
    if isinstance(value, list):
        slots: list[_Slot] = []
        for block in value:
            if isinstance(block, dict) and _member(block, "type") == "text" and isinstance(_member(block, "text"), str):
                slots.append(_Slot(block, "text"))
        return slots
    return []


def _system_slots(body: dict[str, object]) -> list[_Slot]:
    if "system" in body:
        return _text_slots(body["system"], body, "system")
    if "instructions" in body:
        return _text_slots(body["instructions"], body, "instructions")
    messages = _member(body, "messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict) and _member(first, "role") in _SYSTEM_ROLES and "content" in first:
            return _text_slots(first["content"], first, "content")
    return []


def _matches(where: CallMatch, host: str, model: str | None, system: str | None) -> bool:
    if where.host is not None and where.host.lower() != host.lower():
        return False
    if where.model is not None and where.model != model:
        return False
    return not (where.system_contains is not None and (system is None or where.system_contains not in system))


def apply_edits(body: bytes, host: str, edits: list[ModelEdit]) -> bytes | None:
    """The edited body, or None when no edit applies and the request goes on untouched."""
    if not edits or not body:
        return None
    try:
        parsed: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    sent_model = _member(parsed, "model")
    model = sent_model if isinstance(sent_model, str) else None
    slots = _system_slots(parsed)
    system = "\n".join(slot.read() for slot in slots) if slots else None
    # Every edit is matched against the request as the agent sent it, so a swap of
    # the model cannot change which prompt patches apply after it.
    chosen = [e for e in edits if _matches(e.where, host, model, system)]
    changed = False
    for edit in chosen:
        if isinstance(edit, ModelSwap):
            if model != edit.to:
                parsed["model"] = edit.to
                changed = True
        elif isinstance(edit, PromptPatch) and slots:
            if edit.find is None:
                slots[-1].write(slots[-1].read() + edit.text)
                changed = True
            else:
                for slot in slots:
                    before = slot.read()
                    after = before.replace(edit.find, edit.text)
                    if after != before:
                        slot.write(after)
                        changed = True
    if not changed:
        return None
    return json.dumps(parsed, ensure_ascii=False).encode()
