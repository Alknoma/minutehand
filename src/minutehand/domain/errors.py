"""The kinds of exception that may leave a provider's request handler, after LocalStack's `ServiceException`
and moto's: one converter at the proxy (`adapters.answering`) tells them apart and answers each.

- `ServiceRefusal`: the real service would refuse this request. Each provider's own refusal classes subclass it and
  render the answer the real service gives (`render`), in that service's wire shape.
- `NotServed`: the real service has this operation and the fake does not serve it. Answered 501 in the vendor's
  error shape, naming the method and path; when the run declares the host outbound (`domain.outbound`), the call
  falls through to that declaration instead. A bare `NotImplementedError` is answered the same 501 and never falls
  through: it may be Python's own, not the provider saying what it does not serve. A refusal that is both (a
  provider's `ServiceRefusal` subclassing `NotServed`) keeps the vendor's own rendering of its 501 and still falls
  through.
- `GrpcRefusal`: the real service would refuse this gRPC call, with the status it names. Only a provider's gRPC
  methods (`ports.provider.ServesGrpc`) raise it; their other exceptions are read as above.
- anything else: Minutehand's own bug. Answered 500 in the vendor's error shape, its message beginning
  "minutehand internal error while answering", and kept with its traceback on the recorded call
  (`world.CallFailure`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import AwareDatetime, Field

from minutehand.domain.scenario import Model
from minutehand.domain.world import GrpcCode


class Rendered(Model):
    """An answer as it goes on the wire."""

    status: int = Field(ge=100, le=599)
    content_type: str
    body: bytes
    headers: list[tuple[str, str]] = Field(default=[], description="Besides content-type and content-length")


class Asked(Model):
    """The call a refusal answers, as far as a vendor's error body names it: some stamp the moment, a request id
    derived from the URL, or an id the caller sent in a header."""

    method: str
    url: str = Field(description="As the provider's app saw it: scheme, host, path and query")
    path: str = Field(description="As the provider's app saw it, without the query")
    headers: list[tuple[str, str]] = Field(default=[], description="The request's headers, names lowercased")
    now: AwareDatetime = Field(description="The world's clock")

    def header(self, name: str) -> str | None:
        """The first value of the header `name`, when the request carried it."""
        return next((value for key, value in self.headers if key == name.lower()), None)


class ServiceRefusal(Exception, ABC):
    """The real service would refuse this request. Never raised as itself: a provider subclasses it once per wire
    shape it refuses in, and `render` gives exactly the answer that provider's app gives for it."""

    @abstractmethod
    def render(self, asked: Asked) -> Rendered:
        """The answer the real service gives to `asked`."""


class NotServed(NotImplementedError):
    """The real service has this operation and the fake does not serve it: what a provider raises, by name, for a
    method, a path or an option it leaves out."""


class GrpcRefusal(Exception):
    """The real service would refuse this gRPC call: the status it ends with and its `grpc-message`."""

    def __init__(self, code: GrpcCode, message: str) -> None:
        if code is GrpcCode.OK:
            raise ValueError("a refusal ends with a status other than OK")
        super().__init__(message)
        self.code = code
        self.message = message
