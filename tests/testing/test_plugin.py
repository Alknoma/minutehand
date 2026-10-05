"""The pytest plugin, end to end, in a project of its own run by pytest in a subprocess: the plugin is loaded
from the installed entry point, as it is in any suite once minutehand is installed."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.timeout(120)

CONFTEST = """
import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed


@pytest.fixture
def minutehand_spec(request):
    token = "xoxb-" + request.node.name
    return CreateWorld(
        seed=Seed.model_validate(
            {"people": [{"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com"}]}
        ),
        claims=Claims(tokens=[token]),
    )
"""

TESTS = """
import ssl

from slack_sdk import WebClient


def _slack(minutehand, world):
    env = minutehand.environment()
    token = world.view.claims.tokens[0]
    return WebClient(
        token=token, proxy=env["HTTPS_PROXY"], ssl=ssl.create_default_context(cafile=env["SSL_CERT_FILE"])
    )


def test_one(minutehand, minutehand_world):
    slack = _slack(minutehand, minutehand_world)
    channel = slack.conversations_list()["channels"][0]["id"]
    slack.chat_postMessage(channel=channel, text="from test one")
    minutehand_world.assert_message(containing="from test one")
    minutehand_world.assert_message(containing="from test two", at_least=0)


def test_two(minutehand, minutehand_world):
    slack = _slack(minutehand, minutehand_world)
    channel = slack.conversations_list()["channels"][0]["id"]
    slack.chat_postMessage(channel=channel, text="from test two")
    texts = [e.after.text for e in minutehand_world.events() if e.after is not None and e.after.kind == "message"]
    assert texts == ["from test two"]


def test_fails_and_says_what_the_world_did(minutehand, minutehand_world):
    slack = _slack(minutehand, minutehand_world)
    channel = slack.conversations_list()["channels"][0]["id"]
    slack.chat_postMessage(channel=channel, text="a status update")
    minutehand_world.assert_message(containing="the budget")


def test_every_world_is_closed_after_its_test(minutehand):
    assert minutehand.worlds() == []
"""

UNUSED = """
import sys


def test_nothing_of_minutehand_is_loaded():
    assert "minutehand.serve" not in sys.modules
    assert "minutehand.testing.client" not in sys.modules
    assert "mitmproxy" not in sys.modules
"""


def test_a_suite_opens_a_world_per_test_and_a_failure_shows_what_the_world_did(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_suite=TESTS)
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=3, failed=1)
    output = result.stdout.str()
    assert "wanted at least 1 agent messages holding 'the budget', found 0" in output
    assert "'a status update'" in output


def test_a_suite_that_requests_no_fixture_is_unchanged(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_unused=UNUSED)
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert "minutehand" in pytester.runpytest_subprocess("--trace-config", "-p", "no:randomly").stdout.str()


def test_a_world_with_no_spec_is_a_usage_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_nospec="def test_it(minutehand_world):\n    pass\n")
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider")
    assert "define it in your conftest.py" in result.stdout.str()
