"""An inbox in the agent's own product, reached over HTTP and JSON as each person (`domain.inboxes.HttpInbox`):
`ports.inboxes.ReachesInbox`.

Every request is the declaration's template filled with the person's fields and, for a decision, the item's id
and the inputs given, with the capture declarations' own templating (`adapters.proxy.capture.fill`), read with
their paths (`values_at`). It goes straight to the agent, never through the proxy, and is recorded in the world as
Minutehand's call, as that person (`Exchange.inbox_call`): no header is kept, so neither is a credential, and the
credential's value is replaced wherever else it appears (a URL, a body) before anything is stored.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import JsonValue

from minutehand.adapters.proxy import redact
from minutehand.adapters.proxy.capture import fill, structured, values_at
from minutehand.domain.inboxes import DecideAnswer, HttpInbox, Listed, ListedItem
from minutehand.domain.people import Decides
from minutehand.domain.scenario import Person
from minutehand.domain.world import Exchange, InboxAct, InboxCall
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

TIMEOUT = 30.0
ANSWER_KEPT = 500
"""Characters of a product's answer kept in what a decision answered."""


class HttpInboxReach:
    def __init__(self, declared: HttpInbox, credentials: Mapping[str, str]) -> None:
        """`credentials` holds each person's resolved `Person.credential`, by `Person.key`."""
        self.declared = declared
        self._credentials = dict(credentials)

    def can_act_as(self, person: Person) -> bool:
        signing_in = self.declared.as_person
        return signing_in is None or not signing_in.needs_credential or person.key in self._credentials

    # -- reading ------------------------------------------------------------------------------------------------

    async def pending(self, person: Person, world: Store, clock: Clock) -> Listed:
        listing = self.declared.pending
        values = self._person(person)
        found: list[ListedItem] = []
        cursor: str | None = None
        for _ in range(listing.paging.most if listing.paging is not None else 1):
            url = listing.url
            if cursor is not None and listing.paging is not None and listing.paging.param is not None:
                url = _with_query(url, listing.paging.param, cursor)
            page_values = {**values, "{cursor}": cursor or ""}
            body = fill(listing.body, page_values) if listing.body is not None else None
            headers = {**self._signed_in(values), **_filled(listing.headers, values)}
            sent = await self._send(
                listing.method, _filled_url(url, page_values), body, headers, False, person, InboxAct.LIST, world
            )
            if sent is None or not sent.is_success:
                why = "nothing answered" if sent is None else f"it answered {sent.status_code}"
                return Listed(read=False, problem=f"{listing.method} {listing.url}: {why}")
            parsed = structured(sent.text, sent.headers.get("content-type"))
            if parsed is None:
                return Listed(read=False, problem=f"{listing.method} {listing.url} answered something that is not JSON")
            items = values_at(parsed, listing.items) if listing.items is not None else parsed
            if not isinstance(items, list):
                return Listed(read=False, problem=f"{listing.method} {listing.url} answered no list of items")
            for item in items:
                read = _item(item, self.declared)
                if read is None:
                    return Listed(read=False, problem=f"an item {listing.url} listed has no id at {listing.id!r}")
                found.append(read)
            if listing.paging is None:
                break
            following = _first(parsed, listing.paging.next)
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
            **self._person(person),
            "{item_id}": item,
            "{decided_at}": clock.now().isoformat(),
            **{"{" + i.name + "}": decides.inputs[i.name] if i.name in decides.inputs else "" for i in decision.inputs},
        }
        body = fill(decision.body, values) if decision.body is not None else None
        headers = {**self._signed_in(values), **_filled(decision.headers, values)}
        sent = await self._send(
            decision.method,
            _filled_url(decision.url, values),
            body,
            headers,
            decision.form,
            person,
            InboxAct.DECIDE,
            world,
        )
        if sent is None:
            return DecideAnswer(
                accepted=False, status=None, answer=f"{decision.method} {decision.url}: nothing answered"
            )
        succeeds = decision.succeeds
        accepted = sent.status_code in succeeds.statuses if succeeds.statuses else sent.is_success
        if accepted and succeeds.at is not None:
            parsed = structured(sent.text, sent.headers.get("content-type"))
            accepted = parsed is not None and _first(parsed, succeeds.at) == succeeds.equals
        return DecideAnswer(accepted=accepted, status=sent.status_code, answer=sent.text[:ANSWER_KEPT])

    # -- one request --------------------------------------------------------------------------------------------

    def _person(self, person: Person) -> dict[str, str]:
        return {
            "{person_key}": person.key,
            "{person_email}": person.email,
            "{person_name}": person.name,
            "{credential}": self._credentials[person.key] if person.key in self._credentials else "",
        }

    def _signed_in(self, values: Mapping[str, str]) -> dict[str, str]:
        signing_in = self.declared.as_person
        return _filled(signing_in.headers, values) if signing_in is not None else {}

    async def _send(
        self,
        method: str,
        url: str,
        body: JsonValue,
        headers: dict[str, str],
        form: bool,
        person: Person,
        act: InboxAct,
        world: Store,
    ) -> httpx.Response | None:
        raw: bytes | None = None
        if body is not None:
            if form and isinstance(body, dict):
                raw = urlencode({str(k): str(v) for k, v in body.items()}).encode()
                headers = {"content-type": "application/x-www-form-urlencoded", **headers}
            else:
                raw = json.dumps(body, ensure_ascii=False).encode()
                headers = {"content-type": "application/json", **headers}
        answer: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            try:
                answer = await client.request(method, url, content=raw, headers=headers)
            except httpx.HTTPError:
                answer = None
        secret = self._credentials[person.key] if person.key in self._credentials else None
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        kind = headers["content-type"] if "content-type" in headers else ""
        world.attach(
            Exchange(
                method=method,
                host=parts.netloc,
                path=_hidden(redact.path(path), secret) or path,
                status=answer.status_code if answer is not None else 0,
                request_body=_hidden(redact.body(raw.decode("utf-8", "replace"), kind), secret) if raw else None,
                response_body=_hidden(answer.text, secret) if answer is not None and answer.text else None,
                inbox_call=InboxCall(inbox=self.declared.name, person=person.key, act=act),
            ),
            first_seq=world.head() + 1,
            last_seq=world.head(),
        )
        return answer


def _hidden(text: str | None, secret: str | None) -> str | None:
    """`text` with the credential's value replaced wherever it appears."""
    if text is None or not secret:
        return text
    return text.replace(secret, redact.REDACTED)


def _filled(templates: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
    filled: dict[str, str] = {}
    for name, template in templates.items():
        value = template
        for placeholder, given in values.items():
            value = value.replace(placeholder, given)
        filled[name] = value
    return filled


def _filled_url(template: str, values: Mapping[str, str]) -> str:
    """A URL template filled with each value quoted for where it lands, so an email or a reason with spaces reads
    back as given."""
    url = template
    for placeholder, given in values.items():
        url = url.replace(placeholder, quote(given, safe="@"))
    return url


def _with_query(url: str, name: str, value: str) -> str:
    parts = urlsplit(url)
    query = f"{parts.query}&" if parts.query else ""
    return urlunsplit(parts._replace(query=query + urlencode({name: value})))


def _first(holder: object, path: str) -> object | None:
    found = values_at(holder, path)
    return found[0] if found else None


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str | int | float | bool):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _item(holder: object, declared: HttpInbox) -> ListedItem | None:
    listing = declared.pending
    item_id = _text(_first(holder, listing.id))
    if item_id is None or item_id == "":
        return None
    decisions: list[str] | None = None
    if listing.decisions is not None:
        named = values_at(holder, listing.decisions)
        flat = [n for v in named for n in (v if isinstance(v, list) else [v])]
        decisions = [str(n) for n in flat if isinstance(n, str | int)]
    return ListedItem(
        item_id=item_id,
        summary=_text(_first(holder, listing.summary)) or "",
        waits_on=_text(_first(holder, listing.waits_on)) if listing.waits_on is not None else None,
        category=_text(_first(holder, listing.category)) if listing.category is not None else None,
        decisions=decisions,
        gates=_text(_first(holder, listing.gates)) if listing.gates is not None else None,
    )
