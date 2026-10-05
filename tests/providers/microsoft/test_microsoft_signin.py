"""Sign-in at `login.microsoftonline.com`, as the services sign in: client credentials for the connector and Graph,
and a user's authorization code and refresh token; each token verified against the published key set."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from jwt import PyJWKSet

from tests.providers.microsoft.tenant import GRAPH, LOGIN, Intercepted, Tenant, bearer, token

REDIRECT = "https://admin.example.com/oauth/microsoft/callback"
SCOPES = "openid profile email offline_access Files.ReadWrite.All Sites.Read.All"


async def _verified(http: httpx.AsyncClient, tenant: Tenant, presented: str, audience: str) -> dict[str, object]:
    metadata = (await http.get(f"{LOGIN}/{tenant.directory.tenant_id}/v2.0/.well-known/openid-configuration")).json()
    keys = PyJWKSet.from_dict((await http.get(metadata["jwks_uri"])).json())
    header = jwt.get_unverified_header(presented)
    key = next(k for k in keys.keys if k.key_id == header["kid"])
    return jwt.decode(presented, key.key, algorithms=["RS256"], audience=audience)


async def test_client_credentials_tokens_name_the_app_tenant_and_audience(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        graph = await token(http, tenant, "https://graph.microsoft.com/.default")
        claims = await _verified(http, tenant, graph, "https://graph.microsoft.com")
        assert (claims["tid"], claims["appid"]) == (tenant.directory.tenant_id, tenant.directory.bot_app_id)
        connector = await token(http, tenant, "https://api.botframework.com/.default")
        assert (await _verified(http, tenant, connector, "https://api.botframework.com"))[
            "appid"
        ] == tenant.directory.bot_app_id


async def _refusal(http: httpx.AsyncClient, tenant: Tenant, path: str, **form: str) -> tuple[int, str, str]:
    answered = await http.post(f"{LOGIN}/{path}/oauth2/v2.0/token", data=form)
    body = answered.json()
    return answered.status_code, body["error"], body["error_description"].split(":")[0]


async def test_a_wrong_secret_an_unknown_tenant_a_bad_scope_and_an_unknown_grant_are_refused(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    d = tenant.directory
    good = {"grant_type": "client_credentials", "client_id": d.bot_app_id, "client_secret": d.bot_app_secret}
    async with microsoft.http() as http:
        assert await _refusal(
            http,
            tenant,
            d.tenant_id,
            **{**good, "client_secret": "nope", "scope": "https://graph.microsoft.com/.default"},
        ) == (401, "invalid_client", "AADSTS7000215")
        assert await _refusal(
            http,
            tenant,
            "00000000-0000-0000-0000-000000000000",
            **{**good, "scope": "https://graph.microsoft.com/.default"},
        ) == (400, "invalid_request", "AADSTS90002")
        assert await _refusal(http, tenant, d.tenant_id, **{**good, "scope": "Files.Read"}) == (
            400,
            "invalid_scope",
            "AADSTS1002012",
        )
        assert await _refusal(http, tenant, d.tenant_id, **{**good, "grant_type": "password", "scope": "x"}) == (
            400,
            "unsupported_grant_type",
            "AADSTS70003",
        )
        assert await _refusal(
            http,
            tenant,
            d.tenant_id,
            **{**good, "client_id": "someone-else", "scope": "https://graph.microsoft.com/.default"},
        ) == (400, "unauthorized_client", "AADSTS700016")


async def test_a_user_signs_in_by_code_refreshes_and_reads_themself(tenant: Tenant, microsoft: Intercepted) -> None:
    d = tenant.directory
    async with microsoft.http() as http:
        authorized = await http.get(
            f"{LOGIN}/common/oauth2/v2.0/authorize",
            params={
                "client_id": d.bot_app_id,
                "response_type": "code",
                "redirect_uri": REDIRECT,
                "scope": SCOPES,
                "state": "s-1",
                "response_mode": "query",
                "prompt": "consent",
                "login_hint": "owen@example.com",
            },
        )
        assert authorized.status_code == 302
        back = parse_qs(urlsplit(authorized.headers["location"]).query)
        assert back["state"] == ["s-1"]
        exchanged = await http.post(
            f"{LOGIN}/common/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "client_id": d.bot_app_id,
                "client_secret": d.bot_app_secret,
                "code": back["code"][0],
                "redirect_uri": REDIRECT,
                "scope": SCOPES,
            },
        )
        assert exchanged.status_code == 200, exchanged.text
        answer = exchanged.json()
        assert {"access_token", "refresh_token", "id_token", "expires_in", "scope"} <= set(answer)
        identity = jwt.decode(answer["id_token"], options={"verify_signature": False})
        assert (identity["tid"], identity["email"], identity["name"]) == (
            d.tenant_id,
            "owen@example.com",
            "Owen Okafor",
        )
        me = (await http.get(f"{GRAPH}/me", headers=bearer(answer["access_token"]))).json()
        assert me["mail"] == "owen@example.com"
        refreshed = await http.post(
            f"{LOGIN}/{d.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "refresh_token",
                "client_id": d.bot_app_id,
                "client_secret": d.bot_app_secret,
                "refresh_token": answer["refresh_token"],
                "scope": "https://graph.microsoft.com/Files.ReadWrite.All offline_access",
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert (
            await http.get(f"{GRAPH}/me/drive", headers=bearer(refreshed.json()["access_token"]))
        ).status_code == 200


async def test_authorize_without_a_user_named_is_refused(tenant: Tenant, microsoft: Intercepted) -> None:
    async with microsoft.http() as http:
        answered = await http.get(
            f"{LOGIN}/common/oauth2/v2.0/authorize",
            params={"client_id": tenant.directory.bot_app_id, "redirect_uri": REDIRECT, "response_type": "code"},
        )
    assert answered.status_code == 400 and answered.json()["error"] == "invalid_request"


async def test_a_code_presented_twice_with_another_redirect_is_refused(tenant: Tenant, microsoft: Intercepted) -> None:
    d = tenant.directory
    async with microsoft.http() as http:
        authorized = await http.get(
            f"{LOGIN}/{d.tenant_id}/oauth2/v2.0/authorize",
            params={"client_id": d.bot_app_id, "redirect_uri": REDIRECT, "login_hint": "sofia@example.com"},
        )
        code = parse_qs(urlsplit(authorized.headers["location"]).query)["code"][0]
        answered = await http.post(
            f"{LOGIN}/{d.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "client_id": d.bot_app_id,
                "client_secret": d.bot_app_secret,
                "code": code,
                "redirect_uri": "https://elsewhere.example.com/cb",
            },
        )
    assert answered.status_code == 400 and answered.json()["error"] == "invalid_grant"
