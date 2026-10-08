"""What every Google API this provider answers reads first: whom the bearer token acts as, and whether a fault the
scenario declared is due for the call. Drive, Docs, Slides, Gmail and Calendar share Google's sign-in, so a token
`/token` issued acts as one Google account in each of them.

Minutehand is a simulation and enforces no credential: no token is refused, whether unknown, expired, revoked or
missing. What a caller may see is still world data (whose mailbox, calendar or file it is, and what is shared)."""

from __future__ import annotations

from starlette.requests import Request

from minutehand.adapters import answering
from minutehand.adapters.providers.google_workspace import wire
from minutehand.adapters.providers.google_workspace.state import DriveWorld
from minutehand.domain.scenario import Model
from minutehand.ports.clock import Clock


class Caller(Model):
    email: str
    user: wire.DriveUser


def bearer(request: Request, access_token: str | None) -> str | None:
    """The token of `Authorization: Bearer`, or the `access_token` query parameter Google also reads."""
    authorization = request.headers["authorization"] if "authorization" in request.headers else ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return access_token or None


def signed_in(drive: DriveWorld, token: str | None) -> Caller:
    """Whom a call acts as: an access token `/token` issued acts as its user, expired or revoked alike; a credential
    the world holds (a seeded refresh token or service account) sent as the bearer as whom it signs in as; and any
    other token, or none, as the world's default identity (`WorkspaceSeed.unknown_credentials_act_as`)."""
    issued = drive.token(token) if token is not None else None
    email = issued.email if issued is not None else drive.credential(token).email
    user = drive.user(email)
    if user is None:
        raise LookupError(f"a credential acts as {email}, who is not a user of this Google Workspace")
    return Caller(email=email, user=user)


def due_fault(drive: DriveWorld, clock: Clock, operation: str) -> wire.FaultKind | None:
    """The fault the scenario declared for the next call of `operation`, if one is due now, counted as spent."""
    now = clock.now()
    for key, fault in drive.faults():
        if fault.operation != operation or fault.remaining < 1 or wire.moment(fault.after) > now:
            continue
        drive.keep_fault(key, fault.model_copy(update={"remaining": fault.remaining - 1}))
        answering.injected()
        return fault.kind
    return None
