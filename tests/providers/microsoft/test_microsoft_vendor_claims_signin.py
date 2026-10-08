"""Vendor claims about the identity platform's token endpoint, carried over from an older emulator's own tests and
checked against Microsoft's documentation. `CLAIMS.md` beside the provider lists each one with its source."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx

from tests.providers.microsoft.tenant import LOGIN, Intercepted, Tenant

CLIENT_CREDENTIALS = "https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow"
ERROR_CODES = "https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes"
REFRESH = "https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow"

BOT_SCOPE = "https://api.botframework.com/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
REDIRECT = "https://desk.example.org/microsoft/return"


def _form(tenant: Tenant, scope: str, **changed: str) -> dict[str, str]:
    d = tenant.directory
    form = {
        "grant_type": "client_credentials",
        "client_id": d.bot_app_id,
        "client_secret": d.bot_app_secret,
        "scope": scope,
    }
    return {**form, **changed}


async def _ask(http: httpx.AsyncClient, tenant: Tenant, form: dict[str, str]) -> httpx.Response:
    return await http.post(f"{LOGIN}/{tenant.directory.tenant_id}/oauth2/v2.0/token", data=form)


async def test_a_registered_app_gets_a_bearer_token_for_the_bot_framework_and_for_graph(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Documented: the client-credentials grant answers `access_token`, `token_type` Bearer and a lifetime in
    seconds, for whichever single resource the `.default` scope names. Class (a), CLIENT_CREDENTIALS."""
    async with microsoft.http() as http:
        for scope in (BOT_SCOPE, GRAPH_SCOPE):
            answered = await _ask(http, tenant, _form(tenant, scope))
            assert answered.status_code == 200, answered.text
            body = answered.json()
            assert body["token_type"].lower() == "bearer"
            assert isinstance(body["access_token"], str) and body["access_token"]
            assert isinstance(body["expires_in"], int) and body["expires_in"] > 0


async def test_a_wrong_client_secret_and_an_unknown_app_still_get_a_token(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Minutehand does not enforce credentials (`CLAIMS.md`): the identity platform refuses these (AADSTS7000215,
    AADSTS700016, ERROR_CODES); this provider answers each with a token, for both scopes."""
    async with microsoft.http() as http:
        for scope in (BOT_SCOPE, GRAPH_SCOPE):
            wrong = await _ask(http, tenant, _form(tenant, scope, client_secret="not-this-apps-secret"))
            unknown = await _ask(http, tenant, _form(tenant, scope, client_id="an-app-registered-nowhere"))
            for answered in (wrong, unknown):
                assert answered.status_code == 200, answered.text
                assert answered.json()["access_token"]


async def test_a_refresh_token_the_platform_issued_answers_a_fresh_access_token(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Documented: redeeming a refresh token with the app's credentials answers a new access token. Class (a),
    REFRESH."""
    d = tenant.directory
    async with microsoft.http() as http:
        authorized = await http.get(
            f"{LOGIN}/common/oauth2/v2.0/authorize",
            params={
                "client_id": d.bot_app_id,
                "response_type": "code",
                "redirect_uri": REDIRECT,
                "scope": "offline_access Files.ReadWrite.All",
                "login_hint": "sofia@example.com",
            },
        )
        code = parse_qs(urlsplit(authorized.headers["location"]).query)["code"][0]
        first = await http.post(
            f"{LOGIN}/common/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "client_id": d.bot_app_id,
                "client_secret": d.bot_app_secret,
                "code": code,
                "redirect_uri": REDIRECT,
                "scope": "offline_access Files.ReadWrite.All",
            },
        )
        refreshed = await _ask(
            http,
            tenant,
            {
                "grant_type": "refresh_token",
                "client_id": d.bot_app_id,
                "client_secret": d.bot_app_secret,
                "refresh_token": first.json()["refresh_token"],
                "scope": "https://graph.microsoft.com/Files.ReadWrite.All offline_access",
            },
        )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"] != first.json()["access_token"]


async def test_a_refresh_token_naming_no_user_is_refused_as_not_served(tenant: Tenant, microsoft: Intercepted) -> None:
    """A refresh token is read only for the user it names; a string that names nobody cannot be answered with a
    user's token, so it is not served, by name (501), rather than refused as a credential."""
    d = tenant.directory
    async with microsoft.http() as http:
        refused = await _ask(
            http,
            tenant,
            {
                "grant_type": "refresh_token",
                "client_id": d.bot_app_id,
                "client_secret": d.bot_app_secret,
                "refresh_token": "made-up-by-the-caller",
                "scope": "https://graph.microsoft.com/Files.ReadWrite.All offline_access",
            },
        )
    assert refused.status_code == 501
    assert "names no user" in refused.text
