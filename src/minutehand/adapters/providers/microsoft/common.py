"""What every surface of the provider shares: reading the bearer token a call carries, and Graph's error shape."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.providers.microsoft import tokens, wire
from minutehand.adapters.providers.microsoft.state import MicrosoftWorld
from minutehand.adapters.providers.microsoft.wire import TokenUse
from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
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


class GraphRefusal(ServiceRefusal):
    """Graph answered an error: `status`, Graph's `code` and a message of this provider's own wording."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def render(self, asked: Asked) -> Rendered:
        """What `graph_error` answers, from the call as `asked` names it."""
        client_request = asked.header("client-request-id") or tokens.derived_trace(f"{asked.path}{self.code}")
        body = wire.GraphError(
            error=wire.GraphErrorBody(
                code=self.code,
                message=self.message,
                innerError=wire.InnerError(
                    date=asked.now.strftime("%Y-%m-%dT%H:%M:%S"),
                    request_id=tokens.derived_trace(f"{asked.url}{asked.now.isoformat()}"),
                    client_request_id=client_request,
                ),
            )
        )
        headers = [("retry-after", str(self.retry_after))] if self.retry_after is not None else []
        return Rendered(status=self.status, content_type=JSON, body=wire.dump(body).encode(), headers=headers)


def error_answer(status: int, code: str, message: str) -> Rendered:
    """What Minutehand answers in Microsoft's place (501, 500), in Graph's `{"error": {"code", "message"}}`, which a
    Graph client and a Bot Framework client both read a failure from."""
    body = wire.GraphError(error=wire.GraphErrorBody(code=code, message=message))
    return Rendered(status=status, content_type=JSON, body=wire.dump(body).encode())


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


def graph_caller(request: Request, world: MicrosoftWorld) -> wire.Claims:
    """Who a Graph call is from: the tenant, app and user its bearer token names. Minutehand does not enforce
    credentials: a missing, unreadable, expired or foreign token is never refused, and one that names nobody is the
    tenant's own application calling (app-only)."""
    claims = tokens.presented(bearer(request))
    return claims if claims is not None else application(world, tokens.GRAPH_AUDIENCE)


def application(world: MicrosoftWorld, audience: str) -> wire.Claims:
    """The claims of the tenant's application calling with no user: what a call presenting no readable token is."""
    tenant = next(iter(world.tenants()), None)
    app = next(iter(world.apps()), None)
    app_id = app.app_id if app is not None else ""
    return wire.Claims(
        iss=tokens.issuer_for(tenant.id if tenant is not None else ""),
        aud=audience,
        iat=0,
        nbf=0,
        exp=0,
        tid=tenant.id if tenant is not None else None,
        appid=app_id,
        azp=app_id,
        sub=app_id,
        ver="2.0",
        uti="",
        minutehand_use=TokenUse.ACCESS,
    )
