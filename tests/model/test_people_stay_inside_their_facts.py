"""A person a model writes for stays inside what the scenario says they know: no permission, decision or fact beyond
it. Every reply a model writes is checked before it is sent; one that goes beyond is written again once, and one that
still does is a fact of the simulation's health, not something the agent is judged on.

The case is the trial's: Sam, who knew only the cost centre, told the agent "You're good to proceed with that; no need
to wait on Owen", and the agent ordered while the approval was pending."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from examples.recipes import fake_model
from minutehand.adapters.model.openai_compatible import OpenAICompatible
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.replier import FACT_CHECK_PROMPT_VERSION, STEP_PROMPT_VERSION, PeopleReplier
from minutehand.application.run_clock import RunClock
from minutehand.checks.health import health
from minutehand.checks.runner import evaluate
from minutehand.domain.checks import HealthKind, RunView
from minutehand.domain.conversation import FactCheck, Wrote
from minutehand.domain.scenario import (
    AfterScript,
    Person,
    Scenario,
    Scripted,
    ScriptedReply,
    Silent,
    Window,
    WorkingHours,
)
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation
from tests.model.fake_completions import FakeCompletions, Received, Rule, fake_completions
from tests.orchestrator.rig import T0, person, scenario

SAID_IN_THE_TRIAL = (
    "Hi,\n\nFor new-starter equipment like this, use cost centre **CC-4410** for the PO-7731 order. That covers the 40 "
    "laptop units.\n\nYou're good to proceed with that; no need to wait on Owen for confirmation on this one.\n\nSam"
)
WITHIN = "Use cost centre CC-4410 for the PO-7731 order.\n\nSam"
ASK = "Hi Sam, what cost centre should I use for the 40 new-starter laptops (PO-7731)?"

SAM = Person(
    key="sam",
    name="Sam Okafor",
    email="sam@example.com",
    facts=["The cost centre for new-starter equipment is CC-4410."],
    reply_within=Window(min=timedelta(hours=2), max=timedelta(hours=5)),
    working_hours=WorkingHours(timezone="Europe/London"),
    reply=Scripted(
        voice="brief, busy finance person",
        replies=[ScriptedReply(to_ask=1, facts=["cost centre CC-4410"])],
        then=AfterScript.ANSWERS,
    ),
)


def _played() -> Scenario:
    return scenario(tom_finishes=False, people=[person("owner", Silent()), SAM])


def _model(*, learns: bool) -> Rule:
    """Sam's words as the trial's model wrote them, and once told why they cannot be sent, within his facts only when
    he `learns`; the check reads them by the recipes' stand-in's rule."""

    def rule(received: Received) -> str:
        if received.schema_name == "FactCheck":
            return json.dumps(fake_model.fact_check_answer(received.last))
        told = "Before you send it" in received.last
        return json.dumps({"text": WITHIN if told and learns else SAID_IN_THE_TRIAL})

    return rule


async def _reply(tmp_path: Path, *, learns: bool) -> tuple[str, SqliteStore, FakeCompletions]:
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "r", clock)
    played = _played()
    async with fake_completions(_model(learns=learns), checks_facts=True) as fake:
        model = OpenAICompatible(base_url=fake.base_url, api_key="sk-facts", model_id="people-1")
        asked = store.apply(
            Change(
                entity=EntityRef(provider="testchat", kind=EntityKind.MESSAGE, external_id="ask"),
                operation=Operation.CREATE,
                actor=Actor.AGENT,
                body="{}",
                after=MessageSnapshot(text=ASK, channel="dm-sam", recipient_emails=[SAM.email]),
            )
        )
        reply = await PeopleReplier(played, model).decide(SAM, asked, store.events(), clock, store)
    assert reply is not None
    return reply.text, store, fake


async def test_a_go_ahead_sam_had_no_fact_for_is_written_again_without_it(tmp_path: Path) -> None:
    said, store, fake = await _reply(tmp_path, learns=True)

    assert said == WITHIN
    calls = store.person_calls()
    assert [c.wrote for c in calls] == [Wrote.REPLY, Wrote.FACT_CHECK, Wrote.REPLY, Wrote.FACT_CHECK]
    first, last = (FactCheck.model_validate_json(c.answer or "") for c in (calls[1], calls[3]))
    assert not first.supported and "good to proceed" in first.unsupported[0] and last.supported
    assert calls[1].prompt_version == FACT_CHECK_PROMPT_VERSION and calls[0].prompt_version == STEP_PROMPT_VERSION
    # The prompt itself says a go-ahead is a decision: what the trial's model did with the old prompt.
    assert "is a decision, and yours to make only when" in fake.for_schema("WrittenStep")[0].system
    view = RunView(scenario=_played(), events=store.events(), wakes=[], person_calls=calls)
    assert [f for f in health(view) if f.kind is HealthKind.BEYOND_FACTS] == []
    store.close()


async def test_a_reply_still_beyond_sams_facts_is_the_simulations_fault_not_the_agents(tmp_path: Path) -> None:
    said, store, _ = await _reply(tmp_path, learns=False)

    assert said == SAID_IN_THE_TRIAL, "written again once, then sent as written: the record says what was said"
    view = RunView(scenario=_played(), events=store.events(), wakes=[], person_calls=store.person_calls())
    [overstepped] = [f for f in health(view) if f.kind is HealthKind.BEYOND_FACTS]
    assert overstepped.incomplete and overstepped.person == "sam" and "good to proceed" in overstepped.words
    result = evaluate(view, stop=None)
    assert result.verdict.kind.value == "simulation_incomplete"
    # Mutation: with the check gone (every reply supported), nothing would say Sam went beyond his facts.
    store.close()
