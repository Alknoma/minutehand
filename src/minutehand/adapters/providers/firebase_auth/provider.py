"""Firebase Authentication: one project per world, its accounts, and the tokens its users sign in with.

The Admin SDK (`firebase_admin.auth`) and plain REST callers reach it at `identitytoolkit.googleapis.com/v1`, the
Secure Token API at `securetoken.googleapis.com/v1/token`, and `verify_id_token` fetches the certificates of the
key every ID token is signed with from `www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com`.

**Tokens.** An ID token is an RS256 JWT, its claims Firebase's (`wire.id_token_payload`), its times the world's
clock, signed with a key derived from a fixed seed: the same in every world, since the Admin SDK caches the
certificates it fetched for hours. A token is told apart from another world's by its `aud` and `iss`, the project.
A custom token is read by its claims (`uid`, `claims`, `aud`, `exp`) and its signature is not checked, as no
credential is (`docs/design.md`, "Authentication is out of scope"); whether it is signed by the project's service
account is IAM's business.

**What is checked** is the account's own data, which is the service's: a password signs in only its account, a
disabled account signs in nowhere, a refresh token issued before the account's `validSince` is expired, and an ID
token this provider did not sign, or one that has expired by the world's clock, is invalid. The API key and the
Admin SDK's bearer token are never checked: any, or none, acts.

What the world log holds is in `state.py`. Where each behaviour comes from is in `CLAIMS.md`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlencode

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Router
from starlette.types import Receive, Scope, Send

from minutehand.adapters.derived_key import DerivedKey
from minutehand.adapters.google_account import account_id
from minutehand.adapters.providers.firebase_auth import wire
from minutehand.adapters.providers.firebase_auth.manifest import MANIFEST
from minutehand.adapters.providers.firebase_auth.seed import FirebaseAuthSeed, SeededUser
from minutehand.adapters.providers.firebase_auth.state import Accounts, letters
from minutehand.domain.errors import NotServed, Rendered
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

KEY = DerivedKey("minutehand firebase securetoken signing key, version 1")
GOOGLE = "google.com"
PASSWORD = "password"
CUSTOM = "custom"
ANONYMOUS = "anonymous"
DISCOVERY_METHODS: dict[tuple[str, str], str] = {
    ("POST", "/v1/accounts:createAuthUri"): "accounts.createAuthUri",
    ("POST", "/v1/accounts:issueSamlResponse"): "accounts.issueSamlResponse",
    ("POST", "/v1/accounts:resetPassword"): "accounts.resetPassword",
    ("POST", "/v1/accounts:sendVerificationCode"): "accounts.sendVerificationCode",
    ("POST", "/v1/accounts:signInWithGameCenter"): "accounts.signInWithGameCenter",
    ("POST", "/v1/accounts:signInWithPhoneNumber"): "accounts.signInWithPhoneNumber",
    ("POST", "/v1/accounts:verifyIosClient"): "accounts.verifyIosClient",
    ("GET", "/v1/projects"): "getProjects",
    ("GET", "/v1/publicKeys"): "getPublicKeys",
    ("GET", "/v1/recaptchaParams"): "getRecaptchaParams",
    ("GET", "/v1/sessionCookiePublicKeys"): "getSessionCookiePublicKeys",
}
"""The methods of Identity Toolkit v1's discovery document with a fixed path that are not served; the project and
tenant methods are named by `_unserved_name`."""
SERVED = frozenset(
    {
        "accounts.signUp",
        "accounts.signInWithPassword",
        "accounts.signInWithCustomToken",
        "accounts.signInWithIdp",
        "accounts.signInWithEmailLink",
        "accounts.lookup",
        "accounts.update",
        "accounts.delete",
        "accounts.sendOobCode",
        "projects.accounts",
        "projects.accounts.lookup",
        "projects.accounts.update",
        "projects.accounts.delete",
        "projects.accounts.batchDelete",
        "projects.accounts.batchGet",
        "projects.accounts.sendOobCode",
    }
)
"""The 16 of the discovery document's 41 methods served."""


def key_id() -> str:
    """The `kid` of every ID token and the key the certificates list it under: 40 hexadecimal digits, as Google's."""
    public = KEY.private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha1(public).hexdigest()


def certificates() -> bytes:
    return json.dumps({key_id(): KEY.certificate(wire.SECURETOKEN_ACCOUNT)}).encode()


def _json(answer: wire.Model, status: int = 200) -> Response:
    return Response(wire.dump(answer), status_code=status, media_type=wire.JSON)


def _refused(refusal: wire.AuthRefusal) -> Response:
    return Response(wire.error_body(refusal.status, refusal.message), status_code=refusal.status, media_type=wire.JSON)


def _seconds(at: datetime) -> int:
    return int(at.timestamp())


def _salted(uid: str, password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}\x1f{uid}\x1f{password}".encode()).hexdigest()


def _checked_password(password: str) -> str:
    if len(password) < wire.MIN_PASSWORD:
        raise wire.AuthRefusal("WEAK_PASSWORD", f"Password should be at least {wire.MIN_PASSWORD} characters")
    return password


def _checked_email(email: str) -> str:
    local, at, domain = email.partition("@")
    if not at or not local or not domain or " " in email:
        raise wire.AuthRefusal("INVALID_EMAIL")
    return email


class FirebaseAuthProvider:
    manifest: Manifest = MANIFEST
    seed_model = FirebaseAuthSeed

    def error(self, status: int, code: str, message: str) -> Rendered:
        return Rendered(status=status, content_type=wire.JSON, body=wire.unimplemented_body(status, message))

    def seed(self, scenario: Scenario, world: Store) -> None:
        """The project `FirebaseAuthSeed` names and its accounts. A `SignIn` on this provider is refused: a Firebase
        user signs in with what the provider's own seed gives them (a password, a Google account)."""
        if any(s.provider == MANIFEST.key for s in scenario.sign_ins):
            raise ValueError(
                f"a seeded sign_in is on {MANIFEST.key}: a Firebase user signs in with a password or a linked Google "
                "account, given in FirebaseAuthSeed.users"
            )
        given = scenario.provider_seed(MANIFEST.key)
        if given is None:
            return
        own = FirebaseAuthSeed.model_validate_json(given.body)
        accounts = Accounts(world)
        accounts.keep_project(
            wire.Project(
                project_id=own.project_id,
                api_key=own.api_key,
                email_enumeration_protection=own.email_enumeration_protection,
            )
        )
        people: dict[str, Person] = {p.key: p for p in scenario.people}
        taken: set[str] = set()
        for position, seeded in enumerate(own.users):
            user = _seeded_user(own.project_id, seeded, people, scenario.starts_at, position)
            if user.localId in taken:
                raise ValueError(f"two seeded Firebase users are {user.localId}: give each a uid of its own")
            taken.add(user.localId)
            accounts.put_user(own.project_id, user, actor=Actor.SCENARIO, operation=Operation.CREATE)

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return _app(_Api(Accounts(world), world, clock))


def _seeded_user(
    project: str, seeded: SeededUser, people: dict[str, Person], starts_at: datetime, position: int
) -> wire.StoredUser:
    if seeded.person is not None and seeded.person not in people:
        raise ValueError(f"a seeded Firebase user names {seeded.person}, who is not a person of the scenario")
    person = people[seeded.person] if seeded.person is not None else None
    email = seeded.email or (person.email if person is not None else None)
    name = seeded.display_name or (person.name if person is not None else None)
    identity = seeded.person or email or name or f"#{position}"
    uid = seeded.uid or letters("seeded", project, identity, length=28)
    links: list[wire.Link] = []
    salt: str | None = None
    digest: str | None = None
    if seeded.password is not None:
        if email is None:
            raise ValueError(f"the seeded Firebase user {uid} has a password and no address to sign in with")
        links.append(wire.Link(providerId=PASSWORD, rawId=email, federatedId=email, email=email))
        salt = letters("salt", project, uid, length=16)
        digest = _salted(uid, seeded.password, salt)
    if seeded.google is not None:
        linked = people[seeded.google] if seeded.google in people else None
        if linked is None or linked.email is None:
            raise ValueError(
                f"the seeded Firebase user {uid} links the Google account of {seeded.google}, who has no address"
            )
        sub = account_id(linked.email)
        links.append(
            wire.Link(providerId=GOOGLE, rawId=sub, federatedId=sub, email=linked.email, displayName=linked.name)
        )
    created = starts_at - seeded.created_before_start
    return wire.StoredUser(
        localId=uid,
        email=email,
        emailVerified=seeded.email_verified,
        displayName=name,
        photoUrl=seeded.photo_url,
        password_salt=salt,
        password_digest=digest,
        passwordUpdatedAt=wire.millis(created) if seeded.password is not None else None,
        disabled=seeded.disabled,
        customAttributes=json.dumps(seeded.custom_claims) if seeded.custom_claims is not None else None,
        createdAt=wire.millis(created),
        links=links,
    )


Handler = Callable[[Request], Awaitable[Response]]


class _Api:
    """Identity Toolkit's and the Secure Token API's operations over the world."""

    def __init__(self, accounts: Accounts, world: Store, clock: Clock) -> None:
        self._accounts = accounts
        self._world = world
        self._clock = clock

    # -- the project ---------------------------------------------------------------------------------------------

    def project(self, request: Request) -> wire.Project:
        """The world's project; a path naming another is refused by name, since which of Google's refusals a project
        the caller has no access to answers is not documented."""
        held = self._accounts.project()
        if held is None:
            raise NotServed(
                "a world that seeds no Firebase project: give firebase_auth a provider seed naming project_id and "
                "api_key"
            )
        named = request.path_params["project"] if "project" in request.path_params else None
        if named is not None and named != held.project_id:
            raise NotServed(f"project {named}: this world is the Firebase project {held.project_id}")
        return held

    def _now(self) -> datetime:
        return self._clock.now()

    # -- tokens --------------------------------------------------------------------------------------------------

    def _signed_in(
        self, project: wire.Project, user: wire.StoredUser, provider: str, *, claims: str | None = None
    ) -> tuple[str, str]:
        """An ID token and a refresh token for `user`, signed in now by `provider`; the account's last sign-in and
        refresh are now."""
        now = self._now()
        auth_time = _seconds(now)
        refresh = "AMf-vB" + letters("refresh", project.project_id, user.localId, str(self._world.head()), length=180)
        self._accounts.keep_refresh(
            refresh,
            wire.StoredRefresh(uid=user.localId, auth_time=auth_time, provider=provider, claims=claims),
            actor=Actor.AGENT,
        )
        signed = user.model_copy(update={"lastLoginAt": wire.millis(now), "lastRefreshAt": now})
        self._accounts.put_user(project.project_id, signed, actor=Actor.AGENT, operation=Operation.UPDATE)
        return self._id_token(project, signed, auth_time, provider, claims), refresh

    def _id_token(
        self, project: wire.Project, user: wire.StoredUser, auth_time: int, provider: str, claims: str | None
    ) -> str:
        payload = wire.id_token_payload(
            project=project.project_id,
            user=user,
            auth_time=auth_time,
            issued=_seconds(self._now()),
            provider=provider,
            developer_claims=claims,
        )
        signing_input = f"{wire.b64url(wire.jwt_header(key_id()))}.{wire.b64url(payload)}"
        return f"{signing_input}.{wire.b64url(KEY.sign(signing_input.encode()))}"

    def _holder(self, project: wire.Project, token: str) -> wire.StoredUser:
        """The account an ID token of this world names: one this provider signed, for this project, not expired by
        the world's clock, whose account is still there."""
        parts = wire.jwt_parts(token, wire.IdTokenClaims)
        if parts is None:
            raise wire.AuthRefusal("INVALID_ID_TOKEN")
        header, claims, signing_input, signature = parts
        public = KEY.private_key.public_key()
        try:
            public.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as e:
            raise wire.AuthRefusal("INVALID_ID_TOKEN") from e
        if header.kid != key_id() or claims.aud != project.project_id:
            raise wire.AuthRefusal("INVALID_ID_TOKEN")
        if claims.iss != wire.ISSUER_PREFIX + project.project_id or claims.exp <= _seconds(self._now()):
            raise wire.AuthRefusal("INVALID_ID_TOKEN")
        user = self._accounts.user(project.project_id, claims.sub)
        if user is None:
            raise wire.AuthRefusal("USER_NOT_FOUND")
        return user

    # -- creating ------------------------------------------------------------------------------------------------

    def _new_uid(self, project: wire.Project) -> str:
        return letters("created", project.project_id, str(self._world.head()), length=28)

    def _create(self, project: wire.Project, asked: wire.SignUpIn, *, anonymous_allowed: bool) -> wire.StoredUser:
        if asked.email is not None:
            _checked_email(asked.email)
        if asked.email is not None and asked.password is None and anonymous_allowed:
            raise wire.AuthRefusal("MISSING_PASSWORD")
        if asked.password is not None:
            _checked_password(asked.password)
        uid = asked.localId or self._new_uid(project)
        if asked.localId is not None and self._accounts.user(project.project_id, asked.localId) is not None:
            raise wire.AuthRefusal("DUPLICATE_LOCAL_ID")
        if asked.email is not None and self._accounts.by_email(project.project_id, asked.email) is not None:
            raise wire.AuthRefusal("EMAIL_EXISTS")
        now = self._now()
        links: list[wire.Link] = []
        salt = digest = None
        if asked.password is not None:
            if asked.email is None:
                raise wire.AuthRefusal("MISSING_EMAIL")
            links.append(wire.Link(providerId=PASSWORD, rawId=asked.email, federatedId=asked.email, email=asked.email))
            salt = letters("salt", project.project_id, uid, str(self._world.head()), length=16)
            digest = _salted(uid, asked.password, salt)
        user = wire.StoredUser(
            localId=uid,
            email=asked.email,
            emailVerified=bool(asked.emailVerified),
            displayName=asked.displayName,
            photoUrl=asked.photoUrl,
            password_salt=salt,
            password_digest=digest,
            passwordUpdatedAt=wire.millis(now) if asked.password is not None else None,
            disabled=bool(asked.disabled),
            createdAt=wire.millis(now),
        ).model_copy(update={"links": links})
        self._accounts.put_user(project.project_id, user, actor=Actor.AGENT, operation=Operation.CREATE)
        return user

    async def sign_up(self, request: Request) -> Response:
        """accounts.signUp: with an address and a password, a password account; with neither, an anonymous one."""
        project = self.project(request)
        asked = wire.read(wire.SignUpIn, await request.body(), "accounts.signUp")
        user = self._create(project, asked, anonymous_allowed=True)
        provider = PASSWORD if asked.email is not None else ANONYMOUS
        id_token, refresh = self._signed_in(project, user, provider)
        return _json(
            wire.SignUpOut(
                idToken=id_token,
                refreshToken=refresh,
                localId=user.localId,
                email=user.email,
                displayName=user.displayName,
            )
        )

    async def admin_create(self, request: Request) -> Response:
        """projects.accounts: the Admin SDK's `create_user`."""
        project = self.project(request)
        asked = wire.read(wire.SignUpIn, await request.body(), "projects.accounts")
        user = self._create(project, asked, anonymous_allowed=False)
        return _json(wire.AdminCreated(localId=user.localId, email=user.email, displayName=user.displayName))

    # -- signing in ----------------------------------------------------------------------------------------------

    async def sign_in_with_password(self, request: Request) -> Response:
        project = self.project(request)
        asked = wire.read(wire.PasswordIn, await request.body(), "accounts.signInWithPassword")
        if asked.email is None:
            raise wire.AuthRefusal("INVALID_EMAIL")
        if asked.password is None:
            raise wire.AuthRefusal("MISSING_PASSWORD")
        user = self._accounts.by_email(project.project_id, asked.email)
        guarded = project.email_enumeration_protection
        if user is None:
            raise wire.AuthRefusal("INVALID_LOGIN_CREDENTIALS" if guarded else "EMAIL_NOT_FOUND")
        if (
            user.password_digest is None
            or user.password_salt is None
            or _salted(user.localId, asked.password, user.password_salt) != user.password_digest
        ):
            raise wire.AuthRefusal("INVALID_LOGIN_CREDENTIALS" if guarded else "INVALID_PASSWORD")
        if user.disabled:
            raise wire.AuthRefusal("USER_DISABLED")
        id_token, refresh = self._signed_in(project, user, PASSWORD)
        return _json(
            wire.PasswordOut(
                idToken=id_token,
                refreshToken=refresh,
                localId=user.localId,
                email=user.email or asked.email,
                displayName=user.displayName or "",
            )
        )

    async def sign_in_with_custom_token(self, request: Request) -> Response:
        project = self.project(request)
        asked = wire.read(wire.CustomTokenIn, await request.body(), "accounts.signInWithCustomToken")
        if not asked.token:
            raise wire.AuthRefusal("MISSING_CUSTOM_TOKEN")
        parts = wire.jwt_parts(asked.token, wire.CustomTokenClaims)
        if parts is None:
            raise wire.AuthRefusal("INVALID_CUSTOM_TOKEN")
        _, claims, _, _ = parts
        now = _seconds(self._now())
        if (
            claims.aud != wire.FIREBASE_AUDIENCE
            or not claims.uid
            or len(claims.uid) > 128
            or claims.exp is None
            or claims.exp <= now
        ):
            raise wire.AuthRefusal("INVALID_CUSTOM_TOKEN")
        if claims.tenant_id is not None:
            raise NotServed("signInWithCustomToken for a tenant (tenant_id): multi-tenancy is not served")
        user = self._accounts.user(project.project_id, claims.uid)
        new = user is None
        if user is None:
            user = wire.StoredUser(localId=claims.uid, createdAt=wire.millis(self._now()))
            self._accounts.put_user(project.project_id, user, actor=Actor.AGENT, operation=Operation.CREATE)
        if user.disabled:
            raise wire.AuthRefusal("USER_DISABLED")
        developer = json.dumps(claims.claims) if claims.claims else None
        id_token, refresh = self._signed_in(project, user, CUSTOM, claims=developer)
        return _json(wire.CustomTokenOut(idToken=id_token, refreshToken=refresh, isNewUser=new))

    async def sign_in_with_idp(self, request: Request) -> Response:
        """accounts.signInWithIdp with a Google ID token (`postBody` `id_token=...&providerId=google.com`). An
        access token alone is refused by name: Firebase reads whose it is from Google, which this fake cannot ask."""
        project = self.project(request)
        asked = wire.read(wire.IdpIn, await request.body(), "accounts.signInWithIdp")
        if asked.postBody is None or asked.requestUri is None:
            raise wire.AuthRefusal("INVALID_IDP_RESPONSE" if asked.requestUri is not None else "MISSING_REQUEST_URI")
        provider = wire.form_value(asked.postBody, "providerId")
        if provider != GOOGLE:
            raise NotServed(f"signInWithIdp with the provider {provider}: only {GOOGLE} is served")
        token = wire.form_value(asked.postBody, "id_token")
        if token is None:
            if wire.form_value(asked.postBody, "access_token") is not None:
                raise NotServed(
                    "signInWithIdp with a Google access_token and no id_token: Firebase asks Google whose token it "
                    "is, and this fake reads only the ID token's own claims"
                )
            raise wire.AuthRefusal("INVALID_IDP_RESPONSE")
        parts = wire.jwt_parts(token, wire.GoogleIdClaims)
        if parts is None:
            raise wire.AuthRefusal("INVALID_IDP_RESPONSE")
        _, google, _, _ = parts
        if not google.sub or google.iss not in ("https://accounts.google.com", "accounts.google.com"):
            raise wire.AuthRefusal("INVALID_IDP_RESPONSE")
        if google.exp is not None and google.exp <= _seconds(self._now()):
            raise wire.AuthRefusal("INVALID_IDP_RESPONSE")
        user = self._accounts.by_link(project.project_id, GOOGLE, google.sub)
        new = user is None
        if user is None:
            if google.email is not None and self._accounts.by_email(project.project_id, google.email) is not None:
                raise NotServed(
                    "signing in with a Google account whose address another account of the project holds: what "
                    "Firebase does then depends on the project's account-linking setting and on that account"
                )
            now = self._now()
            user = wire.StoredUser(
                localId=self._new_uid(project),
                email=google.email,
                emailVerified=bool(google.email_verified),
                displayName=google.name,
                photoUrl=google.picture,
                createdAt=wire.millis(now),
                links=[
                    wire.Link(
                        providerId=GOOGLE,
                        rawId=google.sub,
                        federatedId=google.sub,
                        email=google.email,
                        displayName=google.name,
                        photoUrl=google.picture,
                    )
                ],
            )
            self._accounts.put_user(project.project_id, user, actor=Actor.AGENT, operation=Operation.CREATE)
        if user.disabled:
            raise wire.AuthRefusal("USER_DISABLED")
        id_token, refresh = self._signed_in(project, user, GOOGLE)
        return _json(
            wire.IdpOut(
                idToken=id_token,
                refreshToken=refresh,
                localId=user.localId,
                federatedId=f"https://accounts.google.com/{google.sub}",
                providerId=GOOGLE,
                email=user.email,
                emailVerified=user.emailVerified,
                displayName=user.displayName,
                fullName=google.name,
                firstName=google.given_name,
                lastName=google.family_name,
                photoUrl=user.photoUrl,
                oauthIdToken=token if asked.returnIdpCredential else None,
                rawUserInfo=wire.raw_user_info(google),
                isNewUser=True if new else None,
            )
        )

    async def sign_in_with_email_link(self, request: Request) -> Response:
        project = self.project(request)
        asked = wire.read(wire.EmailLinkIn, await request.body(), "accounts.signInWithEmailLink")
        if not asked.oobCode:
            raise wire.AuthRefusal("MISSING_OOB_CODE")
        if not asked.email:
            raise wire.AuthRefusal("MISSING_EMAIL")
        held = self._accounts.code(asked.oobCode)
        if held is None or held.used or held.request_type != "EMAIL_SIGNIN":
            raise wire.AuthRefusal("INVALID_OOB_CODE")
        if held.email.lower() != asked.email.lower():
            raise wire.AuthRefusal("INVALID_EMAIL")
        self._accounts.keep_code(asked.oobCode, held.model_copy(update={"used": True}), operation=Operation.UPDATE)
        user = self._accounts.by_email(project.project_id, asked.email)
        new = user is None
        if user is None:
            user = wire.StoredUser(
                localId=self._new_uid(project),
                email=asked.email,
                emailVerified=True,
                createdAt=wire.millis(self._now()),
                links=[wire.Link(providerId=PASSWORD, rawId=asked.email, federatedId=asked.email, email=asked.email)],
            )
            self._accounts.put_user(project.project_id, user, actor=Actor.AGENT, operation=Operation.CREATE)
        elif not user.emailVerified:
            user = user.model_copy(update={"emailVerified": True})
            self._accounts.put_user(project.project_id, user, actor=Actor.AGENT, operation=Operation.UPDATE)
        if user.disabled:
            raise wire.AuthRefusal("USER_DISABLED")
        id_token, refresh = self._signed_in(project, user, PASSWORD)
        return _json(
            wire.EmailLinkOut(
                idToken=id_token, refreshToken=refresh, localId=user.localId, email=asked.email, isNewUser=new
            )
        )

    async def refresh(self, request: Request) -> Response:
        """securetoken.googleapis.com/v1/token: an ID token for a refresh token, in the token endpoint's own
        snake_case answer."""
        try:
            project = self.project(request)
            raw = await request.body()
            form = (
                dict(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True))
                if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")
                else {}
            )
            asked = wire.read_refresh(form, raw)
            if asked.grant_type != "refresh_token":
                raise wire.AuthRefusal("INVALID_GRANT_TYPE")
            if not asked.refresh_token:
                raise wire.AuthRefusal("MISSING_REFRESH_TOKEN")
            held = self._accounts.refresh(asked.refresh_token)
            if held is None:
                raise wire.AuthRefusal("INVALID_REFRESH_TOKEN")
            user = self._accounts.user(project.project_id, held.uid)
            if user is None:
                raise wire.AuthRefusal("USER_NOT_FOUND")
            if user.disabled:
                raise wire.AuthRefusal("USER_DISABLED")
            if user.validSince is not None and held.auth_time < user.validSince:
                raise wire.AuthRefusal("TOKEN_EXPIRED")
        except wire.AuthRefusal as refusal:
            return _refused(refusal)
        now = self._now()
        refreshed = user.model_copy(update={"lastRefreshAt": now})
        self._accounts.put_user(project.project_id, refreshed, actor=Actor.AGENT, operation=Operation.UPDATE)
        id_token = self._id_token(project, refreshed, held.auth_time, held.provider, held.claims)
        return _json(
            wire.RefreshOut(
                access_token=id_token,
                refresh_token=asked.refresh_token,
                id_token=id_token,
                user_id=user.localId,
                project_id=project.project_id,
            )
        )

    # -- reading and changing accounts ---------------------------------------------------------------------------

    async def lookup(self, request: Request) -> Response:
        project = self.project(request)
        asked = wire.read(wire.LookupIn, await request.body(), "accounts.lookup")
        found: list[wire.StoredUser] = []
        if asked.idToken is not None:
            found.append(self._holder(project, asked.idToken))
        for uid in asked.localId:
            user = self._accounts.user(project.project_id, uid)
            if user is not None:
                found.append(user)
        for email in asked.email:
            user = self._accounts.by_email(project.project_id, email)
            if user is not None:
                found.append(user)
        for federated in asked.federatedUserId:
            user = self._accounts.by_link(project.project_id, federated.providerId, federated.rawId)
            if user is not None:
                found.append(user)
        unique = list({u.localId: u for u in found}.values())
        return _json(wire.LookupOut(users=[wire.user_info(u) for u in unique] or None))

    async def update(self, request: Request) -> Response:
        project = self.project(request)
        asked = wire.read(wire.UpdateIn, await request.body(), "accounts.update")
        if asked.idToken is not None:
            if asked.email is not None or asked.password is not None:
                raise NotServed(
                    "accounts.update of an address or a password with an idToken: Firebase answers new tokens for it"
                )
            user = self._holder(project, asked.idToken)
        elif asked.localId is not None:
            held = self._accounts.user(project.project_id, asked.localId)
            if held is None:
                raise wire.AuthRefusal("USER_NOT_FOUND")
            user = held
        else:
            raise wire.AuthRefusal("MISSING_LOCAL_ID")
        changed = user
        now = self._now()
        if asked.email is not None and (user.email or "").lower() != asked.email.lower():
            _checked_email(asked.email)
            if self._accounts.by_email(project.project_id, asked.email) is not None:
                raise wire.AuthRefusal("EMAIL_EXISTS")
            links = [
                link.model_copy(update={"rawId": asked.email, "federatedId": asked.email, "email": asked.email})
                if link.providerId == PASSWORD
                else link
                for link in user.links
            ]
            changed = changed.model_copy(update={"email": asked.email, "emailVerified": False, "links": links})
        if asked.password is not None:
            _checked_password(asked.password)
            email = changed.email
            if email is None:
                raise NotServed("a password for an account with no address: it has nothing to sign in with")
            salt = letters("salt", project.project_id, user.localId, str(self._world.head()), length=16)
            links = [k for k in changed.links if k.providerId != PASSWORD] + [
                wire.Link(providerId=PASSWORD, rawId=email, federatedId=email, email=email)
            ]
            changed = changed.model_copy(
                update={
                    "password_salt": salt,
                    "password_digest": _salted(user.localId, asked.password, salt),
                    "passwordUpdatedAt": wire.millis(now),
                    "links": links,
                }
            )
        updates: dict[str, object] = {}
        if asked.displayName is not None:
            updates["displayName"] = asked.displayName
        if asked.photoUrl is not None:
            updates["photoUrl"] = asked.photoUrl
        if asked.emailVerified is not None:
            updates["emailVerified"] = asked.emailVerified
        if asked.disableUser is not None:
            updates["disabled"] = asked.disableUser
        if asked.validSince is not None:
            updates["validSince"] = int(asked.validSince)
        if asked.customAttributes is not None:
            updates["customAttributes"] = wire.custom_attributes(asked.customAttributes)
        if "DISPLAY_NAME" in asked.deleteAttribute:
            updates["displayName"] = None
        if "PHOTO_URL" in asked.deleteAttribute:
            updates["photoUrl"] = None
        if asked.deleteProvider:
            updates["links"] = [k for k in changed.links if k.providerId not in asked.deleteProvider]
        changed = changed.model_copy(update=updates)
        self._accounts.put_user(project.project_id, changed, actor=Actor.AGENT, operation=Operation.UPDATE)
        return _json(
            wire.UpdateOut(
                localId=changed.localId,
                email=changed.email,
                displayName=changed.displayName,
                photoUrl=changed.photoUrl,
                emailVerified=changed.emailVerified,
                providerUserInfo=changed.links,
            )
        )

    async def delete(self, request: Request) -> Response:
        project = self.project(request)
        asked = wire.read(wire.DeleteIn, await request.body(), "accounts.delete")
        if asked.idToken is not None:
            user = self._holder(project, asked.idToken)
        elif asked.localId is not None:
            held = self._accounts.user(project.project_id, asked.localId)
            if held is None:
                raise wire.AuthRefusal("USER_NOT_FOUND")
            user = held
        else:
            raise wire.AuthRefusal("MISSING_LOCAL_ID")
        self._accounts.delete_user(project.project_id, user, actor=Actor.AGENT)
        return _json(wire.DeleteOut())

    async def batch_delete(self, request: Request) -> Response:
        """projects.accounts.batchDelete: "If false, only disabled accounts will be deleted, and accounts that are
        not disabled will be added to the `errors`" (force); an id the project does not hold deletes nothing and is
        no error (the Admin SDK's `delete_users`: "Deleting a non-existing user won't generate an error")."""
        project = self.project(request)
        asked = wire.read(wire.BatchDeleteIn, await request.body(), "projects.accounts.batchDelete")
        if not asked.localIds:
            raise wire.AuthRefusal("MISSING_LOCAL_ID")
        errors: list[wire.BatchDeleteError] = []
        for index, uid in enumerate(asked.localIds):
            user = self._accounts.user(project.project_id, uid)
            if user is None:
                continue
            if not asked.force and not user.disabled:
                errors.append(
                    wire.BatchDeleteError(
                        index=index,
                        localId=uid,
                        message="NOT_DISABLED : Disabled user accounts are required for deletion.",
                    )
                )
                continue
            self._accounts.delete_user(project.project_id, user, actor=Actor.AGENT)
        return _json(wire.BatchDeleteOut(errors=errors or None))

    async def batch_get(self, request: Request) -> Response:
        project = self.project(request)
        query = request.query_params
        known = {"maxResults", "nextPageToken", "key", "alt", "prettyPrint"}
        unserved = sorted(set(query.keys()) - known)
        if unserved:
            raise NotServed(f"projects.accounts.batchGet with {', '.join(unserved)}")
        spelled = query.get("maxResults")
        size = int(spelled) if spelled is not None and spelled.isdigit() else wire.DEFAULT_LIST
        if spelled is not None and (not spelled.isdigit() or not 1 <= size <= wire.MAX_LIST):
            raise NotServed(f"projects.accounts.batchGet with maxResults {spelled}, outside 1 to {wire.MAX_LIST}")
        token = query.get("nextPageToken")
        after = base64.urlsafe_b64decode(token.encode()).decode() if token else ""
        ordered = sorted(self._accounts.users(project.project_id), key=lambda u: u.localId)
        rest = [u for u in ordered if u.localId > after]
        page = rest[:size]
        following = base64.urlsafe_b64encode(page[-1].localId.encode()).decode() if len(rest) > size else None
        return _json(wire.DownloadOut(users=[wire.user_info(u) for u in page] or None, nextPageToken=following))

    async def send_oob_code(self, request: Request) -> Response:
        """accounts.sendOobCode with `returnOobLink`, as the Admin SDK's `generate_*_link` asks for it: the link,
        on the project's default action handler (https://firebase.google.com/docs/auth/custom-email-handler). Without
        it Firebase sends the email itself, which this fake does not: refused by name."""
        project = self.project(request)
        asked = wire.read(wire.OobIn, await request.body(), "accounts.sendOobCode")
        if not asked.returnOobLink:
            raise NotServed(
                "accounts.sendOobCode without returnOobLink: Firebase emails the link, and this fake sends no email"
            )
        if asked.requestType is None:
            raise wire.AuthRefusal("MISSING_REQ_TYPE")
        if not asked.email:
            raise wire.AuthRefusal("MISSING_EMAIL")
        if asked.dynamicLinkDomain is not None:
            raise NotServed("accounts.sendOobCode with dynamicLinkDomain: Firebase Dynamic Links are not served")
        if asked.requestType == "EMAIL_SIGNIN" and not asked.continueUrl:
            raise wire.AuthRefusal("MISSING_CONTINUE_URI")
        if asked.requestType != "EMAIL_SIGNIN" and self._accounts.by_email(project.project_id, asked.email) is None:
            raise wire.AuthRefusal("EMAIL_NOT_FOUND")
        code = letters("oob", project.project_id, asked.email, str(self._world.head()), length=54)
        self._accounts.keep_code(
            code, wire.StoredCode(email=asked.email, request_type=asked.requestType), operation=Operation.CREATE
        )
        mode = {"EMAIL_SIGNIN": "signIn", "PASSWORD_RESET": "resetPassword", "VERIFY_EMAIL": "verifyEmail"}[
            asked.requestType
        ]
        host = asked.linkDomain or f"{project.project_id}.firebaseapp.com"
        query = {"apiKey": project.api_key, "mode": mode, "oobCode": code}
        if asked.continueUrl:
            query["continueUrl"] = asked.continueUrl
        query["lang"] = "en"
        link = f"https://{host}/__/auth/action?{urlencode(query, quote_via=quote)}"
        return _json(wire.OobOut(email=asked.email, oobLink=link))

    # -- what is not served --------------------------------------------------------------------------------------

    async def unserved(self, request: Request) -> Response:
        raise NotServed(_unserved_name(request.method, request.url.path))


def _unserved_name(method: str, path: str) -> str:
    if (method, path) in DISCOVERY_METHODS:
        return f"identitytoolkit.{DISCOVERY_METHODS[(method, path)]}"
    if "/tenants/" in path:
        return f"{method} {path}: tenants (multi-tenancy) are not served"
    if path.endswith(":createSessionCookie"):
        return "identitytoolkit.projects.createSessionCookie: session cookies are not served"
    for tail, name in (
        (":queryAccounts", "projects.queryAccounts"),
        ("/accounts:query", "projects.accounts.query"),
        ("/accounts:batchCreate", "projects.accounts.batchCreate"),
    ):
        if path.endswith(tail):
            return f"identitytoolkit.{name}"
    return f"no method of Identity Toolkit v1 this fake serves has the route {method} {path}"


def _guarded(handler: Handler) -> Handler:
    async def answered(request: Request) -> Response:
        try:
            return await handler(request)
        except wire.AuthRefusal as refusal:
            return _refused(refusal)

    return answered


def _app(api: _Api) -> ASGIApp:
    async def certs(request: Request) -> Response:
        return Response(certificates(), media_type=wire.JSON, headers={"Cache-Control": wire.CERTIFICATES_CACHE})

    project = "/v1/projects/{project}"
    routes = [
        Route("/v1/accounts:signUp", _guarded(api.sign_up), methods=["POST"]),
        Route("/v1/accounts:signInWithPassword", _guarded(api.sign_in_with_password), methods=["POST"]),
        Route("/v1/accounts:signInWithCustomToken", _guarded(api.sign_in_with_custom_token), methods=["POST"]),
        Route("/v1/accounts:signInWithIdp", _guarded(api.sign_in_with_idp), methods=["POST"]),
        Route("/v1/accounts:signInWithEmailLink", _guarded(api.sign_in_with_email_link), methods=["POST"]),
        Route("/v1/accounts:lookup", _guarded(api.lookup), methods=["POST"]),
        Route("/v1/accounts:update", _guarded(api.update), methods=["POST"]),
        Route("/v1/accounts:delete", _guarded(api.delete), methods=["POST"]),
        Route("/v1/accounts:sendOobCode", _guarded(api.send_oob_code), methods=["POST"]),
        Route(f"{project}/accounts", _guarded(api.admin_create), methods=["POST"]),
        Route(f"{project}/accounts:lookup", _guarded(api.lookup), methods=["POST"]),
        Route(f"{project}/accounts:update", _guarded(api.update), methods=["POST"]),
        Route(f"{project}/accounts:delete", _guarded(api.delete), methods=["POST"]),
        Route(f"{project}/accounts:batchDelete", _guarded(api.batch_delete), methods=["POST"]),
        Route(f"{project}/accounts:batchGet", _guarded(api.batch_get), methods=["GET"]),
        Route(f"{project}/accounts:sendOobCode", _guarded(api.send_oob_code), methods=["POST"]),
        Route("/{rest:path}", api.unserved, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
    ]
    unserved = Route("/{rest:path}", api.unserved, methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    return _ByHost(
        {
            IDENTITY_TOOLKIT: Router(routes=routes),
            SECURE_TOKEN: Router(routes=[Route("/v1/token", api.refresh, methods=["POST"]), unserved]),
            CERTIFICATES_HOST: Router(routes=[Route(wire.CERTIFICATES_PATH, certs, methods=["GET"]), unserved]),
        }
    )


IDENTITY_TOOLKIT = "identitytoolkit.googleapis.com"
SECURE_TOKEN = "securetoken.googleapis.com"
CERTIFICATES_HOST = "www.googleapis.com"


class _ByHost:
    """Each host's own routes: `/v1/token` is the Secure Token API's and no Identity Toolkit method. A request to any
    other host (the app reached directly, without the proxy) is answered by Identity Toolkit's."""

    def __init__(self, by_host: dict[str, Router]) -> None:
        self._by_host = by_host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers: list[tuple[bytes, bytes]] = scope["headers"] if "headers" in scope else []
        host = next((value for name, value in headers if name == b"host"), b"").decode("latin-1").lower()
        host = host.rsplit(":", 1)[0] if not host.startswith("[") else host
        router = self._by_host[host] if host in self._by_host else self._by_host[IDENTITY_TOOLKIT]
        await router(scope, receive, send)


def build() -> FirebaseAuthProvider:
    return FirebaseAuthProvider()
