"""`minutehand migrate <file>`: a scenario written for the mechanisms the transitions design retired, rewritten in the
keys that replaced them (`docs/design-transitions.md`, phase 4). Everything a person is pinned to do on an item is a
take (`Person.takes`):

- `ticket_fates: [{assignee, becomes | deleted, after}]` -> the assignee's take: `take:` the state's meaning (`done`,
  `cancelled`, `open`) or `delete`, `after` as given. A fate covered every ticket handed to its assignee in any
  tracker, so the take names no `nth` and no provider; an item that does not offer it (a message) ignores it.
- A script step's `press: {label, picks, form}` -> a take of that label on the person's nth item (`nth: to_ask`, any
  provider), `picks` in its `fields`, `form` as given, the step's `within` kept; the step itself goes.
- `presses_every: {label, ...}` -> a take of that label on every item, which an item that does not offer it ignores.
- A script's `decisions: [{to_item, inbox, decision, inputs, facts, within}]` -> a take of that decision: `provider`
  the inbox (any when none), `nth` the item, `fields` the inputs, `facts` and `within` as given.

It works on the document as data: what it rewrites loses its comments, and anything it cannot carry over whole is
said in a note.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class MigrationRefused(ValueError):
    """The file cannot be rewritten without something only its author knows."""


@dataclass
class Migrated:
    document: dict[str, object]
    notes: list[str] = field(default_factory=list)
    changed: bool = False


def migrate(document: dict[str, object]) -> Migrated:
    """`document` (a scenario file as loaded) with every retired key rewritten."""
    done = Migrated(document=dict(document))
    people = _people(done.document)
    _fates(done, people)
    for person in people:
        _script(done, person)
    return done


def _people(document: dict[str, object]) -> list[dict[str, object]]:
    found = document["people"] if "people" in document else []
    if not isinstance(found, list):
        raise MigrationRefused("`people` is not a list")
    people: list[dict[str, object]] = []
    for person in found:
        if not isinstance(person, dict):
            raise MigrationRefused("a person is not a mapping")
        people.append(person)  # pyright: ignore[reportUnknownArgumentType]
    return people


def _takes(person: dict[str, object]) -> list[object]:
    if "takes" not in person:
        person["takes"] = []
    takes = person["takes"]
    if not isinstance(takes, list):
        raise MigrationRefused(f"{person['key']}: `takes` is not a list")
    return takes  # pyright: ignore[reportUnknownVariableType]


def _fates(done: Migrated, people: list[dict[str, object]]) -> None:
    if "ticket_fates" not in done.document:
        return
    fates = done.document.pop("ticket_fates")
    done.changed = True
    by_key = {str(p["key"]): p for p in people}
    for fate in fates if isinstance(fates, list) else []:
        if not isinstance(fate, dict):
            raise MigrationRefused("a ticket fate is not a mapping")
        assignee = str(fate["assignee"])
        if assignee not in by_key:
            raise MigrationRefused(f"a ticket fate names {assignee}, who is not among the people")
        deleted = "deleted" in fate and fate["deleted"] is True
        take = "delete" if deleted else str(fate["becomes"])
        _takes(by_key[assignee]).append({"take": take, "after": fate["after"]})
        done.notes.append(f"{assignee}: their ticket fate is a take of {take!r} on every ticket that waits on them")


def _script(done: Migrated, person: dict[str, object]) -> None:
    reply = person["reply"] if "reply" in person else None
    if not isinstance(reply, dict) or "kind" not in reply or reply["kind"] != "scripted":
        return
    key = str(person["key"])
    steps = reply["replies"] if "replies" in reply else None
    if isinstance(steps, list):
        kept: list[object] = []
        for step in steps:
            if isinstance(step, dict) and "press" in step:
                press = step["press"]
                take = _pressed(press, key)
                take["nth"] = step["to_ask"]
                if "within" in step:
                    take["within"] = step["within"]
                _takes(person).append(take)
                done.changed = True
                done.notes.append(f"{key}: the press on their ask {step['to_ask']} is a take of {take['take']!r}")
            else:
                kept.append(step)
        reply["replies"] = kept
    if "presses_every" in reply:
        _takes(person).append(_pressed(reply.pop("presses_every"), key))
        done.changed = True
        done.notes.append(f"{key}: `presses_every` is a take on every item, which one that does not offer it ignores")
    if "decisions" in reply:
        decisions = reply.pop("decisions")
        done.changed = True
        conversing = "then" not in reply or reply["then"] == "answers"
        if isinstance(decisions, list) and not decisions and conversing:
            done.notes.append(
                f"{key}: `decisions: []` left every item pending while they went on conversing; a model now decides "
                "their items: make them `then: silent`, or pin each decision with a take"
            )
        for decision in decisions if isinstance(decisions, list) else []:
            if not isinstance(decision, dict):
                raise MigrationRefused(f"{key}: a scripted decision is not a mapping")
            take: dict[str, object] = {"take": decision["decision"]}
            if "inbox" in decision and decision["inbox"] is not None:
                take["provider"] = decision["inbox"]
            if "to_item" in decision and decision["to_item"] is not None:
                take["nth"] = decision["to_item"]
            for kept_key, as_key in (("inputs", "fields"), ("facts", "facts"), ("within", "within")):
                if decision.get(kept_key):
                    take[as_key] = decision[kept_key]
            _takes(person).append(take)
            done.notes.append(f"{key}: their scripted decision {decision['decision']!r} is a take")


def _pressed(press: object, key: str) -> dict[str, object]:
    if not isinstance(press, dict) or "label" not in press:
        raise MigrationRefused(f"{key}: a press names no `label`")
    take: dict[str, object] = {"take": press["label"]}
    if "picks" in press and press["picks"] is not None:
        take["fields"] = {"picks": press["picks"]}
    if press.get("form"):
        take["form"] = press["form"]
    return take
