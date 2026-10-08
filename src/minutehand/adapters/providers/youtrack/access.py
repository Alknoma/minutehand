"""Who a request is, what the seed says they hold, and what the scenario puts in the way of their calls.

A token names a user: a token the seed declares, or one Hub issued, acts as its user; any other token, or none,
acts as the agent's account. Minutehand deliberately checks no credential and enforces no permission, so no call is
refused for either (a banned user's token, an expired one, a missing one all reach the route).

The permissions a user holds are world data that Hub's permissions cache reports, never a check: every user holds
every permission everywhere but where the seed's grants take one away, and nobody holds Update Project on a project
made through the API (Hub's cache listed it on no project for the account that made and led one, as recorded in
`CLAIMS.md`).
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

    def caller(self, authorization: str | None) -> wire.StoredUser:
        """The user the call acts as: the one its token names, else the agent's account."""
        token = bearer(authorization)
        known = self._world.token(token) if token is not None else None
        user = self._world.user(known.user) if known is not None else None
        return user if user is not None else self._world.agent()

    def holds(self, user: wire.StoredUser, permission: wire.Permission, project: wire.StoredProject | None) -> bool:
        """Whether the seed gives `user` the permission: reported by Hub's permissions cache, never enforced."""
        held = True
        if permission is wire.Permission.UPDATE_PROJECT:
            held = project is not None and not project.createdThroughApi
        for grant in self._world.grants():
            if grant.user != user.id or grant.permission is not permission:
                continue
            if grant.project is None or (project is not None and grant.project == project.id):
                held = grant.held
        return held


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
