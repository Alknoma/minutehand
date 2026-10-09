"""What every route handler of the GitHub provider shares: who a call acts as, what a handler answers, reading a
request's query and headers, and paging a list as GitHub's `Link` header says."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlencode

from starlette.requests import Request

from minutehand.adapters.providers.github import wire
from minutehand.domain.errors import NotServed

PAGE_DEFAULT = 30
PAGE_MAX = 100


@dataclass(frozen=True)
class Caller:
    """Who a call acts as: nobody, or a user through one of their tokens."""

    account: wire.StoredAccount | None
    token: wire.StoredToken | None


@dataclass
class Answered:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


Handler = Callable[[Request, Caller], Awaitable[Answered]]


def param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def chosen[Choice: StrEnum](request: Request, name: str, vocabulary: type[Choice], default: Choice) -> Choice:
    """A query parameter the reference gives a closed vocabulary; a value outside it is refused by name."""
    text = param(request, name)
    if text is None:
        return default
    try:
        return vocabulary(text)
    except ValueError:
        raise NotServed(f"{name}={text}: the reference lists {', '.join(v.value for v in vocabulary)}") from None


def header(request: Request, name: str) -> str | None:
    return request.headers[name] if name in request.headers else None


def as_json(entity: wire.Wire | list[wire.Wire], status: int = 200, headers: dict[str, str] | None = None) -> Answered:
    if isinstance(entity, list):
        body = ("[" + ",".join(e.model_dump_json(by_alias=True) for e in entity) + "]").encode()
    else:
        body = entity.model_dump_json(by_alias=True).encode()
    return Answered(status, body, headers or {})


def number(text: str | None, default: int) -> int:
    if text is None:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def paged(
    request: Request, total: int, *, ceiling: int | None = None, path: str | None = None
) -> tuple[int, int, dict[str, str]]:
    """The window `per_page` and `page` ask for, and the `Link` header pointing at the others, at `path` (the
    call's own path when None)."""
    at_path = request.url.path if path is None else path
    per_page = number(param(request, "per_page"), PAGE_DEFAULT)
    per_page = min(max(per_page, 1), PAGE_MAX)
    page = max(number(param(request, "page"), 1), 1)
    reachable = min(total, ceiling) if ceiling is not None else total
    last = max(math.ceil(reachable / per_page), 1)
    links: list[str] = []

    def at(n: int, rel: str) -> str:
        query = {k: v for k, v in request.query_params.items() if k != "page"}
        query["page"] = str(n)
        return f'<{wire.API}{at_path}?{urlencode(query)}>; rel="{rel}"'

    if page > 1:
        links.append(at(min(page - 1, last), "prev"))
    if page < last:
        links.append(at(page + 1, "next"))
        links.append(at(last, "last"))
    if page > 1:
        links.append(at(1, "first"))
    start = (page - 1) * per_page
    return start, start + per_page, {"Link": ", ".join(links)} if links else {}
