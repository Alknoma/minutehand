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


async def _asked(http: httpx.AsyncClient, path: str, **form: str) -> httpx.Response:
    return await http.post(f"{LOGIN}/{path}/oauth2/v2.0/token", data=form)


async def test_any_secret_any_client_and_any_tenant_get_a_token(tenant: Tenant, microsoft: Intercepted) -> None:
    """Minutehand does not enforce credentials: a wrong secret, no secret, an app id no directory holds and a tenant
    the world does not hold are each answered with a token; the client id is its `appid` as sent."""
    d = tenant.directory
    graph = "https://graph.microsoft.com/.default"
    good = {"grant_type": "client_credentials", "client_id": d.bot_app_id, "scope": graph}
    async with microsoft.http() as http:
        wrong = await _asked(http, d.tenant_id, **{**good, "client_secret": "nope"})
        none = await _asked(http, d.tenant_id, **good)
        stranger = await _asked(http, d.tenant_id, **{**good, "client_id": "someone-else", "client_secret": "x"})
        elsewhere = await _asked(http, "00000000-0000-0000-0000-000000000000", **{**good, "client_secret": "x"})
        for answered in (wrong, none, stranger, elsewhere):
            assert answered.status_code == 200, answered.text
        claims = jwt.decode(stranger.json()["access_token"], options={"verify_signature": False})
        assert (claims["appid"], claims["tid"]) == ("someone-else", d.tenant_id)
        assert "roles" not in claims
        assert jwt.decode(elsewhere.json()["access_token"], options={"verify_signature": False})["tid"] == d.tenant_id


async def test_a_bad_scope_and_an_unknown_grant_are_refused(tenant: Tenant, microsoft: Intercepted) -> None:
    """The protocol's own refusals, which no credential decides: a client-credentials scope without `/.default`
    (AADSTS1002012) and a grant type the endpoint has none of (AADSTS70003)."""
    d = tenant.directory
    good = {"grant_type": "client_credentials", "client_id": d.bot_app_id, "client_secret": d.bot_app_secret}
    async with microsoft.http() as http:
        scope = await _asked(http, d.tenant_id, **{**good, "scope": "Files.Read"})
        grant = await _asked(http, d.tenant_id, **{**good, "grant_type": "password", "scope": "x"})
    assert (scope.status_code, scope.json()["error"]) == (400, "invalid_scope")
    assert scope.json()["error_description"].startswith("AADSTS1002012")
    assert (grant.status_code, grant.json()["error"]) == (400, "unsupported_grant_type")
    assert grant.json()["error_description"].startswith("AADSTS70003")


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


async def test_authorize_without_a_user_named_is_refused_as_not_served(tenant: Tenant, microsoft: Intercepted) -> None:
    """No browser asks who signs in, so authorize without `login_hint` is not served, by name (501)."""
    async with microsoft.http() as http:
        answered = await http.get(
            f"{LOGIN}/common/oauth2/v2.0/authorize",
            params={"client_id": tenant.directory.bot_app_id, "redirect_uri": REDIRECT, "response_type": "code"},
        )
    assert answered.status_code == 501
    assert "login_hint" in answered.text


async def test_a_code_presented_with_another_redirect_and_another_client_still_signs_the_user_in(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Minutehand does not enforce credentials: a code is read only for the user it names."""
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
                "client_id": "another-client",
                "client_secret": "wrong",
                "code": code,
                "redirect_uri": "https://elsewhere.example.com/cb",
            },
        )
        assert answered.status_code == 200, answered.text
        me = await http.get(f"{GRAPH}/me", headers=bearer(answered.json()["access_token"]))
    assert me.json()["mail"] == "sofia@example.com"
