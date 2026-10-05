"""Errors are told apart.

Through `minutehand serve`, by the vendors' own client libraries where there is one: an operation a fake does not
have reaches the client as the fake's own gap ("minutehand's <provider> fake does not implement <METHOD> <path>",
`docs/design.md`, "What leaves a provider") and is recorded `not_implemented`; Minutehand's own bug in a fake reaches
the client as an internal error naming Minutehand, is recorded `internal_error` with its traceback, and makes the
world's verdict the tool's failure; a fault the test declared is recorded as deliberate, never as the service's
refusal.
"""

from __future__ import annotations

import json
import ssl
from pathlib import Path

import asana
import pytest
from asana.rest import ApiException
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from minutehand.adapters.control.wire import CreateWorld, DeclareFaults, Fault
from minutehand.domain.run import VerdictKind
from minutehand.domain.world import CallOutcome
from tests.acceptance.support import (
    OWEN,
    TOLD_ONLY,
    Served,
    installed_ledger_fake,
    person,
    served,
    through_proxy,
)

SEED = {"people": [person("owen", OWEN, TOLD_ONLY)]}


def default_world(server: Served) -> str:
    return server.client.create_world(CreateWorld.model_validate({"seed": SEED, "claims": {"default": True}})).world_id


def slack_client(server: Served, ca: Path) -> WebClient:
    ca.write_bytes(server.client.ca())
    return WebClient(
        token="xoxb-errors",
        proxy=f"http://127.0.0.1:{server.proxy_port}",
        ssl=ssl.create_default_context(cafile=str(ca)),
    )


def test_an_operation_the_slack_fake_lacks_reaches_slack_sdk_as_the_fakes_gap_not_slacks_refusal(
    tmp_path: Path,
) -> None:
    """`chat.scheduleMessage`, which the Slack fake's README says it does not answer."""
    with served(tmp_path) as server:
        world = default_world(server)
        with pytest.raises(SlackApiError) as raised:
            slack_client(server, tmp_path / "ca.pem").chat_scheduleMessage(channel="C1", text="hi", post_at=1900000000)
        [call] = server.client.calls(world).calls
    assert "minutehand's slack fake does not implement POST /api/chat.scheduleMessage" in str(raised.value)
    assert call.exchange.outcome is CallOutcome.NOT_IMPLEMENTED


def test_an_operation_the_asana_fake_lacks_reaches_the_asana_client_as_the_fakes_gap_not_asanas_refusal(
    tmp_path: Path,
) -> None:
    """Webhooks, which `docs/design.md` says the Asana fake answers 501."""
    with served(tmp_path) as server:
        world = default_world(server)
        configuration = asana.Configuration()
        configuration.access_token = "asana-errors"
        configuration.proxy = f"http://127.0.0.1:{server.proxy_port}"  # pyright: ignore[reportAttributeAccessIssue] - typed None in the library
        ca = tmp_path / "ca.pem"
        ca.write_bytes(server.client.ca())
        configuration.ssl_ca_cert = str(ca)  # pyright: ignore[reportAttributeAccessIssue] - typed None in the library
        webhooks = asana.WebhooksApi(asana.ApiClient(configuration))
        with pytest.raises(ApiException) as raised:
            webhooks.create_webhook({"data": {"resource": "1", "target": "https://example.com/hook"}}, {})
        [call] = server.client.calls(world).calls
    assert raised.value.status == 501
    assert "minutehand's asana fake does not implement POST /webhooks" in str(raised.value.body)
    assert call.exchange.outcome is CallOutcome.NOT_IMPLEMENTED


def test_an_operation_an_installed_fake_lacks_is_answered_501_naming_the_fake_and_recorded_not_implemented(
    tmp_path: Path,
) -> None:
    with served(tmp_path, env=installed_ledger_fake(tmp_path)) as server:
        world = default_world(server)
        with through_proxy(server, tmp_path / "ca.pem") as http:
            answered = http.delete("https://ledger.example/entries/7")
        [call] = server.client.calls(world).calls
        verdict = server.client.checks(world).result.verdict
    assert answered.status_code == 501
    assert "minutehand's ledger fake does not implement DELETE /entries/7" in answered.json()["message"]
    assert call.exchange.outcome is CallOutcome.NOT_IMPLEMENTED
    assert verdict.kind is not VerdictKind.TOOL_FAILED, "a gap in a fake is the agent's world, and is scored"


def test_minutehands_own_bug_in_a_fake_is_an_internal_error_naming_minutehand_and_the_tools_failure(
    tmp_path: Path,
) -> None:
    with served(tmp_path, env=installed_ledger_fake(tmp_path)) as server:
        world = default_world(server)
        with through_proxy(server, tmp_path / "ca.pem") as http:
            answered = http.post("https://ledger.example/entries", content=b"missing")
        [call] = server.client.calls(world).calls
        verdict = server.client.checks(world).result.verdict
    assert answered.status_code == 500
    told = answered.json()["message"]
    assert told.startswith("minutehand internal error while answering ledger POST /entries: KeyError")
    assert call.exchange.outcome is CallOutcome.INTERNAL_ERROR
    assert call.exchange.failure is not None and call.exchange.failure.traceback, "the traceback is kept"
    assert verdict.kind is VerdictKind.TOOL_FAILED
    assert "POST ledger.example/entries" in verdict.words


def test_a_declared_fault_is_recorded_as_deliberate_never_as_the_services_refusal(tmp_path: Path) -> None:
    """One fault armed through the control API with the caller's own body, and one of Slack's own typed faults
    declared on the open world; then the same call once more, answered."""
    with served(tmp_path) as server:
        world = default_world(server)
        server.client.arm(
            world,
            Fault(
                provider="slack",
                method="POST",
                path="/api/auth.test",
                status=503,
                body='{"ok": false, "error": "fatal_error"}',
            ),
        )
        server.client.declare_faults(
            world,
            DeclareFaults(
                provider="slack",
                seed=json.dumps(
                    {"faults": [{"call": "users.list", "answer": {"kind": "rate_limited", "retry_after": "PT7S"}}]}
                ),
            ),
        )
        slack = slack_client(server, tmp_path / "ca.pem")
        slack.retry_handlers.clear()
        with pytest.raises(SlackApiError) as armed:
            slack.auth_test()
        with pytest.raises(SlackApiError) as declared:
            slack.users_list()
        assert slack.auth_test()["ok"] is True
        calls = server.client.calls(world).calls
        verdict = server.client.checks(world).result.verdict

    assert armed.value.response.status_code == 503
    assert declared.value.response.status_code == 429
    assert declared.value.response.headers["retry-after"] in ("7", ["7"])
    assert [c.exchange.outcome for c in calls] == [
        CallOutcome.INJECTED_FAULT,
        CallOutcome.INJECTED_FAULT,
        CallOutcome.ANSWERED,
    ]
    assert verdict.kind is not VerdictKind.TOOL_FAILED
