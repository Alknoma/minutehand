"""Hub, the authorization service served beside every YouTrack at `/hub/api/rest`.

A project's team is a Hub user group, so membership is written here, by Hub ids: a project's own uuid, its team
group's uuid, a user's `ringId`. YouTrack's database ids name nothing in Hub. Hub holds no project for one made
through YouTrack's REST API (measured on a live instance), so such a project is in none of these answers.

Hub pages every collection (`{"skip", "top", "total", "<collection>": [...]}`), spells nested `fields=` with
slashes or parentheses, and issues OAuth tokens to its services at `/oauth2/token`.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from pydantic import ValidationError

from minutehand.adapters.providers.youtrack import state, wire
from minutehand.domain.world import Actor, Operation

if TYPE_CHECKING:
    from minutehand.adapters.providers.youtrack.app import Answered, Call, YouTrackApi

TOKEN_LIFETIME_SECONDS = 3600
_GLOBAL_GROUPS = [("all-users", "All Users"), ("registered-users", "Registered Users")]
_TOKENS = uuid.UUID("0c7a3f4e-5a52-4bb0-9a51-77b1c64a7e21")


class Hub:
    def __init__(self, api: YouTrackApi) -> None:
        self._api = api
        self._world = api.world

    def _visible(self, user: wire.StoredUser) -> list[wire.StoredProject]:
        """The projects Hub shows this user: those it holds, that it may read."""
        return [
            p
            for p in self._world.projects()
            if not p.createdThroughApi and self._api.access.holds(user, wire.Permission.READ_PROJECT, p)
        ]

    def _user(self, user: wire.StoredUser) -> wire.HubUserOut:
        return wire.HubUserOut(id=user.ringId, login=user.login, name=user.fullName, banned=user.banned)

    def _project(self, project: wire.StoredProject) -> wire.HubProjectOut:
        return wire.HubProjectOut(
            id=project.ringId,
            key=project.shortName,
            name=project.name,
            team=wire.HubGroupRefOut(id=project.teamRingId, name=f"{project.name} Team"),
        )

    def _page(self, call: Call, collection: str, items: Sequence[wire.HubAnswer]) -> Answered:
        start, limit = wire.page_bounds(call.param("$skip"), call.param("$top"), default=wire.HUB_PAGE_DEFAULT)
        shown = items[start:] if limit is None else items[start : start + limit]
        spec = wire.parse_hub_fields(call.param("fields"))
        top = len(items) if limit is None else limit
        return 200, wire.render_hub_page(collection, list(shown), skip=start, top=top, total=len(items), spec=spec)

    # ------------------------------------------------------------------ routes

    def permissions(self, call: Call) -> Answered:
        """The token's permissions, one entry per permission held: globally, or in the projects listed by Hub id
        and key. A permission held nowhere has no entry."""
        user = call.caller
        hub_projects = [p for p in self._world.projects() if not p.createdThroughApi]
        entries: list[wire.HubAnswer] = []
        for permission in wire.Permission:
            name = wire.HubPermissionRefOut(key=permission.value, name=wire.PERMISSION_NAMES[permission])
            if permission in wire.GLOBAL_PERMISSIONS:
                if self._api.access.holds(user, permission, None):
                    entries.append(wire.HubCachedPermissionOut(permission=name, global_=True, projects=[]))
                continue
            held = [p for p in hub_projects if self._api.access.holds(user, permission, p)]
            if held:
                entries.append(
                    wire.HubCachedPermissionOut(
                        permission=name,
                        global_=False,
                        projects=[wire.HubProjectRefOut(id=p.ringId, key=p.shortName) for p in held],
                    )
                )
        self._world.saw(state.user_ref(user.id), Operation.READ)
        return 200, wire.render_hub(entries, wire.parse_hub_fields(call.param("fields")))

    def projects(self, call: Call) -> Answered:
        visible = self._visible(call.caller)
        query = (call.param("query") or "").strip()
        if query:
            attribute, colon, value = query.partition(":")
            wanted = value.strip().strip("{}").strip().lower()
            if not colon or attribute.strip().lower() not in ("key", "name") or not wanted:
                raise wire.bad_request(f"Unsupported query: {query}")
            read: Callable[[wire.StoredProject], str] = (
                (lambda p: p.shortName) if attribute.strip().lower() == "key" else (lambda p: p.name)
            )
            visible = [p for p in visible if read(p).lower() == wanted]
        self._world.saw(state.instance_ref(), Operation.SEARCH)
        return self._page(call, "projects", [self._project(p) for p in visible])

    def project(self, call: Call) -> Answered:
        reference = call.path("project")
        found = next((p for p in self._visible(call.caller) if p.ringId == reference), None)
        if found is None:
            raise wire.not_found(reference)
        return 200, wire.render_hub(self._project(found), wire.parse_hub_fields(call.param("fields")))

    def groups(self, call: Call) -> Answered:
        """The two instance-wide groups, and a project's team only where the caller may change that project."""
        everyone = [self._user(u) for u in self._world.users()]
        groups: list[wire.HubAnswer] = [
            wire.HubGroupOut(id=group_id, name=name, project=None, users=everyone) for group_id, name in _GLOBAL_GROUPS
        ]
        for project in self._world.projects():
            if project.createdThroughApi or not self._api.access.holds(
                call.caller, wire.Permission.UPDATE_PROJECT, project
            ):
                continue
            members = [self._user(u) for u in (self._world.user(m) for m in project.team) if u is not None]
            groups.append(
                wire.HubGroupOut(
                    id=project.teamRingId,
                    name=f"{project.name} Team",
                    project=wire.HubProjectRefOut(id=project.ringId, key=project.shortName),
                    users=members,
                )
            )
        return self._page(call, "usergroups", groups)

    def group_add(self, call: Call) -> Answered:
        """Put a user, named by Hub id, in a project's team group. Needs Update Project on that project."""
        group = call.path("group")
        project = next((p for p in self._world.projects() if p.teamRingId == group and not p.createdThroughApi), None)
        if project is None:
            if any(group == known for known, _ in _GLOBAL_GROUPS):
                raise wire.Refusal(
                    403, "Forbidden", "Insufficient permissions: this group is managed by the instance, not a project"
                )
            raise wire.not_found(group)
        self._api.access.require(call.caller, wire.Permission.UPDATE_PROJECT, project)
        body = wire.read_body(wire.HubUserRefIn, call.raw)
        if body.id is None or not body.id.strip():
            raise wire.bad_request("user id is required")
        member = self._world.user_by_ring_id(body.id)
        if member is None:
            raise wire.not_found(body.id)
        if member.id not in project.team:
            grown = project.model_copy(update={"team": [*project.team, member.id]})
            self._world.write_project(grown, actor=Actor.AGENT)
        return 200, wire.render_hub(self._user(member), wire.parse_hub_fields(call.param("fields")))

    def me(self, call: Call) -> Answered:
        self._world.saw(state.user_ref(call.caller.id), Operation.READ)
        return 200, wire.render_hub(self._user(call.caller), wire.parse_hub_fields(call.param("fields")))

    def users(self, call: Call) -> Answered:
        query = (call.param("query") or "").strip().lower()
        found = [
            u
            for u in self._world.users()
            if not query or query in u.login.lower() or query in u.fullName.lower() or query in (u.email or "").lower()
        ]
        self._world.saw(state.instance_ref(), Operation.SEARCH)
        return self._page(call, "users", [self._user(u) for u in found])

    def user(self, call: Call) -> Answered:
        reference = call.path("user")
        found = call.caller if reference == "me" else self._world.user_by_ring_id(reference)
        if found is None:
            raise wire.not_found(reference)
        return 200, wire.render_hub(self._user(found), wire.parse_hub_fields(call.param("fields")))

    def token(self, call: Call) -> Answered:
        """OAuth 2.0 client credentials: the service's id and secret, by Basic auth or in the form."""
        try:
            form = wire.TokenRequestIn.model_validate(
                {k: v[0] for k, v in parse_qs(call.raw.decode("utf-8", "replace")).items()}
            )
        except ValidationError as error:
            raise wire.oauth_refusal(
                400, "invalid_request", "The token request has a parameter Hub does not take"
            ) from error
        client_id, secret = form.client_id, form.client_secret
        scheme, _, encoded = (call.header("authorization") or "").partition(" ")
        if scheme.lower() == "basic":
            try:
                client_id, _, secret = base64.b64decode(encoded).decode().partition(":")
            except (binascii.Error, UnicodeDecodeError) as error:
                raise wire.oauth_refusal(401, "invalid_client", "Malformed client credentials") from error
        grant = form.grant_type
        if grant is None:
            raise wire.oauth_refusal(400, "invalid_request", "grant_type is required")
        if grant != "client_credentials":
            raise wire.oauth_refusal(400, "unsupported_grant_type", f"Grant type {grant} is not supported")
        service = None if client_id is None else self._world.service(client_id)
        if service is None or secret is None or state.digest(secret) != service.secretDigest:
            raise wire.oauth_refusal(401, "invalid_client", "Client authentication failed")
        issued = f"1.{uuid.uuid5(_TOKENS, f'{service.clientId}:{self._world.head()}')}"
        expires = call.now + TOKEN_LIFETIME_SECONDS * 1000
        self._world.write_token(
            issued, wire.StoredToken(user=service.user, client=service.clientId, expires=expires), actor=Actor.AGENT
        )
        scope = form.scope or "YouTrack"
        return 200, wire.token_body(wire.TokenOut(access_token=issued, expires_in=TOKEN_LIFETIME_SECONDS, scope=scope))


def hub_routes(api: YouTrackApi) -> list[tuple[str, dict[str, Callable[[Call], Answered]], bool]]:
    """Each Hub path with its handlers by method, and whether it is reached without a token."""
    hub = Hub(api)
    return [
        ("/permissions/cache", {"GET": hub.permissions}, False),
        ("/projects", {"GET": hub.projects}, False),
        ("/projects/{project}", {"GET": hub.project}, False),
        ("/usergroups", {"GET": hub.groups}, False),
        ("/usergroups/{group}/users", {"POST": hub.group_add}, False),
        ("/users/me", {"GET": hub.me}, False),
        ("/users", {"GET": hub.users}, False),
        ("/users/{user}", {"GET": hub.user}, False),
        ("/oauth2/token", {"POST": hub.token}, True),
    ]
