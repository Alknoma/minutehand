"""Two tenants, two bots, two worlds of one `minutehand serve`: each sees only its own messages and files, routed by
the Microsoft JWTs the fake minted. What cannot be routed is a sign-in itself: a client-credentials request carries
its `client_secret` in the form body, which credential routing does not read, so a world's sign-in reaches it only
by the host it claims or as the default."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.adapters.providers.microsoft.state import Directory, directory_of
from minutehand.domain.scenario import Seed
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld

LOGIN_HOSTS = ["login.microsoftonline.com", "login.botframework.com"]


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[MinutehandClient, dict[str, str]]]:
    with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as client:
        yield client, client.environment()


def _seed(name: str, document: str) -> Seed:
    return Seed.model_validate(
        {
            "name": name,
            "starts_at": "2026-09-01T09:00:00Z",
            "people": [
                {"key": "owen", "name": "Owen Owner", "email": f"owen@{name}.example.com", "reply": {"kind": "silent"}}
            ],
            "documents": [{"provider": "microsoft", "title": document, "text": f"Only {name} has this."}],
        }
    )


def _directory(seed: Seed) -> Directory:
    return directory_of(seed.starting(datetime(2026, 9, 1, 9, tzinfo=UTC)))


def _client(environment: dict[str, str]) -> httpx.Client:
    trust = ssl.create_default_context(cafile=environment["SSL_CERT_FILE"])
    return httpx.Client(proxy=environment["HTTPS_PROXY"], verify=trust, trust_env=False)


def _sign_in(http: httpx.Client, directory: Directory, scope: str) -> httpx.Response:
    return http.post(
        f"https://login.microsoftonline.com/{directory.tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": directory.bot_app_id,
            "client_secret": directory.bot_app_secret,
            "scope": scope,
        },
    )


def test_two_tenants_see_only_their_own_files_and_messages_routed_by_the_tokens_minted(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    client, environment = served
    first_seed, second_seed = _seed("tenant_one", "One.docx"), _seed("tenant_two", "Two.docx")
    first = OpenWorld(client, client.create_world(CreateWorld(seed=first_seed, claims=Claims(default=True))))
    second: OpenWorld | None = None
    try:
        with _client(environment) as http:
            one = _directory(first_seed)
            one_graph = _sign_in(http, one, "https://graph.microsoft.com/.default").json()["access_token"]
            one_bot = _sign_in(http, one, "https://api.botframework.com/.default").json()["access_token"]
            # The second world takes the sign-in hosts: from now on every sign-in, whichever tenant, is answered there.
            second = OpenWorld(
                client, client.create_world(CreateWorld(seed=second_seed, claims=Claims(hosts=LOGIN_HOSTS)))
            )
            two = _directory(second_seed)
            two_graph = _sign_in(http, two, "https://graph.microsoft.com/.default").json()["access_token"]
            two_bot = _sign_in(http, two, "https://api.botframework.com/.default").json()["access_token"]

            def names(token: str) -> list[str]:
                site = http.get(
                    "https://graph.microsoft.com/v1.0/sites?search=*", headers={"Authorization": f"Bearer {token}"}
                )
                drive = site.json()["value"][0]["id"]
                listed = http.get(
                    f"https://graph.microsoft.com/v1.0/sites/{drive}/drive/root/children",
                    headers={"Authorization": f"Bearer {token}"},
                )
                return [i["name"] for i in listed.json()["value"]]

            assert names(one_graph) == ["One.docx"]
            assert names(two_graph) == ["Two.docx"]
            for directory, bot, text in ((one, one_bot, "hello one"), (two, two_bot, "hello two")):
                sent = http.post(
                    f"https://smba.trafficmanager.net/teams/v3/conversations/{directory.general_channel_id}/activities",
                    json={"type": "message", "text": text},
                    headers={"Authorization": f"Bearer {bot}"},
                )
                assert sent.status_code == 201, sent.text
            texts = [
                [
                    e.after.text
                    for e in w.events(provider="microsoft")
                    if e.after is not None and e.after.kind == "message"
                ]
                for w in (first, second)
            ]
            assert texts == [["hello one"], ["hello two"]]
    finally:
        client.close_world(first.world_id)
        if second is not None:
            client.close_world(second.world_id)


def test_a_sign_in_of_a_world_that_does_not_hold_the_sign_in_host_is_refused(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    client, environment = served
    first_seed, second_seed = _seed("tenant_three", "Three.docx"), _seed("tenant_four", "Four.docx")
    first = OpenWorld(client, client.create_world(CreateWorld(seed=first_seed, claims=Claims(default=True))))
    second = OpenWorld(client, client.create_world(CreateWorld(seed=second_seed, claims=Claims(hosts=LOGIN_HOSTS))))
    try:
        with _client(environment) as http:
            refused = _sign_in(http, _directory(first_seed), "https://graph.microsoft.com/.default")
        # Answered by the world holding the host, whose directory has no such tenant.
        assert refused.status_code == 400 and refused.json()["error_codes"] == [90002]
        assert [c.exchange.host for c in second.calls()] == ["login.microsoftonline.com"]
    finally:
        client.close_world(first.world_id)
        client.close_world(second.world_id)
