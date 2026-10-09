"""A whole run through the run loop: the owner hands an agent its goal by DM, the agent posts a card to Nadia through
the proxy, Nadia presses Accept a scripted hour later, the agent replaces the card through its `response_url`, and
Nadia writes in the channel the next morning. The world log reads as it happened, each step stamped with the
simulated moment and the person who did it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from starlette.responses import Response

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.orchestrator import Reach, Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentUnderTest, GoalByMessage
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import (
    AfterScript,
    DelayRange,
    Person,
    PersonPosts,
    Scenario,
    Scripted,
    Silent,
    Take,
)
from minutehand.domain.world import Actor, InteractionSnapshot, MessageSnapshot, Operation
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, Intercepted, Received, data
from tests.providers.slack.test_slack_interactions import CARD

START = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)

SCENARIO = Scenario(
    name="contract_approval",
    goal="Get Nadia's approval to send the contract.",
    owner="owen",
    starts_at=START,
    deadline_after=timedelta(days=2),
    people=[
        Person(key="owen", name="Owen Hale", email="owen@example.com", reply=Silent()),
        Person(
            key="nadia",
            name="Nadia Rahman",
            email="nadia@example.com",
            reply=Scripted(
                then=AfterScript.SILENT,
                delay=DelayRange(shortest=timedelta(hours=1, minutes=4), longest=timedelta(hours=1, minutes=4)),
            ),
            takes=[Take(nth=1, take="Accept")],
        ),
    ],
    happenings=[PersonPosts(provider="slack", person="nadia", text="Sent it myself, thanks", after=timedelta(days=1))],
)


async def played(agent: AgentEndpoint, tmp_path: Path, *, card_by_edit: bool) -> tuple[SqliteStore, StopReason]:
    """One run of the scenario. With `card_by_edit` the agent posts its question as plain text and adds the buttons
    by `chat.update` in the same breath, as a caller does when a "Thinking…" message becomes the question."""
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    provider = build()
    proxy = Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca")
    async with proxy:
        import ssl

        slack = Intercepted(proxy=proxy, trust=ssl.create_default_context(cafile=str(proxy.ca_bundle)))

        async def the_agent(got: Received) -> Response:
            if "payload" in got.body.decode():
                async with slack.http() as http:
                    await http.post(got.payload["response_url"], json={"replace_original": True, "text": "Approved."})
                return Response(status_code=200)
            event = got.json["event"]
            if event["user"] == state.user_id("owen"):
                sdk = slack.asynchronous()
                dm = data(await sdk.conversations_open(users=[state.user_id("nadia")]))["channel"]["id"]
                if card_by_edit:
                    posted = data(await sdk.chat_postMessage(channel=dm, text="May I send the contract?"))
                    await sdk.chat_update(channel=dm, ts=posted["ts"], text="May I send the contract?", blocks=CARD)
                else:
                    await sdk.chat_postMessage(channel=dm, text="May I send the contract?", blocks=CARD)
            return Response(status_code=200)

        agent.answer = the_agent
        record = await run_scenario(
            scenario=SCENARIO,
            agent=AgentUnderTest(name="carder", goal=GoalByMessage(provider="slack"), inbound=[agent.target()]),
            reach=Reach(main=None),
            store=store,
            clock=clock,
            services=Services(providers=[provider], pushes={"slack": provider}),
            replier=PeopleReplier(SCENARIO, None),
            mounts=proxy,
            signing={"slack": SECRET},
        )
    return store, record.stop


async def test_a_scripted_press_plays_through_the_run_loop_and_the_log_says_who_approved_when(
    agent: AgentEndpoint, tmp_path: Path
) -> None:
    store, stop = await played(agent, tmp_path, card_by_edit=False)

    assert stop is StopReason.NOTHING_PENDING
    assert agent.forged == []
    story = [
        (e.sim_time, e.actor, e.operation, e.after)
        for e in store.events()
        if e.actor is not Actor.SCENARIO and isinstance(e.after, MessageSnapshot | InteractionSnapshot)
    ]
    pressed_at = START + timedelta(hours=1, minutes=4)
    assert [(t, a, o, type(s).__name__) for t, a, o, s in story] == [
        (START, Actor.PERSON, Operation.CREATE, "MessageSnapshot"),
        (START, Actor.AGENT, Operation.CREATE, "MessageSnapshot"),
        (pressed_at, Actor.PERSON, Operation.CREATE, "InteractionSnapshot"),
        (pressed_at, Actor.AGENT, Operation.UPDATE, "MessageSnapshot"),
        (START + timedelta(days=1), Actor.PERSON, Operation.CREATE, "MessageSnapshot"),
    ]
    press = story[2][3]
    assert isinstance(press, InteractionSnapshot)
    assert (press.person, press.label, press.action_id, press.value) == ("nadia", "Accept", "approve_op1", "op1")
    replies = store.replies()
    assert len(replies) == 1 and replies[0].press is not None and replies[0].at == pressed_at
    assert len(agent.received) == 3, "the goal, the press, and Nadia's message the next day"


async def test_buttons_added_by_an_edit_are_put_to_the_person_though_the_text_stayed_the_same(
    agent: AgentEndpoint, tmp_path: Path
) -> None:
    store, _ = await played(agent, tmp_path, card_by_edit=True)
    pressed = [e.after for e in store.events() if isinstance(e.after, InteractionSnapshot)]
    assert [(p.person, p.label) for p in pressed] == [("nadia", "Accept")]
