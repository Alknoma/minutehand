"""A person uses a card the agent posted: the press reaches the agent signed and shaped as Slack sends it, the agent
answers through the proxy with stock clients (a modal by `views.open`, the card replaced by its `response_url`), and
the world log holds each step, the person's press as theirs."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from slack_sdk.errors import SlackApiError
from starlette.responses import JSONResponse, Response

from minutehand.adapters.providers.slack import interactive, state
from minutehand.adapters.providers.slack.interactive import FormNeverOpened
from minutehand.application.replier import PeopleReplier
from minutehand.domain.people import PersonReply, Press
from minutehand.domain.scenario import (
    AfterScript,
    DelayRange,
    FormInput,
    Person,
    Scripted,
    ScriptedPress,
    ScriptedReply,
)
from minutehand.domain.world import (
    Actor,
    InteractionKind,
    InteractionSnapshot,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    WorldEvent,
)
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, Intercepted, Received, data
from tests.providers.slack.slack_workspace import SCENARIO, Workspace
from tests.support.people import people_model

CARD = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Send the contract to Acme?*"}},
    {
        "type": "actions",
        "block_id": "consent_actions",
        "elements": [
            {
                "type": "button",
                "action_id": "approve_op1",
                "text": {"type": "plain_text", "text": "Accept"},
                "value": "op1",
                "style": "primary",
            },
            {
                "type": "button",
                "action_id": "reject_op1",
                "text": {"type": "plain_text", "text": "Reject"},
                "value": "op1",
                "style": "danger",
            },
        ],
    },
    {
        "type": "actions",
        "elements": [
            {
                "type": "users_select",
                "action_id": "assign_op1",
                "placeholder": {"type": "plain_text", "text": "Assign to"},
            },
        ],
    },
]

REJECT_MODAL = {
    "type": "modal",
    "callback_id": "reject_reason_op1",
    "title": {"type": "plain_text", "text": "Reject"},
    "submit": {"type": "plain_text", "text": "Reject"},
    "private_metadata": json.dumps({"operation_id": "op1"}),
    "blocks": [
        {
            "type": "input",
            "block_id": "rejection_reason_block",
            "label": {"type": "plain_text", "text": "Why?"},
            "element": {"type": "plain_text_input", "action_id": "rejection_reason_input", "multiline": True},
        }
    ],
}


def tomas(press: ScriptedPress) -> Person:
    return Person(
        key="tomas",
        name="Tomas Brandt",
        email="tomas@example.com",
        reply=Scripted(
            then=AfterScript.SILENT,
            delay=DelayRange(shortest=timedelta(0), longest=timedelta(0)),
            replies=[ScriptedReply(to_ask=1, press=press)],
        ),
    )


async def card_to_tomas(slack: Intercepted, workspace: Workspace) -> WorldEvent:
    sdk = slack.asynchronous()
    dm = data(await sdk.conversations_open(users=[state.user_id("tomas")]))["channel"]["id"]
    posted = data(await sdk.chat_postMessage(channel=dm, text="Approval Required", blocks=CARD))
    [event] = [e for e in workspace.store.events() if e.entity == state.message_ref(posted["ts"])]
    return event


async def decided(person: Person, asked: WorldEvent, workspace: Workspace) -> PersonReply:
    scenario = SCENARIO.model_copy(update={"people": [p for p in SCENARIO.people if p.key != "tomas"] + [person]})
    reply = await PeopleReplier(scenario, people_model()).decide(
        person, asked, workspace.store.events(), workspace.clock
    )
    assert reply is not None and reply.press is not None
    return reply


def interactions(workspace: Workspace) -> list[InteractionSnapshot]:
    return [e.after for e in workspace.store.events() if isinstance(e.after, InteractionSnapshot)]


async def test_accept_reaches_the_agent_signed_and_the_card_is_replaced_by_its_response_url(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    async def answer(got: Received) -> Response:
        payload = got.payload
        async with slack.http() as http:
            replaced = await http.post(
                payload["response_url"],
                json={"response_type": "ephemeral", "replace_original": True, "text": "Accepted by Tomas"},
            )
        assert replaced.status_code == 200 and replaced.json() == {"ok": True}
        return Response(status_code=200)

    agent.answer = answer
    asked = await card_to_tomas(slack, workspace)
    reply = await decided(tomas(ScriptedPress(label="accept")), asked, workspace)
    await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)

    assert agent.forged == [] and len(agent.received) == 1
    got = agent.received[0]
    assert got.headers["content-type"] == "application/x-www-form-urlencoded"
    payload = got.payload
    assert payload["type"] == "block_actions"
    assert payload["user"]["id"] == state.user_id("tomas") and payload["team"]["id"] == state.TEAM_ID
    action = payload["actions"][0]
    assert (action["action_id"], action["value"], action["block_id"], action["type"]) == (
        "approve_op1",
        "op1",
        "consent_actions",
        "button",
    )
    assert payload["container"]["channel_id"] == payload["channel"]["id"] == workspace.dm("tomas")
    assert payload["container"]["message_ts"] == payload["message"]["ts"] == asked.entity.external_id
    assert [b["type"] for b in payload["message"]["blocks"]] == ["section", "actions", "actions"]
    assert payload["trigger_id"] and payload["response_url"].startswith("https://hooks.slack.com/actions/")

    history = data(await slack.asynchronous().conversations_history(channel=workspace.dm("tomas")))["messages"]
    assert [m["text"] for m in history] == ["Accepted by Tomas"] and "blocks" not in history[0]
    [pressed] = interactions(workspace)
    assert (pressed.interaction, pressed.person, pressed.action_id, pressed.value, pressed.label) == (
        InteractionKind.PRESS,
        "tomas",
        "approve_op1",
        "op1",
        "Accept",
    )
    log = [e for e in workspace.store.events() if e.seq > asked.seq and e.actor is not Actor.SCENARIO]
    steps = [(e.actor, e.operation, type(e.after).__name__) for e in log if e.after is not None]
    assert steps == [
        (Actor.PERSON, Operation.CREATE, "InteractionSnapshot"),
        (Actor.AGENT, Operation.UPDATE, "MessageSnapshot"),
    ]


async def test_reject_with_a_reason_fills_and_submits_the_modal_the_agent_opens(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    async def answer(got: Received) -> Response:
        payload = got.payload
        if payload["type"] == "block_actions":
            await slack.asynchronous().views_open(trigger_id=payload["trigger_id"], view=REJECT_MODAL)
        return Response(status_code=200)

    agent.answer = answer
    asked = await card_to_tomas(slack, workspace)
    reason = "The price went up; hold it."
    reply = await decided(tomas(ScriptedPress(label="Reject", form=[FormInput(value=reason)])), asked, workspace)
    await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)

    assert reply.text == reason
    assert [r.payload["type"] for r in agent.received] == ["block_actions", "view_submission"]
    submitted = agent.received[1].payload
    assert submitted["view"]["callback_id"] == "reject_reason_op1"
    assert json.loads(submitted["view"]["private_metadata"]) == {"operation_id": "op1"}
    assert submitted["view"]["state"]["values"] == {
        "rejection_reason_block": {"rejection_reason_input": {"type": "plain_text_input", "value": reason}}
    }
    assert submitted["user"]["id"] == state.user_id("tomas") and submitted["team"]["id"] == state.TEAM_ID
    assert submitted["trigger_id"] != agent.received[0].payload["trigger_id"]
    press, submission = interactions(workspace)
    assert (submission.interaction, submission.action_id, submission.form) == (
        InteractionKind.SUBMIT,
        "reject_reason_op1",
        [reason],
    )
    assert press.action_id == "reject_op1"
    view_events = [
        e for e in workspace.store.events() if isinstance(e.after, RecordSnapshot) and e.after.resource == "views"
    ]
    assert [e.operation for e in view_events] == [Operation.CREATE, Operation.UPDATE], "opened, then closed"
    with pytest.raises(SlackApiError) as used:
        await slack.asynchronous().views_open(trigger_id=agent.received[0].payload["trigger_id"], view=REJECT_MODAL)
    assert used.value.response["error"] == "exchanged_trigger_id"


async def test_a_submission_answered_with_errors_leaves_the_modal_open_with_them(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    async def answer(got: Received) -> Response:
        if got.payload["type"] == "block_actions":
            await slack.asynchronous().views_open(trigger_id=got.payload["trigger_id"], view=REJECT_MODAL)
            return Response(status_code=200)
        return JSONResponse({"response_action": "errors", "errors": {"rejection_reason_block": "Say more."}})

    agent.answer = answer
    asked = await card_to_tomas(slack, workspace)
    reply = await decided(
        tomas(ScriptedPress(label="Reject", form=[FormInput(input_id="rejection_reason_input", value="no")])),
        asked,
        workspace,
    )
    await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)

    from minutehand.adapters.providers.slack import wire

    [view] = workspace.slack.bodies(state.view_ref("x").kind, state.VIEWS, wire.OpenView)
    assert view.open is True and view.errors == {"rejection_reason_block": "Say more."}


async def test_a_submission_answered_with_an_update_shows_the_next_view(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    thanks = {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Done"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Rejected. I will tell the requester."}}],
    }

    async def answer(got: Received) -> Response:
        if got.payload["type"] == "block_actions":
            await slack.asynchronous().views_open(trigger_id=got.payload["trigger_id"], view=REJECT_MODAL)
            return Response(status_code=200)
        return JSONResponse({"response_action": "update", "view": thanks})

    agent.answer = answer
    asked = await card_to_tomas(slack, workspace)
    reply = await decided(tomas(ScriptedPress(label="Reject", form=[FormInput(value="no")])), asked, workspace)
    await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)

    last = [e for e in workspace.store.events() if isinstance(e.after, RecordSnapshot) and e.after.resource == "views"][
        -1
    ]
    assert isinstance(last.after, RecordSnapshot) and "I will tell the requester" in last.after.text


async def test_a_person_picker_sends_who_was_picked(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    asked = await card_to_tomas(slack, workspace)
    reply = await decided(tomas(ScriptedPress(label="Assign to", picks="noor")), asked, workspace)
    await workspace.provider.press(
        reply, agent.target(interactivity=agent.url), workspace.store, workspace.clock, secret=SECRET
    )

    [action] = agent.received[0].payload["actions"]
    assert action["type"] == "users_select" and action["selected_user"] == state.user_id("noor")
    assert interactions(workspace)[0].value == state.user_id("noor")


async def test_a_press_meant_for_a_form_the_agent_never_opens_fails_the_agent(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(interactive, "FORM_WAIT", 0.3)
    asked = await card_to_tomas(slack, workspace)
    reply = await decided(tomas(ScriptedPress(label="Reject", form=[FormInput(value="no")])), asked, workspace)
    with pytest.raises(FormNeverOpened):
        await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)


async def test_a_press_on_a_control_the_message_does_not_carry_is_refused(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    asked = await card_to_tomas(slack, workspace)
    reply = PersonReply(
        person="tomas",
        in_reply_to=asked.entity,
        text="Approve all",
        at=workspace.clock.now(),
        press=Press(action_id="approve_plan_op1", label="Approve all"),
    )
    with pytest.raises(LookupError, match="approve_plan_op1"):
        await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)
    assert agent.received == []


async def test_a_scripted_press_on_a_label_the_message_lacks_is_no_reply(
    slack: Intercepted, workspace: Workspace
) -> None:
    asked = await card_to_tomas(slack, workspace)
    person = tomas(ScriptedPress(label="Approve all 3"))
    scenario = SCENARIO.model_copy(update={"people": [*SCENARIO.people[:1], person, SCENARIO.people[2]]})
    assert (
        await PeopleReplier(scenario, people_model()).decide(person, asked, workspace.store.events(), workspace.clock)
        is None
    )
    assert isinstance(asked.after, MessageSnapshot) and [a.label for a in asked.after.actions] == [
        "Accept",
        "Reject",
        "Assign to",
    ]


async def test_a_response_url_answers_five_times_and_then_refuses(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    asked = await card_to_tomas(slack, workspace)
    reply = await decided(tomas(ScriptedPress(label="Accept")), asked, workspace)
    await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)
    url = agent.received[0].payload["response_url"]
    async with slack.http() as http:
        answers = [await http.post(url, json={"text": f"note {i}"}) for i in range(6)]
        forged = await http.post(url.rsplit("/", 1)[0] + "/not-the-secret", json={"text": "x"})

    assert [a.status_code for a in answers] == [200] * 5 + [404]
    assert answers[5].json()["error"] == "used_url" and forged.status_code == 404
    notes = [
        e for e in workspace.store.events() if isinstance(e.after, MessageSnapshot) and e.after.text.startswith("note")
    ]
    assert len(notes) == 5 and all(
        isinstance(e.after, MessageSnapshot) and e.after.recipient_emails == ["tomas@example.com"] for e in notes
    ), "ephemeral by default"


async def test_delete_original_removes_the_card(slack: Intercepted, workspace: Workspace, agent: AgentEndpoint) -> None:
    asked = await card_to_tomas(slack, workspace)
    reply = await decided(tomas(ScriptedPress(label="Accept")), asked, workspace)
    await workspace.provider.press(reply, agent.target(), workspace.store, workspace.clock, secret=SECRET)
    async with slack.http() as http:
        await http.post(agent.received[0].payload["response_url"], json={"delete_original": True})
    assert data(await slack.asynchronous().conversations_history(channel=workspace.dm("tomas")))["messages"] == []
