"""What Minutehand answers in Slack's place reaches stock `slack_sdk` as Slack's own error: an operation the fake
does not implement as a 501, and Minutehand's own bug as a 500, each in the Web API's `{"ok": false, "error": …}`
with the words in `response_metadata.messages`, so `WebClient` raises `SlackApiError` carrying them.

The Slack app is the real one, served through the real proxy; two test-only routes are added to its Starlette
router, one raising `NotImplementedError` and one raising a bug, so the exception passes through Starlette's own
error middleware as one from any of its handlers would."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.domain.world import CallOutcome
from tests.providers.slack.intercepted import off_loop
from tests.providers.slack.slack_workspace import TOKEN, Workspace

UNIMPLEMENTED = "minutehand.test.unimplemented"
BROKEN = "minutehand.test.broken"


async def _unimplemented(_: Request) -> Response:
    raise NotImplementedError("a method this test adds and never answers")


async def _broken(_: Request) -> Response:
    raise ZeroDivisionError("the test's own bug")


@pytest.mark.parametrize(
    ("method", "status", "code", "said", "kind"),
    [
        (UNIMPLEMENTED, 501, "not_implemented",
         f"minutehand's slack fake does not implement POST /api/{UNIMPLEMENTED}: a method this test adds",
         CallOutcome.NOT_IMPLEMENTED),
        (BROKEN, 500, "internal_error",
         f"minutehand internal error while answering slack POST /api/{BROKEN}: ZeroDivisionError: the test's own bug",
         CallOutcome.INTERNAL_ERROR),
    ],
)  # fmt: skip
async def test_minutehands_own_answer_reaches_slack_sdk_as_a_slack_api_error(
    workspace: Workspace, tmp_path: Path, method: str, status: int, code: str, said: str, kind: CallOutcome
) -> None:
    app = workspace.provider.app(workspace.store, workspace.clock)
    assert isinstance(app, Starlette)
    app.router.routes[:0] = [
        Route(f"/api/{UNIMPLEMENTED}", _unimplemented, methods=["POST"]),
        Route(f"/api/{BROKEN}", _broken, methods=["POST"]),
    ]
    proxy = Proxy(Routing(Registry.installed()), workspace.store, workspace.clock, confdir=tmp_path / "ca")
    async with proxy:
        proxy.mount(workspace.store, workspace.clock, {"slack": app})
        slack = WebClient(token=TOKEN, proxy=proxy.url, ssl=ssl.create_default_context(cafile=str(proxy.ca_bundle)))
        with pytest.raises(SlackApiError) as raised:
            await off_loop(lambda: slack.api_call(method))

    answer = raised.value.response
    assert answer.status_code == status
    assert answer["ok"] is False and answer["error"] == code
    assert answer["response_metadata"]["messages"][0].startswith(said)
    [call] = [c for c in workspace.store.calls() if c.exchange.path == f"/api/{method}"]
    assert call.exchange.outcome is kind
    assert call.exchange.failure is not None and call.exchange.failure.message.startswith(said)
