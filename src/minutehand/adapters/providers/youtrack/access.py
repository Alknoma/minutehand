"""Who a request is, what they may do, and what the scenario puts in the way of their calls.

A token maps to a user. With no token seeded, any bearer token acts as the agent's account; once a token is
seeded, only seeded tokens and those Hub issued are let in, and anything else is a 401.

Every user holds every permission everywhere, but two: nobody holds Update Project on a project made through the
API (measured on a live instance: the account that made a project and leads it could not change its settings), and
grants the seed writes give or take one permission from one user, in one project or all, the latest winning.
"""

from __future__ import annotations

import re

from minutehand.adapters.providers.youtrack import wire
from minutehand.adapters.providers.youtrack.state import YouTrackWorld

_BEARER = "bearer"


def bearer(authorization: str | None) -> str | None:
    """The token an `Authorization: Bearer …` header carries; None for no header, another scheme or no token."""
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() if scheme.lower() == _BEARER and token.strip() else None


class Access:
    def __init__(self, world: YouTrackWorld) -> None:
        self._world = world

    def caller(self, authorization: str | None, now: int) -> wire.StoredUser:
        token = bearer(authorization)
        if token is None:
            raise wire.unauthorized()
        known = self._world.token(token)
        if known is None and self._world.settings().tokensRequired:
            raise wire.unauthorized()
        if known is not None and known.expires is not None and now >= known.expires:
            raise wire.unauthorized()
        user = self._world.agent() if known is None else self._world.user(known.user)
        if user is None or user.banned:
            raise wire.unauthorized()
        return user

    def holds(self, user: wire.StoredUser, permission: wire.Permission, project: wire.StoredProject | None) -> bool:
        held = True
        if permission is wire.Permission.UPDATE_PROJECT:
            held = project is not None and not project.createdThroughApi
        for grant in self._world.grants():
            if grant.user != user.id or grant.permission is not permission:
                continue
            if grant.project is None or (project is not None and grant.project == project.id):
                held = grant.held
        return held

    def require(self, user: wire.StoredUser, permission: wire.Permission, project: wire.StoredProject | None) -> None:
        if not self.holds(user, permission, project):
            raise wire.forbidden(wire.PERMISSION_NAMES[permission])

    def readable(self, user: wire.StoredUser) -> list[wire.StoredProject]:
        """The projects whose issues this user may read: what a search covers."""
        return [p for p in self._world.projects() if self.holds(user, wire.Permission.READ_ISSUE, p)]


def fault_for(world: YouTrackWorld, method: str, path: str, now: int) -> wire.Refusal | None:
    """The refusal the scenario puts in the way of this call at this moment, if any."""
    for fault in world.faults():
        if fault.method != method.upper() or not _matches(fault.path, path):
            continue
        if now < fault.starts or (fault.ends is not None and now >= fault.ends):
            continue
        retry = None if fault.ends is None else max(1, (fault.ends - now + 999) // 1000)
        return wire.Refusal(
            fault.status,
            _REASONS.get(fault.status, "Error"),
            _DESCRIPTIONS.get(fault.status, "The request failed"),
            retry_after=retry,
            deliberate=True,
        )
    return None


_REASONS = {
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}
_DESCRIPTIONS = {
    429: "Too many requests. Try again later.",
    500: "Internal server error",
    502: "Bad gateway",
    503: "Service is temporarily unavailable",
    504: "Gateway timeout",
}


def _matches(pattern: str, path: str) -> bool:
    regex = "^" + "/".join("[^/]+" if part == "*" else re.escape(part) for part in pattern.split("/")) + "/?$"
    return re.match(regex, path) is not None
