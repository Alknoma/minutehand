"""Hub, the authorization service served beside every YouTrack at `/hub/api/rest`.

Since YouTrack 2026.1 Hub holds no YouTrack project and no project team: `/projects` answers none of the
instance's projects and a team is joined through YouTrack's own `/admin/projects/{id}/team/ownUsers`
(https://www.jetbrains.com/help/youtrack/devportal/hub-rest-api-deprecated-endpoints-2026-1.html). Hub still answers
its users, its two instance-wide groups, the caller's permissions cache and service tokens. A user is named here by
its `ringId`; YouTrack's database ids name nothing in Hub.

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
from minutehand.domain.errors import NotServed
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

    def _user(self, user: wire.StoredUser) -> wire.HubUserOut:
        return wire.HubUserOut(id=user.ringId, login=user.login, name=user.fullName, banned=user.banned)

    def _page(self, call: Call, collection: str, items: Sequence[wire.HubAnswer]) -> Answered:
        start, limit = wire.page_bounds(call.param("$skip"), call.param("$top"), default=wire.HUB_PAGE_DEFAULT)
        shown = items[start : start + limit]
        spec = wire.parse_hub_fields(call.param("fields"))
        top = limit
        return 200, wire.render_hub_page(collection, list(shown), skip=start, top=top, total=len(items), spec=spec)

    # ------------------------------------------------------------------ routes

    def permissions(self, call: Call) -> Answered:
        """The token's permissions, one entry per permission held: globally, or in the projects listed by Hub id
        and key. A permission held nowhere has no entry. What the seed's grants say, reported, never enforced."""
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
        """Since YouTrack 2026.1 Hub's projects are Hub's own and no YouTrack project is among them: whatever the
        query, the collection holds none of the instance's projects."""
        self._world.saw(state.instance_ref(), Operation.SEARCH)
        return self._page(call, "projects", [])

    def project(self, call: Call) -> Answered:
        raise wire.not_found(call.path("project"))

    def groups(self, call: Call) -> Answered:
        """The two instance-wide groups. A YouTrack project's team is no Hub group since YouTrack 2026.1."""
        everyone = [self._user(u) for u in self._world.users()]
        groups: list[wire.HubAnswer] = [
            wire.HubGroupOut(id=group_id, name=name, project=None, users=everyone) for group_id, name in _GLOBAL_GROUPS
        ]
        return self._page(call, "usergroups", groups)

    def group_add(self, call: Call) -> Answered:
        """A project's team is joined through YouTrack (`/admin/projects/{id}/team/ownUsers`), not Hub: an id that
        is no instance-wide group names nothing here."""
        group = call.path("group")
        if any(group == known for known, _ in _GLOBAL_GROUPS):
            raise NotServed(f"POST /usergroups/{group}/users: membership of Hub's instance-wide groups")
        raise wire.not_found(group)

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
        """OAuth 2.0 client credentials: the token acts as the seeded service's user, else as the agent's account.
        The client's secret is not checked."""
        try:
            form = wire.TokenRequestIn.model_validate(
                {k: v[0] for k, v in parse_qs(call.raw.decode("utf-8", "replace")).items()}
            )
        except ValidationError as error:
            raise NotServed(
                "a token request with a parameter this fake does not read: what Hub answers is not recorded"
            ) from error
        client_id = form.client_id
        scheme, _, encoded = (call.header("authorization") or "").partition(" ")
        if scheme.lower() == "basic":
            try:
                client_id = base64.b64decode(encoded).decode().partition(":")[0]
            except (binascii.Error, UnicodeDecodeError):
                client_id = None
        grant = form.grant_type
        if grant is None:
            raise NotServed("a token request without grant_type: what Hub answers is not recorded")
        if grant != "client_credentials":
            raise NotServed(f"the grant {grant}: only client_credentials is served")
        service = None if client_id is None else self._world.service(client_id)
        user = self._world.agent().id if service is None else service.user
        issued = f"1.{uuid.uuid5(_TOKENS, f'{client_id}:{self._world.head()}')}"
        expires = call.now + TOKEN_LIFETIME_SECONDS * 1000
        self._world.write_token(
            issued, wire.StoredToken(user=user, client=client_id, expires=expires), actor=Actor.AGENT
        )
        scope = form.scope or "YouTrack"
        return 200, wire.token_body(wire.TokenOut(access_token=issued, expires_in=TOKEN_LIFETIME_SECONDS, scope=scope))


def hub_routes(api: YouTrackApi) -> list[tuple[str, dict[str, Callable[[Call], Answered]]]]:
    """Each Hub path with its handlers by method."""
    hub = Hub(api)
    return [
        ("/permissions/cache", {"GET": hub.permissions}),
        ("/projects", {"GET": hub.projects}),
        ("/projects/{project}", {"GET": hub.project}),
        ("/usergroups", {"GET": hub.groups}),
        ("/usergroups/{group}/users", {"POST": hub.group_add}),
        ("/users/me", {"GET": hub.me}),
        ("/users", {"GET": hub.users}),
        ("/users/{user}", {"GET": hub.user}),
        ("/oauth2/token", {"POST": hub.token}),
    ]
