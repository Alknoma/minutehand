"""Worlds in one standing server: each call reaches the world that claims its credential, and only that one."""

from __future__ import annotations

import ssl
import threading
from datetime import timedelta

import httpx
import pytest
import requests
from pydantic import ValidationError
from slack_sdk.errors import SlackApiError

from minutehand.adapters.control.wire import Claims, CreateWorld, Fault
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from minutehand.testing.client import Refused
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, answer, dm, seed, spec


def _general(world: OpenWorld) -> str:
    channels = [s for s in world.entities(provider="slack", kind=EntityKind.CHANNEL) if '"is_general":true' in s.body]
    assert len(channels) == 1
    return channels[0].entity.external_id


def _posted(world: OpenWorld) -> list[str]:
    return [
        e.after.text
        for e in world.events(provider="slack", actor=Actor.AGENT, operation=Operation.CREATE)
        if isinstance(e.after, MessageSnapshot)
    ]


def test_two_worlds_called_at_once_each_see_only_the_calls_carrying_their_own_token(served: Served) -> None:
    first = OpenWorld(served.client, served.client.create_world(spec("xoxb-world-one")))
    second = OpenWorld(served.client, served.client.create_world(spec("xoxb-world-two")))
    try:
        channels = {"one": _general(first), "two": _general(second)}
        failures: list[BaseException] = []

        def post(token: str, label: str) -> None:
            try:
                slack = served.slack(token)
                for n in range(15):
                    slack.chat_postMessage(channel=channels[label], text=f"{label} {n}")
            except BaseException as e:
                failures.append(e)

        threads = [
            threading.Thread(target=post, args=("xoxb-world-one", "one")),
            threading.Thread(target=post, args=("xoxb-world-two", "two")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert failures == []
        assert sorted(_posted(first)) == sorted(f"one {n}" for n in range(15))
        assert sorted(_posted(second)) == sorted(f"two {n}" for n in range(15))
        assert {c.exchange.status for c in first.calls()} == {200}
    finally:
        served.client.close_world(first.world_id)
        served.client.close_world(second.world_id)


def test_a_call_with_a_token_no_world_claims_is_refused_and_readable(served: Served) -> None:
    before = served.client.unmatched().head
    with pytest.raises(SlackApiError) as raised:
        served.slack("xoxb-nobody-claims-this").auth_test()
    assert raised.value.response.status_code == 502
    found = served.client.unmatched(since=before)
    # The lobby is the whole server's: a call another test's client library makes late, after its world closed,
    # lands here too. This test answers for its own call.
    mine = [c for c in found.calls if c.provider == "slack"]
    assert [(c.exchange.path, c.exchange.status) for c in mine] == [("/api/auth.test", 502)]
    assert "nobody-claims-this" not in (mine[0].exchange.request_body or "")


def test_a_seeded_world_is_read_and_written_through_the_real_sdk_and_inspected(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-seeded")))
    try:
        slack = served.slack("xoxb-seeded")
        members = answer(slack.users_list())["members"]
        names = {u["profile"]["email"] for u in members if "email" in u["profile"]}
        assert names == {"owen@example.com", "sofia@example.com"}
        slack.chat_postMessage(
            channel=dm(served, "xoxb-seeded", "sofia@example.com"), text="Could you confirm the venue, please?"
        )

        sent = world.assert_message(containing="confirm the venue", to="sofia@example.com")
        assert len(sent) == 1 and sent[0].actor is Actor.AGENT
        with pytest.raises(AssertionError) as shown:
            world.assert_message(containing="the budget")
        assert str(shown.value).startswith("wanted at least 1 agent messages holding 'the budget', found 0")
        assert "Could you confirm the venue, please?" in str(shown.value)
    finally:
        served.client.close_world(world.world_id)


def test_a_service_account_signs_in_and_its_minted_token_is_routed_to_the_same_world(served: Served) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2 import service_account

    account = "reader@sim-project.iam.example.com"
    world = OpenWorld(
        served.client,
        served.client.create_world(
            CreateWorld(
                seed=seed(("owen", "Owen Owner")),
                claims=Claims(tokens=[account]),
            )
        ),
    )
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        credentials = service_account.Credentials.from_service_account_info(
            {
                "type": "service_account",
                "project_id": "sim-project",
                "private_key_id": "k1",
                "private_key": pem,
                "client_email": account,
                "client_id": "100000000000000000001",
                "token_uri": "https://oauth2.googleapis.com/token",
            },
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        plain = requests.Session()
        session = AuthorizedSession(credentials, auth_request=Request(plain))
        for configured in (plain, session):
            configured.proxies = {"https": served.proxy}
            configured.verify = served.bundle
            configured.trust_env = False
        listed = session.get("https://www.googleapis.com/drive/v3/files")
        assert listed.status_code == 200, listed.text
        answered = [(c.exchange.host, c.exchange.status) for c in world.calls() if c.provider is not None]
        # google-auth may also ask iamcredentials.googleapis.com in the background, which the Drive provider
        # answers: carrying the minted token, it is this world's, and kept with it.
        assert [a for a in answered if a[0] != "iamcredentials.googleapis.com"] == [
            ("oauth2.googleapis.com", 200),
            ("www.googleapis.com", 200),
        ]
        assert all(status == 200 for _, status in answered)
        assert world.unmatched_calls() == []
    finally:
        served.client.close_world(world.world_id)


def test_a_fault_armed_on_a_world_answers_its_next_matching_call_and_then_stops(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-faulted")))
    try:
        world.arm(
            Fault(
                provider="slack",
                path="/api/chat.postMessage",
                status=429,
                body='{"ok": false, "error": "ratelimited"}',
                retry_after=7,
            )
        )
        slack = served.slack("xoxb-faulted")
        slack.retry_handlers.clear()
        channel = _general(world)
        with pytest.raises(SlackApiError) as raised:
            slack.chat_postMessage(channel=channel, text="first")
        assert raised.value.response.status_code == 429 and raised.value.response.headers["retry-after"] == "7"
        slack.chat_postMessage(channel=channel, text="second")
        assert _posted(world) == ["second"]
        assert [c.exchange.status for c in world.calls()] == [429, 200]
    finally:
        served.client.close_world(world.world_id)


def test_two_open_worlds_claiming_one_token_is_refused(served: Served) -> None:
    world = served.client.create_world(spec("xoxb-taken"))
    try:
        with pytest.raises(Refused) as refused:
            served.client.create_world(spec("xoxb-taken"))
        assert refused.value.status == 409 and "xoxb-taken" in refused.value.error
    finally:
        served.client.close_world(world.world_id)
    served.client.close_world(served.client.create_world(spec("xoxb-taken")).world_id)


def test_moving_a_world_clock_backwards_is_refused(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-clock")))
    try:
        start = world.now()
        assert world.advance(timedelta(hours=3)).now == start + timedelta(hours=3)
        with pytest.raises(Refused) as refused:
            world.advance(to=start)
        assert refused.value.status == 409 and "only moves forward" in refused.value.error
    finally:
        served.client.close_world(world.world_id)


def test_the_control_api_rejects_unknown_fields(served: Served) -> None:
    good = spec("xoxb-strict").model_dump(mode="json")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CreateWorld.model_validate({**good, "tenant": "T1"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Claims.model_validate({"tokens": ["a"], "token": "b"})
    answered = httpx.post(f"{served.url}/v1/worlds", json={**good, "tenant": "T1"}, trust_env=False)
    assert answered.status_code == 422 and "tenant" in answered.json()["error"]
    assert served.client.worlds() == [] or all(w.claims.tokens != ["xoxb-strict"] for w in served.client.worlds())


def test_a_ca_bundle_is_served_for_a_service_to_trust(served: Served) -> None:
    bundle = served.client.ca()
    assert bundle.count(b"BEGIN CERTIFICATE") > 1
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=bundle.decode())


def _otlp(trace_id: str, name: str) -> dict[str, object]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "gateway"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": "00f067aa0ba902b7",
                                "name": name,
                                "startTimeUnixNano": "1790000000000000000",
                                "endTimeUnixNano": "1790000000100000000",
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_spans_are_kept_with_the_world_whose_calls_carried_their_trace(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-traced")))
    try:
        trace = "4bf92f3577b34da6a3ce929d0e0e4736"
        slack = served.slack("xoxb-traced")
        slack.headers = {"traceparent": f"00-{trace}-00f067aa0ba902b7-01"}
        slack.auth_test()
        endpoint = served.environment["OTEL_EXPORTER_OTLP_ENDPOINT"]
        for trace_id, name in ((trace, "post the reminder"), ("0af7651916cd43dd8448eb211c80319c", "elsewhere")):
            sent = httpx.post(f"{endpoint}/v1/traces", json=_otlp(trace_id, name), trust_env=False)
            assert sent.status_code == 200
        assert [s.span.name for s in served.client.spans(world.world_id).spans] == ["post the reminder"]
    finally:
        served.client.close_world(world.world_id)
