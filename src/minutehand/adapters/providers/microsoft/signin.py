"""The identity platform (`login.microsoftonline.com`) and the Bot Framework's sign-in metadata
(`login.botframework.com`).

| Route | What it answers |
|---|---|
| `POST /{tenant}/oauth2/v2.0/token` | `client_credentials`, `authorization_code`, `refresh_token` |
| `GET /{tenant}/oauth2/v2.0/authorize` | A signed-in user consenting at once: 302 to `redirect_uri` with a `code` |
| `GET /{tenant}/v2.0/.well-known/openid-configuration` | The tenant's OpenID metadata |
| `GET /{tenant}/discovery/v2.0/keys` | The key set every token here is signed with |
| `GET /v1/.well-known/openidconfiguration` (botframework) | The metadata a bot validates pushed activities against |
| `GET /v1/.well-known/keys` (botframework) | The same key, endorsed for `msteams` |

`{tenant}` is a tenant id, its `onmicrosoft.com` domain, `organizations` or `common` (sign-in of a user only), or
`botframework.com`, the multi-tenant bot's authority, which knows every bot registered in the world.

**Authorize has no browser.** A real sign-in shows a page; this one answers at once for the user named in
`login_hint`, as though they signed in and consented, and does not serve a request without one (501).

**Minutehand does not enforce credentials.** Any client id and any secret are answered with a token: the client id
is the token's `appid` as sent, a tenant the world does not hold is the world's own, and a code or refresh token is
read only for the user it names (never its signature, lifetime, app or redirect URI). Tokens carry no `roles`, and no
scope is checked anywhere. What still refuses a sign-in is the world itself: a user an administrator disabled
(AADSTS50057) or removed (AADSTS50034).
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route, Router

from minutehand.adapters.providers.microsoft import keys, tokens, wire
from minutehand.adapters.providers.microsoft.connector import EVERY_METHOD, not_served
from minutehand.adapters.providers.microsoft.state import (
    MicrosoftWorld,
    TenantRecord,
    UserRecord,
    app_ref,
    user_ref,
)
from minutehand.adapters.providers.microsoft.wire import TokenUse
from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.world import Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

JSON = "application/json; charset=utf-8"
BOT_FRAMEWORK_AUTHORITY = "botframework.com"
USER_ONLY_AUTHORITIES = frozenset({"common", "organizations"})
BOT_FRAMEWORK_METADATA = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_FRAMEWORK_KEYS = "https://login.botframework.com/v1/.well-known/keys"

RESOURCES = {
    "https://graph.microsoft.com": tokens.GRAPH_AUDIENCE,
    tokens.GRAPH_APP_ID: tokens.GRAPH_AUDIENCE,
    "https://api.botframework.com": tokens.BOT_FRAMEWORK_AUDIENCE,
}
"""The resources a `.default` scope may name, and the audience a token for each carries."""
OPENID_SCOPES = frozenset({"openid", "profile", "email", "offline_access"})


class SignInRefused(ServiceRefusal):
    """The identity platform refused a sign-in, in its own token error shape."""

    def __init__(self, status: int, error: str, code: int, description: str) -> None:
        super().__init__(description)
        self.status = status
        self.error = error
        self.code = code
        self.description = description

    def render(self, asked: Asked) -> Rendered:
        """What `_refused` answers, stamped from the world's clock as `asked` carries it."""
        return Rendered(status=self.status, content_type=JSON, body=wire.dump(_token_error(self, asked.now)).encode())


def _refuse_disabled(user: UserRecord) -> None:
    """A user an administrator disabled cannot sign in, as Entra refuses them."""
    if user.user.accountEnabled is False:
        raise SignInRefused(400, "invalid_grant", 50057, "The user account is disabled.")


def _token_error(refusal: SignInRefused, now: datetime) -> wire.TokenError:
    return wire.TokenError(
        error=refusal.error,
        error_description=f"AADSTS{refusal.code}: {refusal.description}",
        error_codes=[refusal.code],
        timestamp=now.strftime("%Y-%m-%d %H:%M:%SZ"),
        trace_id=tokens.derived_trace(refusal.description),
        correlation_id=tokens.derived_trace(refusal.error),
    )


def _refused(refusal: SignInRefused, clock: Clock) -> Response:
    return Response(wire.dump(_token_error(refusal, clock.now())), status_code=refusal.status, media_type=JSON)


class SignIn:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = MicrosoftWorld(store)
        self._clock = clock

    # ------------------------------------------------------------------ lookups

    def _tenant(self, authority: str) -> TenantRecord | None:
        """The tenant a path names; None for an authority that is not one tenant (`common`, `botframework.com`). A
        tenant the world does not hold is the world's own: Minutehand does not enforce credentials."""
        if authority in USER_ONLY_AUTHORITIES or authority == BOT_FRAMEWORK_AUTHORITY:
            return None
        wanted = authority.lower()
        tenants = self._world.tenants()
        found = next((t for t in tenants if wanted in (t.id, t.domain.lower())), None)
        return found if found is not None else next(iter(tenants), None)

    def _app_id(self, client_id: str) -> str:
        """The app a token is issued to: the `client_id` as sent, whatever its secret; the world's own app when none
        is sent. Minutehand does not enforce credentials, so no client is refused."""
        if client_id:
            return client_id
        app = next(iter(self._world.apps()), None)
        return app.app_id if app is not None else ""

    @staticmethod
    def _resource(scope: str) -> str:
        """The audience of a client-credentials token: its one scope must be `<resource>/.default`."""
        names = scope.split()
        if len(names) != 1 or not names[0].endswith("/.default"):
            raise SignInRefused(
                400,
                "invalid_scope",
                1002012,
                f"The provided value for scope {scope} is not valid. Client credential flows must have a scope value "
                "with /.default suffixed to the resource identifier (application ID URI).",
            )
        resource = names[0].removesuffix("/.default")
        return RESOURCES[resource] if resource in RESOURCES else resource

    @staticmethod
    def _delegated(scope: str) -> tuple[str, str]:
        """A user's token: the audience of the resource its scopes name, and the scopes as `scp` carries them."""
        names = [s for s in scope.split() if s not in OPENID_SCOPES]
        audience = tokens.GRAPH_AUDIENCE
        granted: list[str] = []
        for name in names:
            resource, _, permission = name.rpartition("/")
            if resource:
                audience = RESOURCES[resource] if resource in RESOURCES else resource
            granted.append("User.Read" if permission == ".default" else permission)
        return audience, " ".join(granted or ["User.Read"])

    # ------------------------------------------------------------------ token

    async def token(self, request: Request) -> Response:
        try:
            authority = request.path_params["tenant"]
            tenant = self._tenant(authority)
            asked = wire.read_form(wire.TokenRequest, await request.body())
            if asked.grant_type == "client_credentials":
                answer = self._client_credentials(asked, tenant, authority)
            elif asked.grant_type == "authorization_code":
                answer = self._code(asked)
            elif asked.grant_type == "refresh_token":
                answer = self._refresh(asked)
            else:
                raise SignInRefused(
                    400,
                    "unsupported_grant_type",
                    70003,
                    f"The app requested an unsupported grant type '{asked.grant_type}'.",
                )
        except SignInRefused as refusal:
            return _refused(refusal, self._clock)
        return Response(wire.dump(answer), media_type=JSON, headers={"Cache-Control": "no-store, no-cache"})

    def _client_credentials(
        self, asked: wire.TokenRequest, tenant: TenantRecord | None, authority: str
    ) -> wire.TokenAnswer:
        if authority in USER_ONLY_AUTHORITIES:
            raise SignInRefused(
                400,
                "invalid_request",
                1002013,
                "The client credentials flow cannot use the /common or /organizations endpoint; name a tenant.",
            )
        app_id = self._app_id(asked.client_id)
        audience = self._resource(asked.scope)
        app = self._world.app(app_id)
        home = tenant.id if tenant is not None else app.tenant_id if app is not None else None
        issuer = (
            f"https://sts.windows.net/{tokens.BOT_FRAMEWORK_TENANT}/"
            if tenant is None
            else tokens.issuer_for(home or "")
        )
        token, _ = tokens.issued(
            use=TokenUse.ACCESS,
            issuer=issuer,
            audience=audience,
            tenant=home,
            app_id=app_id,
            user=None,
        )
        self._world.saw(app_ref(app_id), Operation.READ)
        return wire.TokenAnswer(
            scope=asked.scope,
            expires_in=tokens.LIFETIME_SECONDS,
            ext_expires_in=tokens.LIFETIME_SECONDS,
            access_token=token,
        )

    def _code(self, asked: wire.TokenRequest) -> wire.TokenAnswer:
        return self._for_user(self._app_id(asked.client_id), self._granted(asked.code, "authorization code"), asked)

    def _refresh(self, asked: wire.TokenRequest) -> wire.TokenAnswer:
        return self._for_user(self._app_id(asked.client_id), self._granted(asked.refresh_token, "refresh token"), asked)

    @staticmethod
    def _granted(presented: str | None, what: str) -> wire.Claims:
        """Whom a code or refresh token names. Nothing about it is checked (not its signature, its lifetime, its
        app or its redirect URI); one that names no user cannot be answered with that user's token."""
        granted = tokens.presented(presented)
        if granted is None or granted.oid is None or granted.tid is None:
            raise NotImplementedError(f"an {what} that names no user: Minutehand reads the user from the one it issued")
        return granted

    def _for_user(self, app_id: str, granted: wire.Claims, asked: wire.TokenRequest) -> wire.TokenAnswer:
        user = self._world.user(granted.oid or "")
        if user is None or granted.tid is None:
            raise SignInRefused(400, "invalid_grant", 50034, "The user account does not exist in the directory.")
        _refuse_disabled(user)
        audience, scopes = self._delegated(asked.scope or granted.scp or "")
        issuer = tokens.issuer_for(granted.tid)
        access, _ = tokens.issued(
            use=TokenUse.ACCESS,
            issuer=issuer,
            audience=audience,
            tenant=granted.tid,
            app_id=app_id,
            user=user.user.id,
            scopes=scopes,
        )
        refresh, _ = tokens.issued(
            use=TokenUse.REFRESH,
            issuer=issuer,
            audience=app_id,
            tenant=granted.tid,
            app_id=app_id,
            user=user.user.id,
            scopes=scopes,
            lifetime=tokens.REFRESH_LIFETIME_SECONDS,
        )
        identity, _ = tokens.issued(
            use=TokenUse.ACCESS,
            issuer=issuer,
            audience=app_id,
            tenant=granted.tid,
            app_id=app_id,
            user=user.user.id,
            nonce=granted.nonce,
            identity=user.user,
        )
        self._world.saw(user_ref(user.user.id), Operation.READ)
        return wire.TokenAnswer(
            scope=scopes,
            expires_in=tokens.LIFETIME_SECONDS,
            ext_expires_in=tokens.LIFETIME_SECONDS,
            access_token=access,
            refresh_token=refresh,
            id_token=identity,
        )

    # ------------------------------------------------------------------ authorize

    async def authorize(self, request: Request) -> Response:
        query = request.query_params
        client = query["client_id"] if "client_id" in query else ""
        redirect = query["redirect_uri"] if "redirect_uri" in query else ""
        hint = query["login_hint"] if "login_hint" in query else ""
        if not redirect or not hint:
            raise NotImplementedError(
                "authorize without login_hint and redirect_uri: this sign-in has no browser to ask who signs in"
            )
        user = self._world.user_by(hint)
        try:
            if user is None:
                raise SignInRefused(400, "invalid_grant", 50034, "The user account does not exist in the directory.")
            _refuse_disabled(user)
        except SignInRefused as refusal:
            return _refused(refusal, self._clock)
        app_id = self._app_id(client)
        code, _ = tokens.issued(
            use=TokenUse.CODE,
            issuer=tokens.issuer_for(user.tenant_id),
            audience=app_id,
            tenant=user.tenant_id,
            app_id=app_id,
            user=user.user.id,
            scopes=query["scope"] if "scope" in query else "User.Read",
            lifetime=600,
            nonce=query["nonce"] if "nonce" in query else None,
            redirect_uri=redirect,
        )
        answer = {"code": code, "session_state": tokens.derived_trace(code)}
        if "state" in query:
            answer["state"] = query["state"]
        joiner = "&" if "?" in redirect else "?"
        return RedirectResponse(f"{redirect}{joiner}{urlencode(answer)}", status_code=302)

    # ------------------------------------------------------------------ metadata

    async def openid_configuration(self, request: Request) -> Response:
        authority = request.path_params["tenant"]
        tenant = self._tenant(authority)
        named = tenant.id if tenant is not None else "{tenantid}"
        base = f"{tokens.AAD}/{authority}"
        answer = wire.OpenIdConfiguration(
            issuer=tokens.issuer_for(named),
            authorization_endpoint=f"{base}/oauth2/v2.0/authorize",
            token_endpoint=f"{base}/oauth2/v2.0/token",
            jwks_uri=f"{base}/discovery/v2.0/keys",
            id_token_signing_alg_values_supported=["RS256"],
            token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"],
            response_types_supported=["code", "id_token", "code id_token"],
            subject_types_supported=["pairwise"],
            scopes_supported=["openid", "profile", "email", "offline_access"],
            tenant_region_scope="EU",
        )
        return Response(wire.dump(answer), media_type=JSON)

    async def keys(self, request: Request) -> Response:
        return Response(wire.dump(key_set(endorsed=False)), media_type=JSON)


def key_set(*, endorsed: bool) -> wire.JwkSet:
    return wire.JwkSet(
        keys=[
            wire.Jwk(
                kid=keys.key_id(),
                n=keys.modulus(),
                e=keys.exponent(),
                endorsements=["msteams"] if endorsed else None,
            )
        ]
    )


async def bot_framework_metadata(request: Request) -> Response:
    answer = wire.OpenIdConfiguration(
        issuer=tokens.BOT_FRAMEWORK_ISSUER,
        authorization_endpoint="https://invalid.botframework.com",
        jwks_uri=BOT_FRAMEWORK_KEYS,
        id_token_signing_alg_values_supported=["RS256"],
        token_endpoint_auth_methods_supported=["private_key_jwt"],
    )
    return Response(wire.dump(answer), media_type=JSON)


async def bot_framework_keys(request: Request) -> Response:
    return Response(wire.dump(key_set(endorsed=True)), media_type=JSON)


def login_router(store: Store, clock: Clock) -> Router:
    sign_in = SignIn(store, clock)
    return Router(
        routes=[
            Route("/{tenant}/oauth2/v2.0/token", sign_in.token, methods=["POST"]),
            Route("/{tenant}/oauth2/v2.0/authorize", sign_in.authorize, methods=["GET"]),
            Route("/{tenant}/v2.0/.well-known/openid-configuration", sign_in.openid_configuration, methods=["GET"]),
            Route("/{tenant}/discovery/v2.0/keys", sign_in.keys, methods=["GET"]),
            Route("/discovery/v2.0/keys", sign_in.keys, methods=["GET"]),
            Route("/{rest:path}", not_served, methods=EVERY_METHOD),
        ]
    )


def bot_framework_router() -> Router:
    return Router(
        routes=[
            Route("/v1/.well-known/openidconfiguration", bot_framework_metadata, methods=["GET"]),
            Route("/v1/.well-known/keys", bot_framework_keys, methods=["GET"]),
            Route("/{rest:path}", not_served, methods=EVERY_METHOD),
        ]
    )
