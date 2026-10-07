"""A send through a captured host read as a message to a person: seen by the expectations, the scorecard and
the team's rules as a chat message is, and never taken for an ask, since nobody can answer where it went."""

from __future__ import annotations

from datetime import timedelta

from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.run_clock import RunClock
from minutehand.checks.effectiveness import measure
from minutehand.checks.expectations import Expectations
from minutehand.domain.checks import FindingKind, ObligationKind
from minutehand.domain.scenario import DelayRange, PersonAsked, Scripted, ScriptedReply, Silent
from tests.checks.world import START, Log, person, scenario, view

OWNER = person("owner", Silent())
SOFIA = person("sofia")


def test_an_email_to_a_silent_person_opens_no_wait() -> None:
    """Silent means every message is a question left unanswered, where an answer could come: not by email."""
    log = Log()
    log.email([OWNER], 1)
    assert [o.kind for o in view(scenario(OWNER), log).obligations] == []


def test_an_email_to_someone_with_an_open_wait_is_a_follow_up_on_it() -> None:
    log = Log()
    asked = log.message([OWNER], 1)
    emailed = log.email([OWNER], 30)
    [wait] = view(scenario(OWNER), log).obligations
    assert (wait.kind, wait.opened_by, wait.agent_touches) == (
        ObligationKind.ANSWER_FROM_PERSON,
        asked.seq,
        [emailed.seq],
    )


def test_a_person_asked_expectation_is_met_by_an_email_and_the_scorecard_counts_it() -> None:
    log = Log()
    emailed = log.email([OWNER], 2, text="Weekly report: the contract is with legal.")
    world = scenario(OWNER, expect=[PersonAsked(person="owner", mentions=["weekly report"])])
    report = Expectations().run(view(world, log))
    [met] = report.findings
    assert met.kind is FindingKind.INFORMATIONAL and met.evidence == [emailed.seq]
    assert "the message to Owner" in met.message
    card = measure(view(world, log), report.findings, 1, START + timedelta(days=1))
    assert (card.messages_to_people, card.waits_opened) == (1, 0)


async def test_an_email_does_not_move_a_scripted_persons_script_on() -> None:
    """Sofia's script answers her first ask. An email she got first is not that ask: her chat message is."""
    sofia = person(
        "sofia",
        Scripted(
            delay=DelayRange(shortest=timedelta(hours=1), longest=timedelta(hours=1)),
            replies=[ScriptedReply(to_ask=1, text="Signed.")],
        ),
    )
    log = Log()
    log.email([sofia], 1)
    asked = log.message([sofia], 2)
    reply = await ScriptedReplier(scenario(sofia)).decide(sofia, asked, log.events, RunClock(START))
    assert reply is not None and reply.text == "Signed."
