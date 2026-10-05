"""An inbox in the agent's own product, reached over HTTP and JSON as each person (`domain.inboxes.HttpInbox`):
`ports.inboxes.ReachesInbox`.

Each request is the declaration's, filled with the named values (`domain.templates`): a template's method, URL,
headers and body; or an operation of an OpenAPI document (`adapters.agent.openapi`), whose method, path and
parameter locations come from the document and whose answer is checked against it. Values are read from answers
with JSONPath (`domain.jsonpath`).

Every request goes straight to the agent, never through the proxy, and is recorded in the world as Minutehand's
call, as that person (`Exchange.inbox_call`, with how the answer departed from the agent's API description when it
did): no header is kept, so neither is a credential, and the credential's value is replaced wherever else it
appears (a URL, a body) before anything is stored.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import JsonValue

from minutehand.adapters.agent.openapi import Operation, OperationUnresolved, resolve
from minutehand.adapters.proxy import redact
from minutehand.adapters.proxy.capture import structured
from minutehand.domain.inboxes import (
    BUILT_IN,
    CURSOR,
    DecideAnswer,
    Decision,
    HttpInbox,
    InboxRequest,
    Listed,
    ListedItem,
    OperationRequest,
    TemplateRequest,
)
from minutehand.domain.jsonpath import first, query
from minutehand.domain.people import Decides
from minutehand.domain.scenario import Person
from minutehand.domain.templates import fill
from minutehand.domain.world import Exchange, InboxAct, InboxCall
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

TIMEOUT = 30.0
ANSWER_KEPT = 500
"""Characters of a product's answer kept in what a decision answered."""


@dataclass(frozen=True)
class _Built:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    operation: Operation | None
    what: str


@dataclass(frozen=True)
class _Answered:
    status: int
    text: str
    parsed: object | None
    contract: str | None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpInboxReach:
    def __init__(self, declared: HttpInbox, credentials: Mapping[str, str]) -> None:
        """`credentials` holds each person's resolved `Person.credential`, by `Person.key`. Every operation the inbox
        names is resolved in its document now: one it cannot find, or fills as the document does not allow, raises
        `OperationUnresolved` naming it, before anything is called."""
        self.declared = declared
        self._credentials = dict(credentials)
        documents: dict[str, dict[str, object]] = {}
        self._operations: dict[int, Operation] = {}
        listing = declared.pending.request
        paging = declared.pending.paging
        if isinstance(listing, OperationRequest):
            given = {paging.param} if paging is not None and paging.param is not None else set()
            built_in = {"inbox", "person"} if listing.document == BUILT_IN else set()
            resolved = resolve(listing, documents, defaulted=frozenset(given | built_in))
            if paging is not None and paging.param is not None and paging.param not in resolved.parameters:
                raise OperationUnresolved(
                    f"{listing.operation} in {listing.document} has no parameter {paging.param} to send a page's "
                    "cursor in"
                )
            self._operations[id(listing)] = resolved
        for decision in declared.decisions:
            request = decision.request
            if isinstance(request, OperationRequest):
                built_in = {"inbox", "item"} if request.document == BUILT_IN else set()
                self._operations[id(request)] = resolve(request, documents, defaulted=frozenset(built_in))

    def can_act_as(self, person: Person) -> bool:
        signing_in = self.declared.as_person
        return signing_in is None or not signing_in.needs_credential or person.key in self._credentials

    # -- reading ------------------------------------------------------------------------------------------------

    async def pending(self, person: Person, world: Store, clock: Clock) -> Listed:
        listing = self.declared.pending
        values = self._values(person, clock)
        found: list[ListedItem] = []
        cursor: str | None = None
        for _ in range(listing.paging.most if listing.paging is not None else 1):
            param = listing.paging.param if listing.paging is not None else None
            extra = {param: cursor} if param is not None and cursor is not None else {}
            built = self._built(listing.request, {**values, CURSOR: cursor or ""}, person, extra=extra)
            answered = await self._send(built, person, InboxAct.LIST, world)
            if answered is None or not answered.ok:
                why = "nothing answered" if answered is None else f"it answered {answered.status}"
                return Listed(read=False, problem=f"{built.what}: {why}")
            if answered.contract is not None:
                return Listed(read=False, problem=answered.contract, contract=answered.contract)
            if answered.parsed is None:
                return Listed(read=False, problem=f"{built.what} answered something that is not JSON")
            for item in query(answered.parsed, listing.items):
                read = _item(item, self.declared)
                if read is None:
                    return Listed(read=False, problem=f"an item {built.what} listed has no id at {listing.id!r}")
                found.append(read)
            if listing.paging is None:
                break
            following = first(answered.parsed, listing.paging.next)
            if following is None or following == "":
                break
            cursor = str(following)
        return Listed(items=found)

    # -- deciding -----------------------------------------------------------------------------------------------

    async def decide(self, person: Person, item: str, decides: Decides, world: Store, clock: Clock) -> DecideAnswer:
        decision = self.declared.decision(decides.decision)
        if decision is None:
            return DecideAnswer(
                accepted=False, status=None, answer=f"inbox {self.declared.name} has no {decides.decision}"
            )
        values = {
            **self._values(person, clock),
            "item.id": item,
            **{f"input.{i.name}": decides.inputs[i.name] if i.name in decides.inputs else "" for i in decision.inputs},
        }
        built = self._built(decision.request, values, person, decision=(decision, item, decides))
        answered = await self._send(built, person, InboxAct.DECIDE, world)
        if answered is None:
            return DecideAnswer(accepted=False, status=None, answer=f"{built.what}: nothing answered")
        succeeds = decision.succeeds
        accepted = answered.status in succeeds.statuses if succeeds.statuses else answered.ok
        if accepted and succeeds.at is not None:
            accepted = answered.parsed is not None and first(answered.parsed, succeeds.at) == succeeds.equals
        return DecideAnswer(
            accepted=accepted, status=answered.status, answer=answered.text[:ANSWER_KEPT], contract=answered.contract
        )

    # -- one request --------------------------------------------------------------------------------------------

    def _values(self, person: Person, clock: Clock) -> dict[str, str]:
        return {
            "person.key": person.key,
            "person.email": person.email,
            "person.name": person.name,
            "person.credential": self._credentials[person.key] if person.key in self._credentials else "",
            "clock.now": clock.now().isoformat(),
        }

    def _headers(self, templates: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
        return {name: _text_of(fill(template, values)) for name, template in templates.items()}

    def _built(
        self,
        request: InboxRequest,
        values: Mapping[str, str],
        person: Person,
        *,
        extra: Mapping[str, str] | None = None,
        decision: tuple[Decision, str, Decides] | None = None,
    ) -> _Built:
        signing_in = self.declared.as_person
        headers = {
            **(self._headers(signing_in.headers, values) if signing_in is not None else {}),
            **self._headers(request.headers, values),
        }
        if isinstance(request, TemplateRequest):
            url = _text_of(fill(request.url, {k: quote(v, safe="@") for k, v in values.items()}))
            for name, value in (extra or {}).items():
                url = _with_query(url, name, value)
            raw, kind = _encoded(fill(request.body, values) if request.body is not None else None, form=request.form)
            if kind:
                headers = {"content-type": kind, **headers}
            return _Built(request.method, url, headers, raw, None, f"{request.method} {request.url}")
        operation = self._operations[id(request)]
        parameters = {name: _text_of(fill(template, values)) for name, template in request.parameters.items()}
        if request.document == BUILT_IN:
            defaults = {"inbox": self.declared.name, "person": person.email}
            if decision is not None:
                defaults["item"] = decision[1]
            parameters = {
                **{k: v for k, v in defaults.items() if k in operation.parameters},
                **parameters,
            }
        parameters.update(extra or {})
        url, in_headers = operation.url(parameters)
        body: JsonValue = fill(request.body, values) if request.body is not None else None
        if body is None and decision is not None and request.document == BUILT_IN and operation.takes_body:
            made, item, decides = decision
            body = {
                "item": item,
                "decision": made.name,
                "inputs": dict(decides.inputs),
                "person": {"key": person.key, "email": person.email, "name": person.name},
                "decided_at": values["clock.now"],
            }
        raw, kind = _encoded(body, form=False)
        if kind:
            headers = {"content-type": kind, **headers}
        what = f"{operation.method} {operation.path} ({request.operation})"
        return _Built(operation.method, url, {**headers, **in_headers}, raw, operation, what)

    async def _send(self, built: _Built, person: Person, act: InboxAct, world: Store) -> _Answered | None:
        answer: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            try:
                answer = await client.request(built.method, built.url, content=built.body, headers=built.headers)
            except httpx.HTTPError:
                answer = None
        answered: _Answered | None = None
        if answer is not None:
            parsed = structured(answer.text, answer.headers.get("content-type")) if answer.text else None
            contract = (
                built.operation.mismatch(answer.status_code, _json(parsed))
                if built.operation is not None and (parsed is not None or not answer.text)
                else None
            )
            answered = _Answered(answer.status_code, answer.text, parsed, contract)
        secret = self._credentials[person.key] if person.key in self._credentials else None
        parts = urlsplit(built.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        kind = built.headers["content-type"] if "content-type" in built.headers else ""
        sent = built.body.decode("utf-8", "replace") if built.body else None
        world.attach(
            Exchange(
                method=built.method,
                host=parts.netloc,
                path=_hidden(redact.path(path), secret) or path,
                status=answered.status if answered is not None else 0,
                request_body=_hidden(redact.body(sent, kind), secret) if sent else None,
                response_body=_hidden(answered.text, secret) if answered is not None and answered.text else None,
                inbox_call=InboxCall(
                    inbox=self.declared.name,
                    person=person.key,
                    act=act,
                    contract=answered.contract if answered is not None else None,
                ),
            ),
            first_seq=world.head() + 1,
            last_seq=world.head(),
        )
        return answered


def _text_of(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def _json(value: object) -> JsonValue:
    return json.loads(json.dumps(value))


def _encoded(body: JsonValue, *, form: bool) -> tuple[bytes | None, str]:
    if body is None:
        return None, ""
    if form and isinstance(body, dict):
        return urlencode({str(k): str(v) for k, v in body.items()}).encode(), "application/x-www-form-urlencoded"
    return json.dumps(body, ensure_ascii=False).encode(), "application/json"


def _hidden(text: str | None, secret: str | None) -> str | None:
    """`text` with the credential's value replaced wherever it appears."""
    if text is None or not secret:
        return text
    return text.replace(secret, redact.REDACTED)


def _with_query(url: str, name: str, value: str) -> str:
    parts = urlsplit(url)
    joined = f"{parts.query}&" if parts.query else ""
    return urlunsplit(parts._replace(query=joined + urlencode({name: value})))


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str | int | float | bool):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _item(holder: object, declared: HttpInbox) -> ListedItem | None:
    listing = declared.pending
    item_id = _text(first(holder, listing.id))
    if item_id is None or item_id == "":
        return None
    decisions: list[str] | None = None
    if listing.decisions is not None:
        named = query(holder, listing.decisions)
        flat = [n for v in named for n in (v if isinstance(v, list) else [v])]
        decisions = [str(n) for n in flat if isinstance(n, str | int)]
    return ListedItem(
        item_id=item_id,
        summary=_text(first(holder, listing.summary)) or "",
        waits_on=_text(first(holder, listing.waits_on)) if listing.waits_on is not None else None,
        category=_text(first(holder, listing.category)) if listing.category is not None else None,
        decisions=decisions,
        gates=_text(first(holder, listing.gates)) if listing.gates is not None else None,
    )
