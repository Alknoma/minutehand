"""A whole run of an agent that chases a person by email and then invites them to a call: the real proxy, the Google
Workspace provider, stock `googleapiclient` in the agent's own process, and the person answering by email after a
delay and accepting the invitation, each at their moment, where the agent finds them on its next poll.

The scenario's expectations and the scorecard read the run as they read a Slack one: the email is the agent asking
Sofia, her reply settles that wait and is what the agent relays to the owner, and the invitation is a second ask
her acceptance settles."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.providers.google_workspace.seed import WorkspaceSeed
from minutehand.checks import ledger
from minutehand.domain.agent import AgentUnderTest, Reported
from minutehand.domain.checks import FindingKind, ObligationKind
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import (
    DelayRange,
    Person,
    PersonAsked,
    ProviderSeed,
    Relayed,
    Scenario,
    Scripted,
    ScriptedPress,
    ScriptedReply,
    SignIn,
)
from minutehand.domain.world import Actor, InteractionSnapshot, MessageSnapshot
from tests.e2e.support import OWNER, SOFIA, T0, free_port, messages, world

pytestmark = pytest.mark.timeout(180)

AGENT = Path(__file__).parent / "agents" / "workspace_agent.py"
REFRESH = "1//assistant-refresh-token"
DELAY = timedelta(hours=3, minutes=10)
"""Not a whole number of the agent's half-hour polls, so her reply lands between two of them."""


def scenario() -> Scenario:
    sofia = [
        ScriptedReply(to_ask=1, text="Yes, 40k a year."),
        ScriptedReply(to_ask=2, press=ScriptedPress(label="Yes")),
    ]
    return Scenario(
        name="partner_pricing_by_mail",
        goal="The partner pricing is confirmed with Sofia, and she signs it off on a call.",
        owner="owner",
        starts_at=T0,
        deadline_after=timedelta(days=2),
        people=[
            Person(key="owner", name="Olive Owner", email=OWNER, reply=Scripted(replies=[])),
            Person(key="assistant", name="Ada Assistant", email="assistant@example.com", reply=Scripted(replies=[])),
            Person(
                key="sofia",
                name="Sofia Romano",
                email=SOFIA,
                reply=Scripted(delay=DelayRange(shortest=DELAY, longest=DELAY), replies=sofia),
            ),
        ],
        sign_ins=[SignIn(provider="google_workspace", credential=REFRESH, person="assistant")],
        provider_seeds=[ProviderSeed(provider="google_workspace", body=WorkspaceSeed().model_dump_json())],
        expect=[
            PersonAsked(person="sofia", mentions=["partner pricing"]),
            PersonAsked(person="sofia", at_least=2),
            Relayed(said_by="sofia", to="owner", tell="40k"),
        ],
    )


async def play(tmp_path: Path, scn: Scenario) -> session.Outcome:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    agent = AgentUnderTest(
        name="workspace_agent", wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report")]
    )
    command = [
        sys.executable,
        str(AGENT),
        "--port",
        str(port),
        "--state",
        str(tmp_path / "agent.json"),
        "--refresh",
        REFRESH,
    ]
    [outcome] = await session.play(scn, agent, state=tmp_path / "state", command=command)
    return outcome


async def test_an_emailed_question_answered_by_email_and_an_accepted_invitation_meet_every_expectation(
    tmp_path: Path,
) -> None:
    scn = scenario()
    outcome = await play(tmp_path, scn)

    result = outcome.result
    assert [f.message for f in result.findings if f.kind is FindingKind.FAIL] == []
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (3, 3)
    assert result.effectiveness.waits_opened == 2 and result.effectiveness.waits_open_at_end == 0
    assert result.effectiveness.follow_ups_late == 0
    assert outcome.record.stop is StopReason.AGENT_DONE

    store = world(tmp_path / "state", outcome.record.run_id)
    events = store.events()
    asked = messages(events, Actor.AGENT, to=SOFIA)
    email, invitation = asked
    assert isinstance(email.after, MessageSnapshot) and isinstance(invitation.after, MessageSnapshot)
    assert email.after.text.startswith("Partner pricing\n\nHi Sofia")
    assert [a.label for a in invitation.after.actions] == ["Yes", "Maybe", "No"]

    [answer] = messages(events, Actor.PERSON)
    assert isinstance(answer.after, MessageSnapshot)
    assert answer.after.text == "Re: Partner pricing\n\nYes, 40k a year."
    assert answer.after.thread_of == email.entity.external_id and answer.after.channel == email.after.channel
    assert answer.sim_time == email.sim_time + DELAY
    [accepted] = [e for e in events if isinstance(e.after, InteractionSnapshot)]
    assert isinstance(accepted.after, InteractionSnapshot)
    assert (accepted.after.person, accepted.after.action_id) == ("sofia", "accepted")
    assert accepted.sim_time == invitation.sim_time + DELAY

    assert answer.sim_time not in {w.sim_time for w in outcome.record.wakes}  # her reply woke nobody
    [relay] = messages(events, Actor.AGENT, to=OWNER)
    assert relay.sim_time == answer.sim_time + timedelta(minutes=20)  # the agent's next poll found it
    assert invitation.sim_time == relay.sim_time

    waits = ledger.build(scn, events, store.replies())
    answers = [w for w in waits if w.kind is ObligationKind.ANSWER_FROM_PERSON]
    assert [(w.person, w.opened_by, w.settled_at) for w in answers] == [
        ("sofia", email.seq, answer.sim_time),
        ("sofia", invitation.seq, accepted.sim_time),
    ]
