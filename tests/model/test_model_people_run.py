"""A whole run with a person whose replies a model writes: a real agent process, the real proxy, the Slack
provider, the real store and clock, and a completions server on this machine standing in for the model."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.model.openai_compatible import (
    API_KEY_VARIABLE,
    BASE_URL_VARIABLE,
    MODEL_VARIABLE,
    OpenAICompatible,
)
from minutehand.application.refusals import RunRefused
from minutehand.application.replier import PERSON_PROMPT_VERSION, WrittenReply
from minutehand.checks.judged.asked_about import AskedAboutVerdict
from minutehand.checks.ledger import build
from minutehand.domain.checks import FindingKind, ObligationKind
from minutehand.domain.conversation import Provenance
from minutehand.domain.experiment import Fork
from minutehand.domain.outbound import Acknowledge, HtmlAt, MessageReading
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Answers, DelayRange, PersonAsked, Scenario, Scripted, ScriptedReply
from minutehand.domain.world import Actor
from tests.e2e.support import OWNER, QUESTION, SOFIA, T0, THANKS, agent_under_test, messages, scenario, texts, world
from tests.model.fake_completions import FakeCompletions, Received, fake_completions

KEY = "sk-whole-run-key-must-not-leak-7f3a9"
WRITTEN = "Yes: 40k a year, renewing in March."
SOFIA_WRITES = Answers(delay=DelayRange(shortest=timedelta(hours=36), longest=timedelta(hours=36)))


def the_model(received: Received) -> WrittenReply | AskedAboutVerdict:
    """Sofia answers a question and nothing else; the judge says her question was about pricing."""
    if received.schema_name == "AskedAboutVerdict":
        message = received.last.split("Message:", 1)[1]
        return AskedAboutVerdict(asks_about="pricing" in message, rationale="It asks her to confirm the price.")
    if received.last.rstrip().endswith("?"):
        return WrittenReply(replies=True, text=WRITTEN)
    return WrittenReply(replies=False, text=None)


def model(fake: FakeCompletions) -> OpenAICompatible:
    return OpenAICompatible(base_url=fake.base_url, api_key=KEY, model_id="people-1")


def with_sofia_writing() -> Scenario:
    scn = scenario(SOFIA_WRITES)
    sofia = next(p for p in scn.people if p.key == "sofia")
    sofia = sofia.model_copy(update={"facts": ["The partner price is 40k a year, renewing in March."]})
    return scn.model_copy(
        update={
            "people": [sofia if p.key == "sofia" else p for p in scn.people],
            "expect": [*scn.expect, PersonAsked(person="sofia", about="the partner pricing")],
        }
    )


async def test_a_model_written_answer_is_delivered_when_a_scripted_one_would_be_and_a_thank_you_gets_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    state = tmp_path / "state"
    scn = with_sofia_writing()

    async with fake_completions(the_model) as fake:
        [outcome] = await session.play(
            scn, launched.agent, state=state, command=launched.command, model=model(fake), judge=True
        )

    record, result = outcome.record, outcome.result
    assert record.stop is StopReason.AGENT_DONE
    # The same two moments as the scripted run of this scenario: START, and the answer 36 hours later.
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=36)]
    store = world(state, record.run_id)
    events = store.events()
    assert texts(messages(events, Actor.AGENT, to=SOFIA)) == [QUESTION, THANKS]
    said = messages(events, Actor.PERSON)
    assert texts(said) == [WRITTEN] and [m.sim_time for m in said] == [T0 + timedelta(hours=36)]
    assert launched.state(state, record.run_id)["verified"] == [WRITTEN]
    [reply] = store.replies()
    assert reply.written_by == Provenance(model="people-1", prompt_version=PERSON_PROMPT_VERSION)

    # She was asked twice: the question, and the thank-you, which the model said needs no answer.
    asked = fake.for_schema("WrittenReply")
    assert [r.asked for r in asked] == [QUESTION, THANKS]
    # Her second prompt holds the first exchange: her own answer is part of what she is told she said.
    shown = asked[1].last
    assert shown.index(f"They: {QUESTION}") < shown.index(f"You: {WRITTEN}") < shown.index(f"They: {THANKS}")
    thanks = messages(events, Actor.AGENT, to=SOFIA)[1]
    waits = build(scn, events, store.replies())
    assert [o.opened_by for o in waits if o.kind is ObligationKind.ANSWER_FROM_PERSON and o.person == "sofia"] == [
        messages(events, Actor.AGENT, to=SOFIA)[0].seq
    ]
    assert thanks.seq not in [o.opened_by for o in waits]

    # The judged expectation was judged, met, and counted.
    judged = fake.for_schema("AskedAboutVerdict")
    assert [QUESTION in j.last for j in judged] == [True, False]
    assert all("Topic: the partner pricing" in j.last for j in judged)
    assert [f for f in result.findings if f.kind is not FindingKind.INFORMATIONAL] == []
    assert result.blocked == []
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (3, 3)
    assert "asked_about: sofia asked about 'the partner pricing': 1 of 2 message(s) judged to ask" in result.notes

    # The key went to the model service and nowhere else.
    assert all(r.authorization == f"Bearer {KEY}" for r in fake.received)
    for path in state.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
    out, err = capsys.readouterr()
    assert KEY not in out + err + caplog.text


async def test_a_fork_replays_an_answer_written_before_it_and_asks_the_model_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    state = tmp_path / "state"
    scn = with_sofia_writing()

    async with fake_completions(the_model) as fake:
        [parent] = await session.play(scn, launched.agent, state=state, command=launched.command, model=model(fake))
        assert [r.asked for r in fake.received] == [QUESTION, THANKS]
        before_the_fork = len(fake.received)
        after_start = next(p for p in session.fork_points(state, parent.record.run_id) if p.wake == 1)
        [child] = await session.fork(
            parent.record.run_id,
            Fork(parent_run=parent.record.run_id, at_seq=after_start.seq),
            state=state,
            command=launched.command,
            model=model(fake),
        )
        # The answer to QUESTION was written before the fork and is kept; the agent's thank-you after the fork is
        # put to Sofia in exactly the context her parent's was, so the model's answer is replayed from the world.
        assert fake.received[before_the_fork:] == []

    assert child.record.stop is StopReason.AGENT_DONE
    events = world(state, child.record.run_id, root=parent.record.run_id).events()
    assert texts(messages(events, Actor.PERSON)) == [WRITTEN]
    assert [m.sim_time for m in messages(events, Actor.PERSON)] == [T0 + timedelta(hours=36)]
    assert texts(messages(events, Actor.AGENT, to=OWNER))
    [replayed] = world(state, child.record.run_id, root=parent.record.run_id).person_calls()
    assert replayed.replayed and replayed.asked == messages(events, Actor.AGENT, to=SOFIA)[1].entity


async def test_a_scenario_with_a_written_person_and_no_model_is_refused_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    state = tmp_path / "state"

    with pytest.raises(RunRefused) as raised:
        await session.play(with_sofia_writing(), launched.agent, state=state, command=launched.command)

    message = str(raised.value)
    assert "sofia" in message and all(v in message for v in (MODEL_VARIABLE, API_KEY_VARIABLE, BASE_URL_VARIABLE))
    assert not state.exists()


async def test_a_script_that_can_run_out_with_no_model_is_refused_before_it_starts_naming_the_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    state = tmp_path / "state"
    goes_on = Scripted(replies=[ScriptedReply(to_ask=1, verbatim="Yes, 40k.")])

    with pytest.raises(RunRefused) as raised:
        await session.play(scenario(goes_on), launched.agent, state=state, command=launched.command)

    message = str(raised.value)
    assert "sofia (they go on conversing once their script is used: `then: answers`, the default)" in message
    assert MODEL_VARIABLE in message and not state.exists()


async def test_a_question_emailed_to_a_person_whose_replies_a_model_writes_is_never_put_to_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sofia could answer a question in Slack, and the model would write her answer; an email through a captured
    host is no place she answers, so the run never asks the model whether she would."""
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    emailed = "Could you also confirm the renewal date by email?"
    monkeypatch.setenv("MAIL_URL", "https://api.mail.test/v3/mail/send")
    monkeypatch.setenv("MAIL_TO", SOFIA)
    monkeypatch.setenv("MAIL_TEXT", emailed)
    reading = MessageReading(recipients=["personalizations[*].to[*].email"], text=[HtmlAt(html="content[0].value")])
    agent = launched.agent.model_copy(
        update={"outbound": [Acknowledge(host="api.mail.test", name="mail", message=reading)]}
    )
    async with fake_completions(the_model) as fake:
        [outcome] = await session.play(
            with_sofia_writing(), agent, state=tmp_path / "state", command=launched.command, model=model(fake)
        )
    assert outcome.record.stop is StopReason.AGENT_DONE
    assert [r.asked for r in fake.for_schema("WrittenReply")] == [QUESTION, THANKS]
    events = world(tmp_path / "state", outcome.record.run_id).events()
    assert emailed in texts(messages(events, Actor.AGENT, to=SOFIA))
