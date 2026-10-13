"""Change an agent's prompt or model in the request it sends to its model API.

Three wire shapes carry a system prompt, all of them the model providers' own:

- OpenAI chat completions: `messages[0]` with role `system` or `developer`, its
  `content` a string or a list of `{"type": "text", "text": ...}` parts.
- OpenAI responses: top-level `instructions`, a string.
- Anthropic messages: top-level `system`, a string or a list of text blocks.

And the newest message (`PromptPart.LAST_MESSAGE`), for a patch to a transcript carried across turns, where a
changed system prompt would be answered against a history written under the old one:

- OpenAI chat completions and Anthropic messages: the last of `messages` whose role is not the system's, its
  `content` a string or a list of text blocks.
- OpenAI responses: `input` as a string, or the last of its items with a `role`, its `content` a string or a list
  of `{"type": "input_text", "text": ...}` parts.

Everything the edits do not touch is left as it was: a body no edit applies to is
returned unchanged byte for byte, and an edited body is the same JSON value apart
from the edited strings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from minutehand.adapters.proxy.policy import ModelEdit
from minutehand.domain.experiment import CallMatch, ModelSwap, PromptPart, PromptPatch

_SYSTEM_ROLES = ("system", "developer")
_TEXT_BLOCKS = ("text",)
_INPUT_BLOCKS = ("input_text",)


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


def _text_slots(
    value: object, holder: dict[str, object], key: str, blocks: tuple[str, ...] = _TEXT_BLOCKS
) -> list[_Slot]:
    """A string field is one slot; a list of text blocks (of the given `type`s) is one slot per block."""
    if isinstance(value, str):
        return [_Slot(holder, key)]
    if isinstance(value, list):
        slots: list[_Slot] = []
        for block in value:
            if isinstance(block, dict) and _member(block, "type") in blocks and isinstance(_member(block, "text"), str):
                slots.append(_Slot(block, "text"))
        return slots
    return []


def _last_message_slots(body: dict[str, object]) -> list[_Slot]:
    """The newest message put to the model: the last of `messages` that is not the system's (chat completions,
    Anthropic), or the responses API's `input` as a string or its last item with a role."""
    messages = _member(body, "messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and _member(message, "role") not in _SYSTEM_ROLES and "content" in message:
                return _text_slots(message["content"], message, "content")
        return []
    if "input" in body:
        given = body["input"]
        if isinstance(given, str):
            return [_Slot(body, "input")]
        if isinstance(given, list):
            for item in reversed(given):
                if isinstance(item, dict) and "role" in item and "content" in item:
                    return _text_slots(item["content"], item, "content", _INPUT_BLOCKS)
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


def _matches(where: CallMatch, host: str, model: str | None, system: str | None, last: str | None) -> bool:
    if where.host is not None and where.host.lower() != host.lower():
        return False
    if where.model is not None and where.model != model:
        return False
    if where.system_contains is not None and (system is None or where.system_contains not in system):
        return False
    return not (where.last_message_contains is not None and (last is None or where.last_message_contains not in last))


def _patched(slots: list[_Slot], edit: PromptPatch) -> bool:
    """`edit` applied to `slots`: appended to the last, or found and replaced in each; whether anything changed."""
    if not slots:
        return False
    if edit.find is None:
        slots[-1].write(slots[-1].read() + edit.text)
        return True
    changed = False
    for slot in slots:
        before = slot.read()
        after = before.replace(edit.find, edit.text)
        if after != before:
            slot.write(after)
            changed = True
    return changed


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
    newest = _last_message_slots(parsed)
    last = "\n".join(slot.read() for slot in newest) if newest else None
    # Every edit is matched against the request as the agent sent it, so a swap of
    # the model cannot change which prompt patches apply after it.
    chosen = [e for e in edits if _matches(e.where, host, model, system, last)]
    changed = False
    for edit in chosen:
        if isinstance(edit, ModelSwap):
            if model != edit.to:
                parsed["model"] = edit.to
                changed = True
        elif isinstance(edit, PromptPatch):
            changed = _patched(newest if edit.part is PromptPart.LAST_MESSAGE else slots, edit) or changed
    if not changed:
        return None
    return json.dumps(parsed, ensure_ascii=False).encode()
