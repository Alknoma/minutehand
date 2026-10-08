"""Method-level coverage of Slack's Web API: every method Slack's own sources list is either served by the fake or
refused by name (501 `not_implemented`, the method named in `response_metadata.messages`), never answered
`unknown_method`, which Slack keeps for a name it has no method by.

Three vendor sources, each read here as the vendor published it (`data/README.md` says where and when each was
taken): Slack's methods index (https://docs.slack.dev/reference/methods), Slack's OpenAPI description of the Web API
(github.com/slackapi/slack-api-specs, archived in 2021), and the methods the pinned `slack_sdk`'s `WebClient` calls.

At this commit: 393 methods in all (340 in the index, 174 in the OpenAPI file, 330 in `slack_sdk`); 23 served and
370 refused by name.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import slack_sdk.web.client
from slack_sdk.errors import SlackApiError

from minutehand.adapters.providers.slack.app import SlackApi
from minutehand.adapters.providers.slack.methods import UNSERVED
from minutehand.domain.world import CallOutcome
from tests.providers.slack.intercepted import Intercepted
from tests.providers.slack.slack_workspace import Workspace

DATA = Path(__file__).parent / "data"


def openapi_methods() -> set[str]:
    spec = json.loads((DATA / "slack_web_openapi_v2_without_examples.json").read_text())
    return {path.strip("/") for path in spec["paths"]}


def indexed_methods() -> set[str]:
    """The first column of the index's table: `| [chat.postMessage](https://docs.slack.dev/...) | ... |`."""
    text = (DATA / "slack_methods_index.md").read_text()
    return set(re.findall(r"^\| \[([A-Za-z][\w.]*)\]\(https://docs\.slack\.dev/reference/methods/", text, re.M))


def sdk_methods() -> set[str]:
    """Every method name `WebClient` passes to `api_call`, read from its source."""
    source = Path(slack_sdk.web.client.__file__).read_text()
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "api_call"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def every_method() -> set[str]:
    return openapi_methods() | indexed_methods() | sdk_methods()


def served(workspace: Workspace) -> frozenset[str]:
    return SlackApi(workspace.store, workspace.clock).served


def test_each_source_reads_as_the_vendor_published_it() -> None:
    assert len(openapi_methods()) == 174
    assert len(indexed_methods()) == 340
    assert "chat.postMessage" in sdk_methods() and "apps.connections.open" in sdk_methods()


def test_every_web_api_method_is_served_or_refused_by_name(workspace: Workspace) -> None:
    listed = every_method()
    answered = served(workspace)

    assert listed - answered - UNSERVED == set(), "a Slack method neither served nor refused by name"
    assert answered & UNSERVED == set(), "a method both served and refused"
    assert UNSERVED - listed == set(), "a refused name no Slack source lists"
    assert answered - listed == set(), "a served name no Slack source lists"
    assert (len(listed), len(answered), len(UNSERVED)) == (393, 23, 370)


async def test_every_unserved_method_is_refused_501_naming_it_through_the_sdk(
    slack: Intercepted, workspace: Workspace
) -> None:
    sdk = slack.asynchronous()

    async def answer(method: str) -> tuple[int, str, str]:
        try:
            await sdk.api_call(method)
        except SlackApiError as refused:
            body = refused.response.data
            assert isinstance(body, dict)
            return refused.response.status_code, body["error"], body["response_metadata"]["messages"][0]
        return 200, "", ""

    answers = await asyncio.gather(*(answer(m) for m in sorted(UNSERVED)))

    wrong = {
        method: got
        for method, got in zip(sorted(UNSERVED), answers, strict=True)
        if got[:2] != (501, "not_implemented") or f"/api/{method}: {method}," not in got[2]
    }
    assert wrong == {}
    outcomes = {c.exchange.outcome for c in workspace.store.calls()}
    assert outcomes == {CallOutcome.NOT_IMPLEMENTED}


async def test_a_name_slack_has_no_method_by_is_refused_unknown_method_as_slack_answers_it(
    slack: Intercepted,
) -> None:
    """OBSERVED: `data/observed/unknown_method.http`, Slack's own answer to `foo.bar`: HTTP 200 and
    `{"ok":false,"error":"unknown_method","req_method":"foo.bar"}`."""
    recorded = (DATA / "observed" / "unknown_method.http").read_text().strip().splitlines()[-1]

    try:
        await slack.asynchronous().api_call("foo.bar")
    except SlackApiError as refused:
        assert refused.response.status_code == 200
        assert refused.response.data == json.loads(recorded)
    else:
        raise AssertionError("foo.bar was answered ok")
