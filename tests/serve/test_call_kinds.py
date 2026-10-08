"""How each call a standing world answered is recorded (`Exchange.outcome`): answered by the fake, refused as Slack
refuses (`ok: false` at 200 says nothing in its status), or failed on purpose, by a fault the test armed through the
control API or one the provider declares in its own seed. A fault is never recorded as the service's refusal."""

from __future__ import annotations

import pytest
from slack_sdk.errors import SlackApiError

from minutehand.adapters.control.wire import Fault
from minutehand.domain.world import CallOutcome
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, spec


def test_each_call_is_recorded_answered_refused_or_injected_by_whoever_failed_it(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-kinds")))
    try:
        slack = served.slack("xoxb-kinds")
        slack.retry_handlers.clear()
        slack.auth_test()
        with pytest.raises(SlackApiError):
            slack.chat_postMessage(channel="C0NOSUCHCHANNEL", text="hello")
        world.arm(Fault(provider="slack", path="/api/users.list", status=503, body='{"ok": false}'))
        with pytest.raises(SlackApiError):
            slack.users_list()
        world.declare_faults(
            "slack", {"faults": [{"call": "conversations.list", "answer": {"kind": "refused", "error": "x"}}]}
        )
        with pytest.raises(SlackApiError):
            slack.conversations_list()

        recorded = [(c.exchange.path, c.exchange.status, c.exchange.outcome) for c in world.calls()]
        assert recorded == [
            ("/api/auth.test", 200, CallOutcome.ANSWERED),
            ("/api/chat.postMessage", 200, CallOutcome.REFUSED),
            ("/api/users.list", 503, CallOutcome.INJECTED_FAULT),
            ("/api/conversations.list", 200, CallOutcome.INJECTED_FAULT),
        ]
        assert all(c.exchange.failure is None for c in world.calls())
    finally:
        served.client.close_world(world.world_id)
