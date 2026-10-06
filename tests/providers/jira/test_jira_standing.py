"""Two standing worlds, each with its own Jira site and credentials, behind one `minutehand serve`: a call reaches
the world whose credential it carries, and neither sees the other's issues."""

from __future__ import annotations

import base64
import ssl
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed, TicketState
from minutehand.domain.world import Actor, EntityKind
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[MinutehandClient, dict[str, str]]]:
    with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as client:
        yield client, client.environment()


def _seed(site: str, token: str, title: str, *, refresh: str | None = None, cloud_id: str | None = None) -> Seed:
    credentials: list[dict[str, Any]] = [{"account": "agent", "api_token": token}]
    if refresh is not None:
        credentials.append(
            {
                "account": "agent",
                "oauth": {"access_token": f"{refresh}-access", "refresh_token": refresh, "client_id": "app",
                          "client_secret": "shh"},
            }
        )  # fmt: skip
    return Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "people": [{"key": "owen", "name": "Owen Owner", "email": "owen@example.com"}],
            "tickets": [{"provider": "jira", "project": "Ops", "title": title, "assignee": "owen"}],
            "provider_seeds": [
                {"provider": "jira", "body": {"site": site, "agent_email": f"agent@{site}.example",
                                              "credentials": credentials,
                                              **({"cloud_id": cloud_id} if cloud_id else {})}}
            ],
        }
    )  # fmt: skip


def _client(environment: dict[str, str], site: str, token: str) -> httpx.Client:
    basic = base64.b64encode(f"agent@{site}.example:{token}".encode()).decode()
    return httpx.Client(
        proxy=environment["HTTPS_PROXY"],
        verify=ssl.create_default_context(cafile=environment["SSL_CERT_FILE"]),
        trust_env=False,
        headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
        timeout=30,
    )


def _summaries(http: httpx.Client, site: str) -> list[str]:
    found = http.post(
        f"https://{site}.atlassian.net/rest/api/3/search/jql", json={"jql": "project = OPS", "fields": ["summary"]}
    )
    assert found.status_code == 200, found.text
    return [i["fields"]["summary"] for i in found.json()["issues"]]


def test_two_worlds_each_see_only_their_own_site(served: tuple[MinutehandClient, dict[str, str]]) -> None:
    client, environment = served
    acme = OpenWorld(client, client.create_world(CreateWorld(seed=_seed("acme", "acme-token", "Acme task"),
                                                             claims=Claims(tokens=["acme-token"]))))  # fmt: skip
    globex = OpenWorld(client, client.create_world(CreateWorld(seed=_seed("globex", "globex-token", "Globex task"),
                                                               claims=Claims(tokens=["globex-token"]))))  # fmt: skip
    try:
        with _client(environment, "acme", "acme-token") as a, _client(environment, "globex", "globex-token") as g:
            made = a.post(
                "https://acme.atlassian.net/rest/api/3/issue",
                json={"fields": {"project": {"key": "OPS"}, "summary": "Only at Acme", "issuetype": {"name": "Task"}}},
            )
            assert made.status_code == 201, made.text
            assert _summaries(a, "acme") == ["Acme task", "Only at Acme"]
            assert _summaries(g, "globex") == ["Globex task"]
            crossed = g.get("https://acme.atlassian.net/rest/api/3/issue/OPS-1")
            assert crossed.status_code == 404, "globex's token reaches globex's world, which holds no acme site"
        acme_titles = {
            e.after.title for e in acme.events(provider="jira") if e.after is not None and e.after.kind == "ticket"
        }  # type: ignore[union-attr]
        globex_titles = {
            e.after.title for e in globex.events(provider="jira") if e.after is not None and e.after.kind == "ticket"
        }  # type: ignore[union-attr]
        assert acme_titles == {"Acme task", "Only at Acme"} and globex_titles == {"Globex task"}
    finally:
        client.close_world(acme.view.world_id)
        client.close_world(globex.view.world_id)


def test_an_oauth_refresh_in_json_or_in_a_form_follows_its_world(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    """Atlassian's documented refresh is a JSON body; the proxy reads its `refresh_token` as it reads a form's."""
    client, environment = served
    world = OpenWorld(client, client.create_world(CreateWorld(seed=_seed("initech", "initech-token", "Initech task",
                                                                         refresh="initech-refresh"),
                                                              claims=Claims(tokens=["initech-refresh"]))))  # fmt: skip
    try:
        with httpx.Client(
            proxy=environment["HTTPS_PROXY"],
            verify=ssl.create_default_context(cafile=environment["SSL_CERT_FILE"]),
            trust_env=False,
            timeout=30,
        ) as http:
            as_json = http.post(
                "https://auth.atlassian.com/oauth/token",
                json={"grant_type": "refresh_token", "client_id": "app", "client_secret": "shh",
                      "refresh_token": "initech-refresh"},
            )  # fmt: skip
            assert as_json.status_code == 200, as_json.text
            rotated = as_json.json()["refresh_token"]
            as_form = http.post(
                "https://auth.atlassian.com/oauth/token",
                data={"grant_type": "refresh_token", "client_id": "app", "client_secret": "shh",
                      "refresh_token": rotated},
            )  # fmt: skip
            assert as_form.status_code == 200, as_form.text
            minted = as_form.json()["access_token"]
            resources = http.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {minted}"},
            )
            assert resources.status_code == 200 and resources.json()[0]["name"] == "initech"
    finally:
        client.close_world(world.view.world_id)


def test_a_site_host_and_a_cloud_id_name_their_world_with_no_token_claimed(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    """Each world claims only its site's name and cloud id (`Claims.keys`): the site's own host and the OAuth
    gateway's `/ex/jira/{cloudId}/` path reach it whatever credential the call carries."""
    client, environment = served
    worlds = {
        site: OpenWorld(
            client,
            client.create_world(
                CreateWorld(
                    seed=_seed(
                        site, f"{site}-key-token", f"{site} task", refresh=f"{site}-r", cloud_id=f"{site}-cloud"
                    ),
                    claims=Claims(keys=[site, f"{site}-cloud"]),
                )
            ),
        )
        for site in ("hooli", "piedpiper")
    }
    try:
        for site, world in worlds.items():
            with _client(environment, site, f"{site}-key-token") as http:
                assert _summaries(http, site) == [f"{site} task"]
                me = http.get(
                    f"https://api.atlassian.com/ex/jira/{site}-cloud/rest/api/3/myself",
                    headers={"Authorization": f"Bearer {site}-r-access"},
                )
                assert me.status_code == 200, me.text
            hosts = [c.exchange.host for c in world.calls()]
            assert hosts == [f"{site}.atlassian.net", "api.atlassian.com"]
    finally:
        for world in worlds.values():
            client.close_world(world.view.world_id)


def test_a_person_finishes_a_jira_issue_and_the_scenario_reassigns_it_through_the_control_api(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    client, environment = served
    seeded = Seed.model_validate(
        {
            **_seed("umbrella", "umbrella-token", "Legal review").model_dump(),
            "people": [
                {"key": "owen", "name": "Owen Owner", "email": "owen@example.com"},
                {"key": "dania", "name": "Dania Kovac", "email": "dania@example.com"},
            ],
        }
    )
    world = OpenWorld(client, client.create_world(CreateWorld(seed=seeded, claims=Claims(tokens=["umbrella-token"]))))
    try:
        ticket = world.entities(provider="jira", kind=EntityKind.TICKET)[0].entity
        assert world.move_ticket(ticket, TicketState.DONE).actor is Actor.PERSON
        world.assert_ticket(titled="legal review", state=TicketState.DONE, assignee="owen@example.com")
        assert world.edit_ticket(ticket, assignee="dania").actor is Actor.SCENARIO
        world.assert_ticket(titled="legal review", assignee="dania@example.com")
        with _client(environment, "umbrella", "umbrella-token") as http:
            read = http.get("https://umbrella.atlassian.net/rest/api/3/issue/OPS-1", params={"fields": "status"})
            assert read.json()["fields"]["status"]["name"] == "Done"
    finally:
        client.close_world(world.world_id)
