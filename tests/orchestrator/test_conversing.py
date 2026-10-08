"""A scripted person whose steps are used goes on conversing, a model writing from their facts, until the run ends
or the author declares them silent; each exchange is a fact a team's rule can count."""

from __future__ import annotations

from datetime import timedelta

from minutehand.application.replier import PERSON_PROMPT_VERSION, STEP_PROMPT_VERSION
from minutehand.checks.runner import evaluate_run
from minutehand.domain.assessments import Rule
from minutehand.domain.checks import FindingKind
from minutehand.domain.people import Writing
from minutehand.domain.scenario import AfterScript, Person, Scripted, ScriptedReply, Silent, Window
from minutehand.domain.world import Actor, MessageSnapshot, Operation, WorldEvent
from tests.orchestrator.rig import Rig, person, scenario
from tests.support.people import people_requests

DAY = Window(min=timedelta(days=1), max=timedelta(days=1))
STEPS = [
    ScriptedReply(to_ask=1, facts=["the partner price is 40k a year"]),
    ScriptedReply(to_ask=2, facts=["I can sign on Tuesday"]),
]


def sofia(then: AfterScript) -> Person:
    return Person(
        key="sofia",
        name="Sofia",
        email="sofia@example.com",
        facts=["The contract renews in March."],
        reply_within=DAY,
        reply=Scripted(replies=STEPS, then=then),
    )


def _said(events: list[WorldEvent]) -> list[str]:
    return [
        e.after.text
        for e in events
        if e.actor is Actor.PERSON and e.operation is Operation.CREATE and isinstance(e.after, MessageSnapshot)
    ]


async def test_a_used_up_script_goes_on_conversing_from_the_persons_facts_within_their_window(rig: Rig) -> None:
    scn = scenario(people=[person("owner", Silent()), sofia(AfterScript.ANSWERS), person("tom", Silent())])
    record, store, _ = await rig.run(scn, rig.agent("ask_and_file"))

    replies = store.replies()
    assert [r.writing for r in replies[:3]] == [Writing.SCRIPT, Writing.SCRIPT, Writing.CONVERSING]
    assert _said(store.events())[:3] == [
        "The partner price is 40k a year.",
        "I can sign on Tuesday.",
        "The contract renews in March.",
    ]
    third = replies[2]
    assert third.written_by is not None and third.written_by.prompt_version == PERSON_PROMPT_VERSION
    assert replies[0].written_by is not None and replies[0].written_by.prompt_version == STEP_PROMPT_VERSION
    assert (
        third.drawn is not None and third.drawn.window == DAY and third.at == third.drawn.asked_at + timedelta(days=1)
    )
    assert record.ended_at >= third.at
    calls = store.person_calls()
    assert len(calls) >= 3 and all(c.answer is not None and c.input_tokens for c in calls)

    # Each exchange is a fact a team's rule counts: how many the model wrote once the script was used.
    rule = Rule.model_validate(
        {"id": "keeps_talking", "count": {"replies": {"by": ["sofia"], "written": ["conversing"]}}, "at_most": 0}
    )
    result = evaluate_run(scn, store.events(), record.wakes, store.replies(), stop=record.stop, rules=[rule])
    [failed] = [f for f in result.findings if f.check == "keeps_talking"]
    assert failed.kind is FindingKind.FAIL


async def test_a_script_that_says_then_silent_stays_silent_once_used(rig: Rig) -> None:
    before = len(people_requests())
    scn = scenario(people=[person("owner", Silent()), sofia(AfterScript.SILENT), person("tom", Silent())])
    _, store, _ = await rig.run(scn, rig.agent("ask_and_file"))

    assert [r.writing for r in store.replies()] == [Writing.SCRIPT, Writing.SCRIPT]
    assert len(_said(store.events())) == 2
    asks = [
        e.after
        for e in store.events()
        if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot) and e.operation is Operation.CREATE
    ]
    to_her = [a for a in asks if isinstance(a, MessageSnapshot) and "sofia@example.com" in a.recipient_emails]
    assert len(to_her) == 3, "a third ask, unanswered"
    assert len(people_requests()) - before == 2, "only the two steps were written"
