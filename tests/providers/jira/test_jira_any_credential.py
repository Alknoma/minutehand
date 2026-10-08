"""Minutehand does not enforce credentials or permissions: every credential, or none, is let in, and a credential
only says who calls. Who can see a project is the world's data and still holds (`test_jira_refusals`)."""

from __future__ import annotations

from datetime import timedelta

from minutehand.domain.world import Actor
from tests.providers.jira.jira_site import (
    AGENT,
    AGENT_TOKEN,
    API,
    CLOUD_ID,
    IRIS,
    IRIS_TOKEN,
    OAUTH_ACCESS,
    START,
    TOMAS,
    Site,
    basic,
    ok,
    refused,
)

EX = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"


async def test_a_token_nobody_was_given_acts_as_the_agent(site: Site) -> None:
    async with site.client(basic("stranger@example.com", "ATATT3x-not-a-token")) as stranger:
        assert ok(await stranger.get(f"{API}/myself"))["accountId"] == AGENT
        assert ok(await stranger.get(f"{API}/issue/LAUNCH-1"))["key"] == "LAUNCH-1"


async def test_a_call_with_no_credentials_at_all_acts_as_the_agent(site: Site) -> None:
    async with site.client(None) as anonymous:
        assert ok(await anonymous.get(f"{API}/myself"))["accountId"] == AGENT
        assert ok(await anonymous.get(f"{API}/mypermissions", params={"permissions": "CREATE_PROJECT"}))
        assert ok(await anonymous.get(f"{EX}/issue/LAUNCH-1"))["key"] == "LAUNCH-1"


async def test_a_seeded_token_acts_as_its_account_whatever_email_it_is_sent_with(site: Site) -> None:
    async with site.client(basic("iris@example.com", AGENT_TOKEN)) as mixed:
        assert ok(await mixed.get(f"{API}/myself"))["accountId"] == AGENT


async def test_an_unseeded_token_sent_with_an_accounts_email_acts_as_that_account(site: Site) -> None:
    async with site.client(basic("tomas@example.com", "any-token-at-all")) as tomas:
        assert ok(await tomas.get(f"{API}/myself"))["accountId"] == TOMAS


async def test_an_oauth_token_is_let_in_at_the_site_host_and_through_the_gateway(site: Site) -> None:
    async with site.client(f"Bearer {OAUTH_ACCESS}") as app:
        assert ok(await app.get(f"{API}/myself"))["accountId"] == IRIS
        assert ok(await app.get(f"{EX}/myself"))["accountId"] == IRIS


async def test_an_expired_oauth_token_still_acts_as_its_account(site: Site) -> None:
    site.clock.jump(START + timedelta(hours=5))
    async with site.client(f"Bearer {OAUTH_ACCESS}") as app:
        assert ok(await app.get(f"{EX}/myself"))["accountId"] == IRIS


async def test_a_deactivated_accounts_token_still_acts_as_it(site: Site) -> None:
    jira = site.jira
    iris = next(u for u in jira.users() if u.accountId == IRIS)
    jira.write_user(iris.model_copy(update={"active": False}), actor=Actor.SCENARIO)
    async with site.client(basic("iris@example.com", IRIS_TOKEN)) as gone:
        me = ok(await gone.get(f"{API}/myself"))
    assert (me["accountId"], me["active"]) == (IRIS, False)


async def test_a_viewer_edits_transitions_and_deletes_since_no_permission_is_enforced(site: Site) -> None:
    jira = site.jira
    vault = jira.find_project("VAULT")
    assert vault is not None
    members = [m.model_copy(update={"accounts": [*m.accounts, jira.site().agent]}) if m.role == "10004" else m
               for m in vault.members]  # fmt: skip
    jira.write_project(vault.model_copy(update={"members": members}), actor=Actor.SCENARIO)
    ok(await site.http.put(f"{API}/issue/VAULT-1", json={"fields": {"summary": "Mine now"}}), 204)
    ok(await site.http.post(f"{API}/issue/VAULT-1/transitions", json={"transition": {"id": "31"}}), 204)
    perms = ok(
        await site.http.get(f"{API}/mypermissions", params={"permissions": "EDIT_ISSUES", "projectKey": "VAULT"})
    )
    assert perms["permissions"]["EDIT_ISSUES"]["havePermission"] is True
    ok(await site.http.delete(f"{API}/issue/VAULT-1"), 204)


async def test_an_account_that_administers_nothing_creates_a_project_and_adds_role_members(site: Site) -> None:
    async with site.client(basic("tomas@example.com", "any-token")) as tomas:
        made = ok(
            await tomas.post(
                f"{API}/project",
                json={"key": "TOMAS", "name": "Tomas's", "projectTypeKey": "software", "leadAccountId": TOMAS},
            ),
            201,
        )
        roles = ok(await tomas.get(f"{API}/project/LAUNCH/role"))
        viewer = roles["Viewer"].rsplit("/", 1)[1]
        added = ok(await tomas.post(f"{API}/project/LAUNCH/role/{viewer}", json={"user": [AGENT]}))
    assert made["key"] == "TOMAS"
    assert AGENT in [a["actorUser"]["accountId"] for a in added["actors"]]


async def test_who_belongs_to_a_project_still_decides_what_a_caller_sees(site: Site) -> None:
    async with site.client(None) as anonymous:
        refused(await anonymous.get(f"{API}/issue/VAULT-1"), 404)


async def test_any_refresh_token_code_or_client_is_traded_for_tokens_that_act_as_the_agent(site: Site) -> None:
    async with site.client(None) as anonymous:
        for grant in (
            {"grant_type": "refresh_token", "refresh_token": "never-issued", "client_id": "x", "client_secret": "y"},
            {"grant_type": "authorization_code", "code": "never-issued", "client_id": "x", "redirect_uri": "z"},
            {"grant_type": "client_credentials", "client_id": "x", "client_secret": "y"},
        ):
            pair = ok(await anonymous.post("https://auth.atlassian.com/oauth/token", json=grant))
            async with site.client(f"Bearer {pair['access_token']}") as app:
                [resource] = ok(await app.get("https://api.atlassian.com/oauth/token/accessible-resources"))
                assert resource["id"] == CLOUD_ID
                assert ok(await app.get(f"{EX}/myself"))["accountId"] == AGENT


async def test_a_grant_the_token_endpoint_does_not_have_is_refused(site: Site) -> None:
    async with site.client(None) as anonymous:
        answer = await anonymous.post("https://auth.atlassian.com/oauth/token", json={"grant_type": "password"})
    assert (answer.status_code, answer.json()["error"]) == (400, "unsupported_grant_type")
