"""Adaptive Cards, read as the structured JSON they are: the text a person sees, the inputs they can fill, the
actions they can press. A card is stored whole, exactly as the bot sent it; nothing here changes it."""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import JsonValue

from minutehand.adapters.providers.microsoft import wire
from minutehand.domain.scenario import Model
from minutehand.domain.world import ControlKind, MessageAction


class ActionKind(StrEnum):
    EXECUTE = "Action.Execute"
    SUBMIT = "Action.Submit"
    OPEN_URL = "Action.OpenUrl"
    SHOW_CARD = "Action.ShowCard"
    TOGGLE_VISIBILITY = "Action.ToggleVisibility"


class CardAction(Model):
    """A button: what pressing it sends. `verb` is `Action.Execute`'s; `data` is what both kinds send back."""

    kind: ActionKind
    title: str | None = None
    id: str | None = None
    verb: str | None = None
    data: JsonValue = None
    url: str | None = None


class CardInput(Model):
    type: str
    id: str
    label: str | None = None
    value: JsonValue = None
    choices: list[str] = []


class Card(Model):
    attachment: int = 0
    text: list[str]
    inputs: list[CardInput]
    actions: list[CardAction]


def _walk(node: JsonValue, card: Card) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, card)
        return
    if not isinstance(node, dict):
        return
    kind = node["type"] if "type" in node and isinstance(node["type"], str) else None
    if (kind == "TextBlock" and "text" in node and isinstance(node["text"], str)) or (
        kind == "TextRun" and "text" in node and isinstance(node["text"], str)
    ):
        card.text.append(node["text"])
    elif kind is not None and kind.startswith("Input.") and "id" in node and isinstance(node["id"], str):
        choices = node["choices"] if "choices" in node and isinstance(node["choices"], list) else []
        card.inputs.append(
            CardInput(
                type=kind,
                id=node["id"],
                label=node["label"] if "label" in node and isinstance(node["label"], str) else None,
                value=node["value"] if "value" in node else None,
                choices=[
                    c["value"] for c in choices if isinstance(c, dict) and "value" in c and isinstance(c["value"], str)
                ],
            )
        )
    elif kind in {k.value for k in ActionKind}:
        assert kind is not None
        card.actions.append(
            CardAction(
                kind=ActionKind(kind),
                title=_text(node, "title"),
                id=_text(node, "id"),
                verb=_text(node, "verb"),
                data=node["data"] if "data" in node else None,
                url=_text(node, "url"),
            )
        )
    elif kind == "FactSet" and "facts" in node and isinstance(node["facts"], list):
        for fact in node["facts"]:
            if isinstance(fact, dict):
                card.text.append(f"{_text(fact, 'title') or ''} {_text(fact, 'value') or ''}".strip())
    for key, value in node.items():
        if key not in ("data",) and isinstance(value, (list, dict)):
            _walk(value, card)


def _text(node: dict[str, JsonValue], key: str) -> str | None:
    found = node[key] if key in node else None
    return found if isinstance(found, str) else None


def cards_of(activity: wire.Activity) -> list[Card]:
    """Every Adaptive Card on an activity, in attachment order."""
    found: list[Card] = []
    for position, attachment in enumerate(activity.attachments or []):
        if attachment.contentType != wire.ADAPTIVE_CARD:
            continue
        card = Card(attachment=position, text=[], inputs=[], actions=[])
        _walk(attachment.content, card)
        found.append(card)
    return found


def visible_text(activity: wire.Activity) -> str:
    """What a person reads: the message's text, then every card's text, inputs and buttons."""
    parts = [activity.text] if activity.text else []
    for card in cards_of(activity):
        parts += card.text
        parts += [i.label for i in card.inputs if i.label]
        parts += [a.title for a in card.actions if a.title]
    return "\n".join(parts)


def action_id(action: CardAction) -> str:
    """How the world names a card's button: its own `id`, else `Action.Execute`'s `verb`, else its title."""
    return action.id or action.verb or action.title or action.kind.value


def message_actions(activity: wire.Activity) -> list[MessageAction]:
    """Every button on the activity's cards, as the checks and a person read them."""
    found: list[MessageAction] = []
    for card in cards_of(activity):
        for action in card.actions:
            if action.kind is ActionKind.SHOW_CARD or action.kind is ActionKind.TOGGLE_VISIBILITY:
                continue
            found.append(
                MessageAction(
                    action_id=action_id(action),
                    label=action.title or action_id(action),
                    control=ControlKind.LINK if action.kind is ActionKind.OPEN_URL else ControlKind.BUTTON,
                    value=json.dumps(action.data) if action.data is not None else action.url,
                )
            )
    return found
