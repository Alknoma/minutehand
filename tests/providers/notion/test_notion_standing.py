"""Notion in the standing mode: each world claims its own integration's secrets, and a call reaches only the world
whose secret it carries, with no change to how the standing mode finds credentials."""

from __future__ import annotations

import base64
import json
import ssl
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from notion_client import APIResponseError, Client

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.adapters.providers.notion.seed import object_id
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor, DocumentSnapshot, Operation
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld


def _seed(label: str) -> Seed:
    notion = {
        "workspaces": [
            {
                "key": label,
                "name": f"Workspace {label}",
                "integrations": [
                    {"key": "agent", "name": "Agent", "tokens": [f"ntn_{label}_secret"], "shared": ["home"]},
                    {
                        "key": "app",
                        "name": "App",
                        "type": "public",
                        "client_id": f"client-{label}",
                        "client_secret": f"secret-{label}",
                        "installed_by": "owen",
                        "authorizations": [{"code": f"code-{label}"}],
                        "shared": ["home"],
                    },
                ],
                "pages": [{"key": "home", "title": f"Home of {label}"}],
            }
        ]
    }
    return Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "people": [{"key": "owen", "name": "Owen", "email": "owen@example.com", "reply": {"kind": "silent"}}],
            "provider_seeds": [{"provider": "notion", "body": notion}],
        }
    )


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[MinutehandClient, dict[str, str]]]:
    with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as client:
        yield client, client.environment()


def _client(environment: dict[str, str], token: str) -> Client:
    trust = ssl.create_default_context(cafile=environment["SSL_CERT_FILE"])
    return Client(auth=token, client=httpx.Client(proxy=environment["HTTPS_PROXY"], verify=trust, trust_env=False))


def _answered(found: object) -> dict[str, Any]:
    """A sync client's answer, which the SDK types as possibly awaitable."""
    assert isinstance(found, dict)
    return found


def _titles(found: object) -> list[str]:
    results = _answered(found)["results"]
    return [r["properties"]["title"]["title"][0]["plain_text"] for r in results]


def test_two_worlds_with_their_own_tokens_do_not_see_each_others_pages(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    client, environment = served
    first = OpenWorld(
        client, client.create_world(CreateWorld(seed=_seed("one"), claims=Claims(tokens=["ntn_one_secret"])))
    )
    second = OpenWorld(
        client, client.create_world(CreateWorld(seed=_seed("two"), claims=Claims(tokens=["ntn_two_secret"])))
    )
    try:
        one, two = _client(environment, "ntn_one_secret"), _client(environment, "ntn_two_secret")
        assert _titles(one.search()) == ["Home of one"]
        assert _titles(two.search()) == ["Home of two"]
        with pytest.raises(APIResponseError) as raised:
            one.pages.retrieve(object_id("two", "home"))
        assert raised.value.code == "object_not_found"
        one.pages.create(
            parent={"page_id": object_id("one", "home")}, properties={"title": [{"text": {"content": "Mine"}}]}
        )
        written = first.events(provider="notion", actor=Actor.AGENT, operation=Operation.CREATE)
        assert [e.after.title for e in written if isinstance(e.after, DocumentSnapshot)] == ["Mine"]
        assert second.events(provider="notion", actor=Actor.AGENT, operation=Operation.CREATE) == []
        one.close()
        two.close()
    finally:
        client.close_world(first.world_id)
        client.close_world(second.world_id)


def test_a_public_integrations_code_exchange_is_routed_by_its_client_secret_and_its_token_follows(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    client, environment = served
    world = OpenWorld(
        client, client.create_world(CreateWorld(seed=_seed("three"), claims=Claims(tokens=["secret-three"])))
    )
    try:
        trust = ssl.create_default_context(cafile=environment["SSL_CERT_FILE"])
        basic = base64.b64encode(b"client-three:secret-three").decode()
        with httpx.Client(proxy=environment["HTTPS_PROXY"], verify=trust, trust_env=False) as http:
            granted = http.post(
                "https://api.notion.com/v1/oauth/token",
                json={"grant_type": "authorization_code", "code": "code-three"},
                headers={"Authorization": f"Basic {basic}"},
            )
        assert granted.status_code == 200, granted.text
        token = json.loads(granted.text)["access_token"]
        app = _client(environment, token)
        assert _answered(app.users.me())["name"] == "App"
        assert _titles(app.search()) == ["Home of three"]
        app.close()
    finally:
        client.close_world(world.world_id)
