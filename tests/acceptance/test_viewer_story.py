"""The page tells the story.

A case driven through `serve`: the agent (this test, on Slack's Web API through the proxy) asks Rosa, its planner's
model call that wrote the question exported over OTLP; Rosa answers; the agent rewrites its question in place and
relays her answer to Owen, written by a second model call. What `minutehand view` serves for that run is read as
JSON, the data its page draws: every message with its recipient and text in order, the rewrite with its old and new
text, each model call with what it wrote, and the case by name. A run nobody judged never reads "Passed" anywhere.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx

from minutehand.adapters.control.wire import CreateWorld
from minutehand.domain.world import EntityKind, EntityRef, MessageSnapshot
from minutehand.testing.world import open_case
from tests.acceptance.support import (
    HERE,
    OWEN,
    PYTHON,
    ROSA,
    STANDIN,
    TOLD_ONLY,
    clean_environment,
    free_port,
    person,
    scripted,
    served,
    started,
    through_proxy,
    viewer,
)

TOKEN = "xoxb-viewer-story"
QUESTION = "Rosa, could you confirm which venue is booked for the offsite?"
REWRITTEN = "Rosa, could you confirm which venue is booked for the offsite? (Answered, thank you.)"
ANSWER = "The lakeside hall is booked for the 14th."
RELAY = "Owen, Rosa says the lakeside hall is booked for the 14th."
MODEL_SPAN = HERE / "agents" / "model_span.py"


def wrote(environment: dict[str, str], text: str) -> None:
    """The agent's model call that wrote `text`, exported as the agent's own telemetry."""
    env = clean_environment({"OTEL_EXPORTER_OTLP_ENDPOINT": environment["OTEL_EXPORTER_OTLP_ENDPOINT"]})
    env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    subprocess.run([PYTHON, str(MODEL_SPAN), text], env=env, check=True, timeout=30)


def slack(http: httpx.Client, method: str, **fields: str) -> dict[str, object]:
    answered = http.post(f"https://slack.com/api/{method}", data=fields, headers={"Authorization": f"Bearer {TOKEN}"})
    said = answered.json()
    assert said["ok"] is True, said
    return said


def dm(http: httpx.Client, email: str, text: str) -> tuple[str, str]:
    user = slack(http, "users.lookupByEmail", email=email)["user"]
    assert isinstance(user, dict)
    channel = slack(http, "conversations.open", users=str(user["id"]))["channel"]
    assert isinstance(channel, dict)
    posted = slack(http, "chat.postMessage", channel=str(channel["id"]), text=text)
    return str(channel["id"]), str(posted["ts"])


def test_the_viewer_data_tells_who_was_told_what_in_order_the_rewrite_and_which_model_call_wrote_each(
    tmp_path: Path,
) -> None:
    port, secret = free_port(), "viewer-story-secret"
    seed = {
        "people": [
            person("owen", OWEN, TOLD_ONLY),
            person("rosa", ROSA, scripted({"to_ask": 1, "text": ANSWER}, hours=1)),
        ],
        "expect": [{"kind": "relayed", "said_by": "rosa", "to": "owen", "tell": "lakeside hall"}],
    }
    with served(tmp_path) as server:
        environment = server.environment(tmp_path / "ca.pem")
        receiver = {"STANDIN_PORT": str(port), "STANDIN_SECRET": secret}
        with (
            started(
                [PYTHON, str(STANDIN)], receiver, tmp_path / "receiver.log", ready=f"http://127.0.0.1:{port}/report"
            ),
            through_proxy(server, tmp_path / "ca.pem") as http,
        ):
            spec = CreateWorld.model_validate(
                {
                    "seed": seed,
                    "claims": {"tokens": [TOKEN]},
                    "inbound": [
                        {"provider": "slack", "url": f"http://127.0.0.1:{port}/slack/events", "secret": secret}
                    ],
                }
            )
            case = open_case(server.client, "viewer_story", [spec])
            world = case.worlds[0]
            with case.step(reason="asks Rosa"):
                wrote(environment, QUESTION)
                channel, ts = dm(http, ROSA, QUESTION)
            case.advance(by=timedelta(hours=2))
            question = next(
                e for e in world.events() if isinstance(e.after, MessageSnapshot) and e.after.text == QUESTION
            )
            world.reply(
                "rosa",
                ANSWER,
                to=EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=question.entity.external_id),
            )
            case.advance(by=timedelta(hours=1))
            with case.step(reason="relays the answer"):
                slack(http, "chat.update", channel=channel, ts=ts, text=REWRITTEN)
                wrote(environment, RELAY)
                dm(http, OWEN, RELAY)
            case.close()

        with viewer(tmp_path, server.state) as view:
            runs = view.get("/api/runs")["runs"]
            assert isinstance(runs, list)
            assert [(r["run_id"], r["case"]) for r in runs] == [(case.case_id, "viewer_story")], "the case is named"

            messages = view.get(f"/api/runs/{case.case_id}/messages")["messages"]
            assert isinstance(messages, list)
            told = [(m["change"], m["to"], m["text"], m["before"]) for m in messages]
            assert told == [
                ("sent", ["Rosa Example"], QUESTION, None),
                ("sent", [], ANSWER, None),
                ("edited", ["Rosa Example"], REWRITTEN, QUESTION),
                ("sent", ["Owen Example"], RELAY, None),
            ]

            traffic = view.get(f"/api/runs/{case.case_id}/model-traffic")["calls"]
            assert isinstance(traffic, list)
            by_seq = {m["seq"]: m["text"] for m in messages}
            assert [[by_seq[s] for s in call["wrote"]] for call in traffic] == [[QUESTION], [RELAY]]
            written = {m["text"]: m["written_by"] for m in messages}
            assert written[QUESTION] is not None and written[RELAY] is not None


def test_a_run_nobody_judged_never_reads_passed_anywhere_the_viewer_serves_it(tmp_path: Path) -> None:
    """A standing world a call reached, with no step and nothing expected: every route of its run is read whole."""
    with served(tmp_path) as server:
        spec = CreateWorld.model_validate(
            {"seed": {"people": [person("owen", OWEN, TOLD_ONLY)]}, "claims": {"tokens": [TOKEN]}}
        )
        opened = server.client.create_world(spec)
        with through_proxy(server, tmp_path / "ca.pem") as http:
            slack(http, "auth.test")
        server.client.close_world(opened.world_id)
        with viewer(tmp_path, server.state) as view:
            runs = view.get("/api/runs")["runs"]
            assert isinstance(runs, list) and [r["run_id"] for r in runs] == [opened.world_id]
            assert runs[0]["verdict"] == "not_judged"
            served_json = [json.dumps(runs)]
            for route in ("", "/findings", "/scorecard", "/wakes", "/events", "/calls", "/messages", "/obligations"):
                served_json.append(json.dumps(view.get(f"/api/runs/{opened.world_id}{route}")))
    assert all("Passed" not in text and '"passed"' not in text for text in served_json)
