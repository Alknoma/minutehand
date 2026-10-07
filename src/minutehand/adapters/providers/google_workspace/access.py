"""What every Google API this provider answers checks first: who the bearer token is, and whether a fault the
scenario declared is due for the call. Drive, Docs, Slides, Gmail and Calendar share Google's sign-in, so a token
`/token` issued is good for each of them, as one Google account's token is."""

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


def signed_in(drive: DriveWorld, clock: Clock, token: str | None, *, missing: wire.Refusal) -> Caller:
    """The user an access token was issued to; `missing` when there is none, and Google's 401 when it is unknown,
    expired or revoked."""
    if token is None:
        raise missing
    issued = drive.token(token)
    if issued is None or issued.revoked or wire.moment(issued.expires) <= clock.now():
        raise wire.invalid_credentials()
    user = drive.user(issued.email)
    if user is None:
        raise wire.invalid_credentials()
    return Caller(email=issued.email, user=user)


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
