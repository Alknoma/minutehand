"""The pricing scenario the people tests play: sofia answers in 36 hours, tom finishes what is handed to him."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minutehand.domain.agent import Sandbox
from minutehand.domain.scenario import (
    AfterScript,
    DelayRange,
    Person,
    ReplyBehaviour,
    Scenario,
    Scripted,
    ScriptedReply,
    Silent,
    Take,
)

T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)  # a Monday

SANDBOX = Sandbox(
    deadlines=["runsc", "debug", "--deadlines", "agent"],
    advance=["runsc", "debug", "--advance-clock={nanoseconds}ns", "agent"],
)
"""The sandbox an agent file names, as the patched gVisor's control calls spell it (docs/design.md, "A sandbox whose
clock Minutehand owns"), for a test that reads an agent file and starts no sandbox."""


def person(key: str, reply: ReplyBehaviour) -> Person:
    return Person(key=key, name=key.title(), email=f"{key}@example.com", reply=reply)


def scripted(*texts: str, hours: float) -> Scripted:
    delay = timedelta(hours=hours)
    return Scripted(
        then=AfterScript.SILENT,
        delay=DelayRange(shortest=delay, longest=delay),
        replies=[ScriptedReply(to_ask=n + 1, verbatim=t) for n, t in enumerate(texts)],
    )


def scenario(*, tom_finishes: bool = True, **overrides: object) -> Scenario:
    """The pricing scenario: sofia answers in 36 hours, and (`tom_finishes`) tom finishes every ticket handed to him
    three days after it is."""
    tom = person("tom", Silent())
    if tom_finishes:
        tom = tom.model_copy(update={"takes": [Take(take="done", after=timedelta(days=3))]})
    fields: dict[str, object] = {
        "name": "pricing",
        "goal": "Pricing is confirmed and the legal review is done.",
        "owner": "owner",
        "starts_at": T0,
        "deadline_after": timedelta(days=14),
        "people": [
            person("owner", Silent()),
            person("sofia", scripted("Yes, 40k.", hours=36)),
            tom,
            person("dania", Silent()),
        ],
    }
    fields.update(overrides)
    return Scenario.model_validate(fields)
