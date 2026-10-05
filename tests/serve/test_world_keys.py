"""Calls that carry no credential, or one no world claimed, reach their world by what their URL names
(`Manifest.world_keys`, claimed as `Claims.keys`) or by the token request's own fields: two Microsoft tenants
sign in at once in two worlds of one `minutehand serve`, and what each reads is its own."""

from __future__ import annotations

import base64
import json
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.adapters.providers.jira.manifest import MANIFEST as JIRA
from minutehand.adapters.providers.microsoft import docx as word
from minutehand.adapters.providers.microsoft.manifest import MANIFEST as MICROSOFT
from minutehand.adapters.providers.microsoft.state import Directory, directory_of
from minutehand.adapters.providers.youtrack.manifest import MANIFEST as YOUTRACK
from minutehand.adapters.proxy.credentials import presented
from minutehand.domain.provider import world_keys
from minutehand.domain.scenario import Seed
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld

FORM = "application/x-www-form-urlencoded"


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[MinutehandClient, dict[str, str]]]:
    with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as client:
        yield client, client.environment()


# ------------------------------------------------------------------ what a request names, read alone


def _jwt(claims: dict[str, str]) -> str:
    def part(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


def test_a_token_request_in_json_is_read_by_its_refresh_token_code_and_client() -> None:
    def read(body: dict[str, str]) -> list[str]:
        return presented(authorization=None, path="/oauth/token", content_type="application/json",
                         body=json.dumps(body).encode())  # fmt: skip

    assert read({"grant_type": "refresh_token", "refresh_token": "r-1"}) == ["r-1"]
    assert read({"grant_type": "authorization_code", "code": "c-1", "client_id": "app", "client_secret": "s"}) == [
        "c-1",
        "app",
        "s",
    ]
    assert read({"query": "a search body"}) == [], "a JSON body naming no token field presents nothing"


def test_a_form_presents_client_credentials_a_client_assertion_and_slacks_legacy_token() -> None:
    def read(form: str) -> list[str]:
        return presented(authorization=None, path="/token", content_type=FORM, body=form.encode())

    assert read("grant_type=client_credentials&client_id=app-1&client_secret=s-1") == ["app-1", "s-1"]
    assertion = _jwt({"iss": "app-2", "sub": "app-2", "aud": "https://login.microsoftonline.com/t/oauth2/v2.0/token"})
    assert read(f"grant_type=client_credentials&client_assertion={assertion}&client_assertion_type=jwt") == ["app-2"]
    assert read("token=xoxb-legacy&channel=C1") == ["xoxb-legacy"]


@pytest.mark.parametrize(
    ("manifest", "host", "path", "keys"),
    [
        (MICROSOFT, "login.microsoftonline.com", "/tenant-a/oauth2/v2.0/token", ["tenant-a"]),
        (MICROSOFT, "login.microsoftonline.com", "/tenant-a/oauth2/v2.0/authorize?client_id=x", ["tenant-a"]),
        (MICROSOFT, "login.microsoftonline.com", "/contoso.onmicrosoft.com/v2.0/.well-known/openid-configuration",
         ["contoso.onmicrosoft.com"]),
        (MICROSOFT, "login.microsoftonline.com", "/tenant-a/discovery/v2.0/keys", ["tenant-a"]),
        (MICROSOFT, "login.microsoftonline.com", "/discovery/v2.0/keys", []),
        (MICROSOFT, "acme.sharepoint.com", "/_layouts/15/download.aspx?tempauth=x", ["acme"]),
        (MICROSOFT, "acme-my.sharepoint.com", "/personal/x", ["acme-my", "acme"]),
        (JIRA, "acme.atlassian.net", "/rest/api/3/myself", ["acme"]),
        (JIRA, "api.atlassian.com", "/ex/jira/0e1f-cloud/rest/api/3/myself", ["0e1f-cloud"]),
        (JIRA, "api.atlassian.com", "/oauth/token/accessible-resources", []),
        (YOUTRACK, "acme.youtrack.cloud", "/api/issues", ["acme"]),
        (YOUTRACK, "acme.myjetbrains.com", "/youtrack/api/issues", ["acme"]),
    ],
)  # fmt: skip
def test_a_providers_manifest_says_which_part_of_a_url_names_a_world(
    manifest: object, host: str, path: str, keys: list[str]
) -> None:
    from minutehand.domain.provider import Manifest

    assert isinstance(manifest, Manifest)
    assert world_keys(manifest, host, path) == keys


# ------------------------------------------------------------------ two tenants, one server


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


def _keys(directory: Directory) -> Claims:
    """What a tenant's world claims: its tenant id and domain, and its SharePoint host's label; no token."""
    return Claims(
        keys=[directory.tenant_id, directory.tenant_domain, directory.sharepoint_host.split(".")[0]],
    )


def _client(environment: dict[str, str]) -> httpx.Client:
    trust = ssl.create_default_context(cafile=environment["SSL_CERT_FILE"])
    return httpx.Client(proxy=environment["HTTPS_PROXY"], verify=trust, trust_env=False, timeout=30)


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


def test_two_tenants_sign_in_at_once_and_each_reads_only_its_own_files_and_messages(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    client, environment = served
    seeds = {"one": _seed("tenant_one", "One.docx"), "two": _seed("tenant_two", "Two.docx")}
    directories = {label: _directory(seed) for label, seed in seeds.items()}
    worlds = {
        label: OpenWorld(client, client.create_world(CreateWorld(seed=seed, claims=_keys(directories[label]))))
        for label, seed in seeds.items()
    }
    try:
        signed: dict[tuple[str, str], httpx.Response] = {}
        failures: list[BaseException] = []

        def sign_in(label: str) -> None:
            try:
                with _client(environment) as http:
                    for scope in ("https://graph.microsoft.com/.default", "https://api.botframework.com/.default"):
                        signed[(label, scope)] = _sign_in(http, directories[label], scope)
            except BaseException as e:
                failures.append(e)

        threads = [threading.Thread(target=sign_in, args=(label,)) for label in seeds]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert failures == []
        assert {k: r.status_code for k, r in signed.items()} == {k: 200 for k in signed}, {
            k: r.text for k, r in signed.items() if r.status_code != 200
        }

        with _client(environment) as http:
            for label, directory in directories.items():
                graph = signed[(label, "https://graph.microsoft.com/.default")].json()["access_token"]
                bot = signed[(label, "https://api.botframework.com/.default")].json()["access_token"]
                site = http.get(
                    "https://graph.microsoft.com/v1.0/sites?search=*", headers={"Authorization": f"Bearer {graph}"}
                ).json()["value"][0]["id"]
                listed = http.get(
                    f"https://graph.microsoft.com/v1.0/sites/{site}/drive/root/children",
                    headers={"Authorization": f"Bearer {graph}"},
                ).json()["value"]
                assert [i["name"] for i in listed] == [f"{label.title()}.docx"]
                sent = http.post(
                    f"https://smba.trafficmanager.net/teams/v3/conversations/{directory.general_channel_id}/activities",
                    json={"type": "message", "text": f"hello {label}"},
                    headers={"Authorization": f"Bearer {bot}"},
                )
                assert sent.status_code == 201, sent.text
        texts = {
            label: [e.after.text for e in w.events(provider="microsoft") if e.after is not None and e.after.kind == "message"]  # type: ignore[union-attr]
            for label, w in worlds.items()
        }  # fmt: skip
        assert texts == {"one": ["hello one"], "two": ["hello two"]}
        assert all(w.unmatched_calls() == [] for w in worlds.values())
    finally:
        for world in worlds.values():
            client.close_world(world.world_id)


def test_metadata_keys_authorize_and_a_download_with_no_credential_reach_their_tenants_world(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    client, environment = served
    seeds = {"three": _seed("tenant_three", "Three.docx"), "four": _seed("tenant_four", "Four.docx")}
    directories = {label: _directory(seed) for label, seed in seeds.items()}
    worlds = {
        label: OpenWorld(client, client.create_world(CreateWorld(seed=seed, claims=_keys(directories[label]))))
        for label, seed in seeds.items()
    }
    try:
        with _client(environment) as http:
            for label, directory in directories.items():
                login = f"https://login.microsoftonline.com/{directory.tenant_domain}"
                metadata = http.get(f"{login}/v2.0/.well-known/openid-configuration")
                assert metadata.status_code == 200, metadata.text
                assert directory.tenant_id in metadata.json()["issuer"]
                assert (
                    http.get(f"https://login.microsoftonline.com/{directory.tenant_id}/discovery/v2.0/keys").status_code
                    == 200
                )
                authorized = http.get(
                    f"{login}/oauth2/v2.0/authorize",
                    params={"client_id": directory.bot_app_id, "redirect_uri": "https://app.example/back",
                            "login_hint": f"owen@{seeds[label].name}.example.com", "response_type": "code"},
                )  # fmt: skip
                assert authorized.status_code == 302, authorized.text
                code = parse_qs(urlsplit(authorized.headers["location"]).query)["code"][0]
                token = http.post(
                    f"https://login.microsoftonline.com/{directory.tenant_id}/oauth2/v2.0/token",
                    data={"grant_type": "authorization_code", "code": code, "client_id": directory.bot_app_id,
                          "client_secret": directory.bot_app_secret, "redirect_uri": "https://app.example/back",
                          "scope": "https://graph.microsoft.com/.default"},
                )  # fmt: skip
                assert token.status_code == 200, token.text
                bearer = {"Authorization": f"Bearer {token.json()['access_token']}"}
                site = http.get("https://graph.microsoft.com/v1.0/sites?search=*", headers=bearer).json()["value"][0][
                    "id"
                ]
                [document] = http.get(
                    f"https://graph.microsoft.com/v1.0/sites/{site}/drive/root/children", headers=bearer
                ).json()["value"]
                redirected = http.get(
                    f"https://graph.microsoft.com/v1.0/sites/{site}/drive/items/{document['id']}/content",
                    headers=bearer,
                )
                assert redirected.status_code == 302
                download = redirected.headers["location"]
                assert urlsplit(download).hostname == directory.sharepoint_host
                fetched = http.get(download)
                assert fetched.status_code == 200, fetched.text
                assert word.text_of(fetched.content) == f"Only {seeds[label].name} has this.", "its own tenant's file"
            for world in worlds.values():
                assert "login.microsoftonline.com" in {c.exchange.host for c in world.calls()}
                assert world.unmatched_calls() == []
    finally:
        for world in worlds.values():
            client.close_world(world.world_id)
