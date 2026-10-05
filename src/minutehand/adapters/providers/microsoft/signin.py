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
`login_hint`, as though they signed in and consented, and refuses a request without one.
"""

from __future__ import annotations

from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route, Router

from minutehand.adapters.providers.microsoft import keys, tokens, wire
from minutehand.adapters.providers.microsoft.state import (
    AppRecord,
    MicrosoftWorld,
    TenantRecord,
    UserRecord,
    app_ref,
    user_ref,
)
from minutehand.adapters.providers.microsoft.wire import TokenUse
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
APPLICATION_ROLES = [
    "Sites.ReadWrite.All",
    "Files.ReadWrite.All",
    "User.Read.All",
    "ChannelMessage.Read.All",
    "Chat.Read.All",
]


class SignInRefused(Exception):
    def __init__(self, status: int, error: str, code: int, description: str) -> None:
        super().__init__(description)
        self.status = status
        self.error = error
        self.code = code
        self.description = description


def _refuse_disabled(user: UserRecord) -> None:
    """A user an administrator disabled cannot sign in, as Entra refuses them."""
    if user.user.accountEnabled is False:
        raise SignInRefused(400, "invalid_grant", 50057, "The user account is disabled.")


def _refused(refusal: SignInRefused, clock: Clock) -> Response:
    answer = wire.TokenError(
        error=refusal.error,
        error_description=f"AADSTS{refusal.code}: {refusal.description}",
        error_codes=[refusal.code],
        timestamp=clock.now().strftime("%Y-%m-%d %H:%M:%SZ"),
        trace_id=tokens.derived_trace(refusal.description),
        correlation_id=tokens.derived_trace(refusal.error),
    )
    return Response(wire.dump(answer), status_code=refusal.status, media_type=JSON)


class SignIn:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = MicrosoftWorld(store)
        self._clock = clock

    # ------------------------------------------------------------------ lookups

    def _tenant(self, authority: str) -> TenantRecord | None:
        """The tenant a path names; None for an authority that is not one tenant (`common`, `botframework.com`)."""
        if authority in USER_ONLY_AUTHORITIES or authority == BOT_FRAMEWORK_AUTHORITY:
            return None
        wanted = authority.lower()
        found = next((t for t in self._world.tenants() if wanted in (t.id, t.domain.lower())), None)
        if found is None:
            raise SignInRefused(
                400,
                "invalid_request",
                90002,
                f"Tenant '{authority}' not found. Check to make sure you have the correct tenant ID and are signing "
                "into the correct cloud.",
            )
        return found

    def _app(self, request: wire.TokenRequest, tenant: TenantRecord | None) -> AppRecord:
        app = self._world.app(request.client_id) if request.client_id else None
        if app is None:
            where = tenant.domain if tenant is not None else "the directory"
            raise SignInRefused(
                400,
                "unauthorized_client",
                700016,
                f"Application with identifier '{request.client_id}' was not found in the directory '{where}'.",
            )
        if request.client_secret != app.secret:
            raise SignInRefused(
                401,
                "invalid_client",
                7000215,
                "Invalid client secret provided. Ensure the secret being sent in the request is the client secret "
                f"value, not the client secret ID, for a secret added to app '{app.app_id}'.",
            )
        return app

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
        if resource not in RESOURCES:
            raise SignInRefused(
                400,
                "invalid_resource",
                500011,
                f"The resource principal named {resource} was not found in the tenant.",
            )
        return RESOURCES[resource]

    @staticmethod
    def _delegated(scope: str) -> tuple[str, str]:
        """A user's token: the audience of the resource its scopes name, and the scopes as `scp` carries them."""
        names = [s for s in scope.split() if s not in OPENID_SCOPES]
        audience = tokens.GRAPH_AUDIENCE
        granted: list[str] = []
        for name in names:
            resource, _, permission = name.rpartition("/")
            if resource and resource not in RESOURCES:
                raise SignInRefused(
                    400, "invalid_resource", 500011, f"The resource principal named {resource} was not found."
                )
            if resource:
                audience = RESOURCES[resource]
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
                answer = self._code(asked, tenant)
            elif asked.grant_type == "refresh_token":
                answer = self._refresh(asked, tenant)
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
        app = self._app(asked, tenant)
        audience = self._resource(asked.scope)
        if tenant is not None and app.tenant_id != tenant.id:
            raise SignInRefused(
                400,
                "unauthorized_client",
                700016,
                f"Application with identifier '{app.app_id}' was not found in the directory '{tenant.domain}'.",
            )
        tid = tokens.BOT_FRAMEWORK_TENANT if tenant is None else tenant.id
        issuer = f"https://sts.windows.net/{tokens.BOT_FRAMEWORK_TENANT}/" if tenant is None else tokens.issuer_for(tid)
        token, _ = tokens.issued(
            use=TokenUse.ACCESS,
            issuer=issuer,
            audience=audience,
            tenant=app.tenant_id if tenant is None else tid,
            app_id=app.app_id,
            user=None,
            roles=APPLICATION_ROLES,
        )
        self._world.saw(app_ref(app.app_id), Operation.READ)
        return wire.TokenAnswer(
            scope=asked.scope,
            expires_in=tokens.LIFETIME_SECONDS,
            ext_expires_in=tokens.LIFETIME_SECONDS,
            access_token=token,
        )

    def _code(self, asked: wire.TokenRequest, tenant: TenantRecord | None) -> wire.TokenAnswer:
        app = self._app(asked, tenant)
        try:
            granted = tokens.decode(asked.code or "", use=TokenUse.CODE)
        except tokens.TokenRefused as e:
            raise SignInRefused(
                400, "invalid_grant", 9002313, f"Invalid request. Request is malformed or invalid: {e.reason}."
            ) from e
        if granted.appid != app.app_id or granted.redirect_uri != asked.redirect_uri:
            raise SignInRefused(
                400,
                "invalid_grant",
                50148,
                "The code_verifier, client or redirect_uri does not match the one used in the authorization request.",
            )
        return self._for_user(app, granted, asked.scope or granted.scp or "")

    def _refresh(self, asked: wire.TokenRequest, tenant: TenantRecord | None) -> wire.TokenAnswer:
        app = self._app(asked, tenant)
        try:
            granted = tokens.decode(asked.refresh_token or "", use=TokenUse.REFRESH)
        except tokens.TokenRefused as e:
            raise SignInRefused(
                400, "invalid_grant", 9002313, f"Invalid request. Request is malformed or invalid: {e.reason}."
            ) from e
        if granted.appid != app.app_id:
            raise SignInRefused(
                400, "invalid_grant", 70000, "Provided grant is invalid or malformed: it was issued to another app."
            )
        return self._for_user(app, granted, asked.scope or granted.scp or "")

    def _for_user(self, app: AppRecord, granted: wire.Claims, scope: str) -> wire.TokenAnswer:
        user = self._world.user(granted.oid or "")
        if user is None or granted.tid is None:
            raise SignInRefused(400, "invalid_grant", 50034, "The user account does not exist in the directory.")
        _refuse_disabled(user)
        audience, scopes = self._delegated(scope)
        issuer = tokens.issuer_for(granted.tid)
        access, _ = tokens.issued(
            use=TokenUse.ACCESS,
            issuer=issuer,
            audience=audience,
            tenant=granted.tid,
            app_id=app.app_id,
            user=user.user.id,
            scopes=scopes,
        )
        refresh, _ = tokens.issued(
            use=TokenUse.REFRESH,
            issuer=issuer,
            audience=app.app_id,
            tenant=granted.tid,
            app_id=app.app_id,
            user=user.user.id,
            scopes=scopes,
            lifetime=tokens.REFRESH_LIFETIME_SECONDS,
        )
        identity, _ = tokens.issued(
            use=TokenUse.ACCESS,
            issuer=issuer,
            audience=app.app_id,
            tenant=granted.tid,
            app_id=app.app_id,
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
        try:
            tenant = self._tenant(request.path_params["tenant"])
            client = query["client_id"] if "client_id" in query else ""
            app = self._world.app(client)
            if app is None:
                raise SignInRefused(
                    400, "unauthorized_client", 700016, f"Application with identifier '{client}' was not found."
                )
            redirect = query["redirect_uri"] if "redirect_uri" in query else ""
            hint = query["login_hint"] if "login_hint" in query else ""
            user = self._world.user_by(hint) if hint else None
            if not redirect or user is None:
                raise SignInRefused(
                    400,
                    "invalid_request",
                    900144,
                    "This sign-in has no browser: name the user who signs in with login_hint, and a redirect_uri.",
                )
            if tenant is not None and user.tenant_id != tenant.id:
                raise SignInRefused(400, "invalid_request", 50020, f"User account '{hint}' does not exist in tenant.")
            _refuse_disabled(user)
        except SignInRefused as refusal:
            return _refused(refusal, self._clock)
        code, _ = tokens.issued(
            use=TokenUse.CODE,
            issuer=tokens.issuer_for(user.tenant_id),
            audience=app.app_id,
            tenant=user.tenant_id,
            app_id=app.app_id,
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
        try:
            tenant = self._tenant(authority)
        except SignInRefused as refusal:
            return _refused(refusal, self._clock)
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
        ]
    )


def bot_framework_router() -> Router:
    return Router(
        routes=[
            Route("/v1/.well-known/openidconfiguration", bot_framework_metadata, methods=["GET"]),
            Route("/v1/.well-known/keys", bot_framework_keys, methods=["GET"]),
        ]
    )
