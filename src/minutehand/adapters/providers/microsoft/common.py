"""What every surface of the provider shares: reading the bearer token a call carries, the refusal every surface's
own refuses as, and Graph's error shape."""

from __future__ import annotations

from abc import ABC

from starlette.requests import Request

from minutehand.adapters.providers.microsoft import tokens, wire
from minutehand.adapters.providers.microsoft.wire import TokenUse
from minutehand.domain.errors import Rendered, ServiceRefusal
from minutehand.ports.clock import Clock

JSON = "application/json; charset=utf-8"
GRAPH_JSON = "application/json;odata.metadata=minimal;odata.streaming=true;IEEE754Compatible=false;charset=utf-8"


def header(request: Request, name: str) -> str | None:
    return request.headers[name] if name in request.headers else None


def query(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def bearer(request: Request) -> str | None:
    authorization = header(request, "authorization") or ""
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class MicrosoftRefusal(ServiceRefusal, ABC):
    """A refusal by one of the provider's surfaces. `answered` stamps it with what its answer names of the moment
    and the call it refuses (Graph's `innerError`, the identity platform's `timestamp`), once, as it leaves the app."""

    def answered(self, clock: Clock, request: Request) -> None:
        """Nothing, for a shape that names neither the moment nor the call."""
        del clock, request


class GraphRefusal(MicrosoftRefusal):
    """Graph answered an error: `status`, Graph's `code` and a message of this provider's own wording, in Graph's
    `{"error": {"code", "message", "innerError"}}`, with `Retry-After` when it says when to try again."""

    def __init__(
        self, status: int, code: str, message: str, *, retry_after: int | None = None, deliberate: bool = False
    ) -> None:
        super().__init__(code, message, status=status, deliberate=deliberate)
        self.retry_after = retry_after
        self.inner: wire.InnerError | None = None

    def answered(self, clock: Clock, request: Request) -> None:
        client_request = header(request, "client-request-id") or tokens.derived_trace(f"{request.url.path}{self.code}")
        self.inner = wire.InnerError(
            date=clock.now().strftime("%Y-%m-%dT%H:%M:%S"),
            request_id=tokens.derived_trace(f"{request.url}{clock.now().isoformat()}"),
            client_request_id=client_request,
        )

    def render(self) -> Rendered:
        assert self.inner is not None, f"Graph's {self.code} left the app without being answered"
        body = wire.GraphError(error=wire.GraphErrorBody(code=self.code, message=self.message, innerError=self.inner))
        headers = [("retry-after", str(self.retry_after))] if self.retry_after is not None else []
        return Rendered(status=self.status, content_type=JSON, body=wire.dump(body).encode(), headers=headers)


def not_found(what: str) -> GraphRefusal:
    return GraphRefusal(404, "itemNotFound", f"The resource '{what}' could not be found.")


def bad_request(message: str) -> GraphRefusal:
    return GraphRefusal(400, "invalidRequest", message)


def graph_caller(request: Request) -> wire.Claims:
    """The claims of the Graph access token the call carries; refused in Graph's shape when there is none."""
    token = bearer(request)
    if token is None:
        raise GraphRefusal(401, "InvalidAuthenticationToken", "Access token is empty.")
    try:
        claims = tokens.decode(token, use=TokenUse.ACCESS)
    except tokens.TokenRefused as e:
        code = "InvalidAuthenticationToken"
        raise GraphRefusal(401, code, f"Access token validation failure: {e.reason}.") from e
    if claims.aud != tokens.GRAPH_AUDIENCE:
        raise GraphRefusal(401, "InvalidAuthenticationToken", "Access token validation failure. Invalid audience.")
    return claims
