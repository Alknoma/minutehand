"""What every surface of the provider shares: reading the bearer token a call carries, and Graph's error shape."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.providers.microsoft import tokens, wire
from minutehand.adapters.providers.microsoft.wire import TokenUse
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


class GraphRefusal(Exception):
    """Graph answered an error: `status`, Graph's `code` and a message of this provider's own wording."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def graph_error(refusal: GraphRefusal, clock: Clock, request: Request) -> Response:
    client_request = header(request, "client-request-id") or tokens.derived_trace(f"{request.url.path}{refusal.code}")
    body = wire.GraphError(
        error=wire.GraphErrorBody(
            code=refusal.code,
            message=refusal.message,
            innerError=wire.InnerError(
                date=clock.now().strftime("%Y-%m-%dT%H:%M:%S"),
                request_id=tokens.derived_trace(f"{request.url}{clock.now().isoformat()}"),
                client_request_id=client_request,
            ),
        )
    )
    headers = {"Retry-After": str(refusal.retry_after)} if refusal.retry_after is not None else None
    return Response(wire.dump(body), status_code=refusal.status, media_type=JSON, headers=headers)


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
