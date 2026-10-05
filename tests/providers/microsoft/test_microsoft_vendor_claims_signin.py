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


async def test_a_wrong_client_secret_is_refused_invalid_client(tenant: Tenant, microsoft: Intercepted) -> None:
    """Documented: a secret that is not the app's is AADSTS7000215, error `invalid_client`, and no token. Class (a),
    ERROR_CODES. The HTTP status is not pinned here: the old emulator answered 400 where this provider answers 401
    (RFC 6749 section 5.2 allows 401 for `invalid_client`); `CLAIMS.md` records the disagreement."""
    async with microsoft.http() as http:
        answered = await _ask(http, tenant, _form(tenant, BOT_SCOPE, client_secret="not-this-apps-secret"))
    body = answered.json()
    assert answered.status_code in (400, 401)
    assert body["error"] == "invalid_client" and "access_token" not in body
    assert body["error_description"].startswith("AADSTS7000215")


async def test_an_app_the_directory_has_never_seen_is_refused_unauthorized_client_400(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Documented: an application id not found in the directory is AADSTS700016, `unauthorized_client`, a 400.
    Class (a), ERROR_CODES. Asserted for both scopes the old emulators each claimed it for."""
    async with microsoft.http() as http:
        for scope in (BOT_SCOPE, GRAPH_SCOPE):
            answered = await _ask(http, tenant, _form(tenant, scope, client_id="an-app-registered-nowhere"))
            assert answered.status_code == 400
            body = answered.json()
            assert body["error"] == "unauthorized_client"
            assert body["error_description"].startswith("AADSTS700016")


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


async def test_a_refresh_token_the_platform_never_issued_is_refused_invalid_grant(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Documented: a refresh token that is not valid is refused `invalid_grant`, a 400. Class (a), REFRESH. The old
    emulator issued a token for any string here; `CLAIMS.md` lists that as contradicted."""
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
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_grant" and "access_token" not in refused.json()
