"""The control API has a person act now in any family (`Happen`), use a control on a message (`PressControl`),
and a provider declare its own typed faults on an open world (`DeclareFaults`), each seen through the real SDK
or client a service runs."""

from __future__ import annotations

import base64
import json
import ssl
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from slack_sdk.errors import SlackApiError

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.people import Press
from minutehand.domain.scenario import (
    DocumentHappening,
    FieldSet,
    Moves,
    PersonPosts,
    Seed,
    TicketHappening,
    TicketState,
)
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot
from minutehand.testing.client import Refused
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, answer, dm, event_receiver, spec


def test_a_person_posts_now_and_the_service_is_pushed_the_event(served: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(served.client, served.client.create_world(spec("xoxb-happen", inbound=receiver.url)))
        try:
            posted = world.happen(PersonPosts(provider="slack", person="sofia", text="The venue is booked."))
            assert posted.actor is Actor.PERSON and isinstance(posted.after, MessageSnapshot)
            assert receiver.texts() == ["The venue is booked."]
        finally:
            served.client.close_world(world.world_id)


def test_a_person_moves_a_jira_issue_now_and_the_client_reads_it_done(served: Served) -> None:
    seed = Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "people": [{"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}}],
            "tickets": [{"key": "venue", "provider": "jira", "project": "Ops", "title": "Book the venue",
                         "assignee": "owen"}],
            "provider_seeds": [{"provider": "jira", "body": {"site": "controlact", "agent_email": "agent@x.example",
                                                             "credentials": [{"account": "agent", "api_token": "ca-t"}]}}],
        }
    )  # fmt: skip
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(keys=["controlact"])))
    )
    try:
        event = world.happen(
            TicketHappening(
                person="owen", ticket="Book the venue", after=timedelta(0), action=Moves(to=TicketState.DONE)
            )
        )
        assert event.actor is Actor.PERSON
        basic = base64.b64encode(b"agent@x.example:ca-t").decode()
        trust = ssl.create_default_context(cafile=served.bundle)
        with httpx.Client(proxy=served.proxy, verify=trust, trust_env=False, timeout=30) as http:
            issue = http.get(
                "https://controlact.atlassian.net/rest/api/3/issue/OPS-1",
                params={"fields": "status"},
                headers={"Authorization": f"Basic {basic}"},
            )
        assert issue.status_code == 200, issue.text
        assert issue.json()["fields"]["status"]["statusCategory"]["key"] == "done"
    finally:
        served.client.close_world(world.world_id)


def test_a_happening_its_provider_cannot_show_is_refused_and_nothing_changes(served: Served) -> None:
    seed = Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "people": [{"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}}],
            "documents": [{"provider": "google_drive", "title": "Plan", "text": "x"}],
        }
    )
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=["ya29.refuse"])))
    )
    try:
        head = served.client.world(world.world_id).head
        with pytest.raises(Refused) as refused:
            world.happen(
                DocumentHappening(person="owen", document="Plan", after=timedelta(hours=1),
                                  action=FieldSet(field="Status", value="Done"))
            )  # fmt: skip
        assert refused.value.status == 409 and "no way to show that" in refused.value.error
        assert served.client.world(world.world_id).head == head
    finally:
        served.client.close_world(world.world_id)


def test_a_person_presses_a_button_on_the_agents_message_and_the_service_gets_block_actions(served: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(served.client, served.client.create_world(spec("xoxb-press", inbound=receiver.url)))
        try:
            slack = served.slack("xoxb-press")
            channel = dm(served, "xoxb-press", "sofia@example.com")
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Approve the venue?"}},
                {"type": "actions", "elements": [{"type": "button", "action_id": "approve", "value": "yes",
                                                  "text": {"type": "plain_text", "text": "Approve"}}]},
            ]  # fmt: skip
            sent = answer(slack.chat_postMessage(channel=channel, text="Approve the venue?", blocks=blocks))
            message = next(
                e.entity
                for e in world.events(provider="slack", actor=Actor.AGENT)
                if e.entity.kind is EntityKind.MESSAGE and isinstance(e.after, MessageSnapshot) and e.after.actions
            )
            pressed = world.press("sofia", message, Press(action_id="approve", label="Approve", value="yes"))
            assert pressed.actor is Actor.PERSON
            payload = json.loads(parse_qs(receiver.pushed[-1].body.decode())["payload"][0])
            assert payload["type"] == "block_actions" and payload["actions"][0]["action_id"] == "approve"
            assert payload["message"]["ts"] == sent["ts"]
        finally:
            served.client.close_world(world.world_id)


def test_slack_declares_its_own_fault_on_an_open_world_and_slack_sdk_meets_it_once(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-declared")))
    try:
        world.declare_faults(
            "slack",
            {"faults": [{"call": "chat.postMessage", "answer": {"kind": "refused", "error": "channel_not_found"}}]},
        )
        slack = served.slack("xoxb-declared")
        channel = dm(served, "xoxb-declared", "sofia@example.com")
        with pytest.raises(SlackApiError) as failed:
            slack.chat_postMessage(channel=channel, text="first")
        assert failed.value.response["error"] == "channel_not_found"
        assert answer(slack.chat_postMessage(channel=channel, text="second"))["ok"] is True
    finally:
        served.client.close_world(world.world_id)


def test_a_fault_declaration_setting_anything_but_faults_is_refused(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-not-faults")))
    try:
        with pytest.raises(Refused) as refused:
            world.declare_faults("google_drive", {"faults": [], "tokens": []})
        assert refused.value.status in (409, 422)
        with pytest.raises(Refused) as unknown:
            world.declare_faults("slack", {"faults": [{"answer": {"kind": "melted"}}]})
        assert unknown.value.status in (409, 422)
    finally:
        served.client.close_world(world.world_id)
