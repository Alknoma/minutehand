"""An agent that takes Slack's events over Socket Mode: no request URL and no wake endpoint, only the WebSocket it
opens with `slack_sdk`'s own `SocketModeClient`, configured by nothing but the environment it is started with.

The owner's DM is its goal: it asks ASK_EMAIL a question. The answer from that person is thanked. It acknowledges
each envelope once it has acted on it, well inside Slack's three seconds, and keeps what it heard in a state file.

    python socket_agent.py --state FILE
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path
from typing import Any

from slack_sdk import WebClient
from slack_sdk.socket_mode.builtin import SocketModeClient
from slack_sdk.socket_mode.client import BaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web import SlackResponse

TOKEN = "xoxb-agent-under-test"
APP_TOKEN = "xapp-1-A0AGENT-1-app-level"
QUESTION = "Could you confirm the partner pricing, please?"
THANKS = "Thank you!"

parser = argparse.ArgumentParser()
parser.add_argument("--state", type=Path, required=True)
args = parser.parse_args()

lock = threading.Lock()
state: dict[str, list[str]] = {"heard": [], "envelopes": []}
web = WebClient(token=TOKEN)


def save() -> None:
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state))


def answered(response: SlackResponse) -> dict[str, Any]:
    """Slack's JSON for one call; the SDK has already raised on `ok: false`."""
    assert isinstance(response.data, dict)
    return response.data


def dm(email: str, text: str) -> None:
    user = answered(web.users_lookupByEmail(email=email))["user"]["id"]
    channel = answered(web.conversations_open(users=[user]))["channel"]["id"]
    web.chat_postMessage(channel=channel, text=text)


def handle(client: BaseSocketModeClient, request: SocketModeRequest) -> None:
    if request.type != "events_api" or request.payload is None:
        return
    event = request.payload["event"]
    with lock:
        state["envelopes"].append(request.envelope_id)
        if event["type"] == "message" and "bot_id" not in event:
            asker = answered(web.users_info(user=event["user"]))["user"]["profile"]["email"]
            state["heard"].append(event["text"])
            if asker == os.environ["ASK_EMAIL"]:
                dm(asker, THANKS)
            else:
                dm(os.environ["ASK_EMAIL"], QUESTION)
        save()
    client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))


client = SocketModeClient(app_token=APP_TOKEN, web_client=web)
client.socket_mode_request_listeners.append(handle)
client.connect()
save()
threading.Event().wait()
