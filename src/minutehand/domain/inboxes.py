"""Work waiting on a person inside the agent's OWN product: an operation to approve in its web app, a question it
raised on its own page. No SaaS fake sees these, so the agent file declares, per inbox, how Minutehand reaches
them as each person does:

    inboxes:
      - name: approvals                                   # what its asks are recorded under
        kind: http                                        # an MCP inbox would be a second kind
        as_person:
          headers: {Authorization: "Bearer {credential}"} # `Person.credential`, never stored
        pending:                                          # LIST what waits on one person
          url: "http://127.0.0.1:8790/approvals?approver={person_email}"
          items: "items[*]"                               # every value here is one item
          id: id
          summary: summary
          gates: operation                                # optional: the id of what the item holds back
          paging: {next: next, param: cursor}
        decisions:                                        # DECIDE one, by id, as that person
          - name: approve
            reads: approved
            permits: true
            url: "http://127.0.0.1:8790/approvals/{item_id}/decision"
            body: {decision: approve}
          - name: reject
            reads: rejected
            permits: false
            url: "http://127.0.0.1:8790/approvals/{item_id}/decision"
            body: {decision: reject, reason: "{reason}"}
            inputs: [{name: reason, description: Why it is turned down}]

The shape follows what agent frameworks and workflow engines share (`docs/inboxes.md`): a list per person, each
item an id, who it waits on, a summary, the decisions allowed and what they take; a decision made by id, as that
person, with a choice and its inputs.

Templates are the capture declarations' (`domain.outbound`): `{name}` inside a string, filled with no parsing, a
body written as structure, a path into a JSON answer as `domain.outbound.BodyPath`. A template may name the person
(`PERSON_FIELDS`); a decision's also the item (`ITEM_FIELDS`) and its own inputs by name; a list's also `{cursor}`
when it pages by one in its URL.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from minutehand.domain.outbound import BodyPath, placeholders
from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import Actor, InboxItemSnapshot, ItemStatus

PERSON_FIELDS = ("person_key", "person_email", "person_name", "credential")
"""What any inbox template may name about the person Minutehand acts as: their key, email and name in the scenario,
and their `Person.credential`."""

ITEM_FIELDS = ("item_id", "decided_at")
"""What a decision's template may name beside the person and its inputs: the item's own id, and the simulated
moment the decision is made (ISO 8601)."""

CURSOR = "cursor"
"""What a list's template may name to place the cursor of the page it reads (`Paging`) itself."""

InputName = str


def _named(where: str, value: JsonValue, allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(placeholders(value)) - set(allowed))
    if unknown:
        raise ValueError(f"{where} names {', '.join('{' + u + '}' for u in unknown)}; it may name {', '.join(allowed)}")


class AsPerson(Model):
    """How Minutehand signs in as a person: headers sent with every call it makes as them. A header naming
    `{credential}` makes the people who hold a `Person.credential` the only ones it can act as."""

    headers: dict[str, str] = Field(min_length=1, description="Header name -> template")

    @model_validator(mode="after")
    def _names_known_fields(self) -> Self:
        for name, value in self.headers.items():
            _named(f"the as_person header {name!r}", value, PERSON_FIELDS)
        return self

    @property
    def needs_credential(self) -> bool:
        return any("credential" in placeholders(v) for v in self.headers.values())


class Paging(Model):
    """A list read page by page: `next` is where an answer holds the cursor of the next page (absent, null or empty on
    the last); the cursor is sent as the query parameter `param`, or where the list's URL names `{cursor}`."""

    next: BodyPath
    param: str | None = Field(default=None, min_length=1, description="None: the URL places `{cursor}` itself")
    most: int = Field(default=50, ge=1, description="Pages read at most in one reading")


class Listing(Model):
    """How to list what waits on one person: one request, read as that person, and where each item's parts are.

    `waits_on` makes it a list of everyone's items (or a team's): each item names whom it waits on, by email or
    `Person.key`, and only the person read for keeps theirs. Without it the list is that person's own."""

    url: str = Field(min_length=1)
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = {}
    body: JsonValue = Field(default=None, description="For a POST, as structure, with `{name}` in its strings")
    items: BodyPath | None = Field(
        default=None, description="Every value here in the answer is one item (`items[*]`); None: the answer is a list"
    )
    id: BodyPath = Field(description="In an item: its own id")
    summary: BodyPath = Field(description="In an item: what it asks, in words a person reads")
    waits_on: BodyPath | None = Field(default=None, description="In an item: whom it waits on, an email or a key")
    category: BodyPath | None = Field(default=None, description="In an item: its kind in the product's words")
    decisions: BodyPath | None = Field(
        default=None,
        description="In an item: the names of the decisions allowed on it (a list, or one); None: every one declared",
    )
    gates: BodyPath | None = Field(
        default=None,
        description="In an item: the id of the operation it holds back, which a later call of the agent's carries "
        "when it goes ahead (`checks.acted_without_approval`)",
    )
    paging: Paging | None = None

    @model_validator(mode="after")
    def _names_known_fields(self) -> Self:
        allowed = (*PERSON_FIELDS, CURSOR) if self.paging is not None and self.paging.param is None else PERSON_FIELDS
        _named("a list's url", self.url, allowed)
        _named("a list's body", self.body, allowed)
        for name, value in self.headers.items():
            _named(f"a list's header {name!r}", value, PERSON_FIELDS)
        if self.paging is not None and self.paging.param is None and CURSOR not in placeholders(self.url):
            raise ValueError("a list paged with no `param` places `{cursor}` in its url")
        if self.method == "GET" and self.body is not None:
            raise ValueError("a list read with GET has no body")
        return self


class DecisionInput(Model):
    """Something a person gives with a decision: a reason, an answer. A template names it as `{<name>}`."""

    name: InputName = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, description="What it is; a model-written person reads it")
    required: bool = True


class Succeeds(Model):
    """What a decision's answer looks like when the product took it: one of `statuses` (any 2xx when empty), and,
    with `at`, the JSON answer holding `equals` there."""

    statuses: list[int] = Field(default=[], description="Empty: any 2xx")
    at: BodyPath | None = None
    equals: JsonValue = None

    @model_validator(mode="after")
    def _a_value_has_a_place(self) -> Self:
        if self.equals is not None and self.at is None:
            raise ValueError("a decision's success names a value `equals` with no path `at` to find it")
        if any(not 100 <= s <= 599 for s in self.statuses):
            raise ValueError(f"a status is between 100 and 599: {self.statuses}")
        return self


class Decision(Model):
    """One decision a person can make on an item: the request the product's own page sends when they make it."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(default="", description="What it means; a model-written person reads it")
    reads: str | None = Field(
        default=None, description="How the record says it was made ('approved'); None: 'decided <name>'"
    )
    permits: bool | None = Field(
        default=None,
        description="Whether it lets what the item gates go ahead (approve: true, reject: false); None: it says "
        "nothing about that (an answer to a question)",
    )
    method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str = Field(min_length=1)
    headers: dict[str, str] = {}
    body: JsonValue = Field(default=None, description="As structure, with `{name}` in its strings")
    form: bool = Field(default=False, description="Sent as application/x-www-form-urlencoded: `body` is flat")
    inputs: list[DecisionInput] = []
    succeeds: Succeeds = Succeeds()

    @model_validator(mode="after")
    def _names_known_fields(self) -> Self:
        named = [i.name for i in self.inputs]
        twice = sorted({n for n in named if named.count(n) > 1})
        if twice:
            raise ValueError(f"decision {self.name!r} takes an input twice: {', '.join(twice)}")
        taken = sorted(set(named) & {*PERSON_FIELDS, *ITEM_FIELDS, CURSOR})
        if taken:
            raise ValueError(
                f"decision {self.name!r} names an input {', '.join(taken)}, which a template already means"
            )
        allowed = (*PERSON_FIELDS, *ITEM_FIELDS, *named)
        _named(f"decision {self.name!r}'s url", self.url, allowed)
        _named(f"decision {self.name!r}'s body", self.body, allowed)
        for header, value in self.headers.items():
            _named(f"decision {self.name!r}'s header {header!r}", value, allowed)
        if self.form and not (isinstance(self.body, dict) and all(isinstance(v, str) for v in self.body.values())):
            raise ValueError(f"decision {self.name!r} is sent as a form and has a flat body of strings")
        return self

    @property
    def said(self) -> str:
        return self.reads if self.reads is not None else f"decided {self.name}"

    def refuse_inputs(self, given: dict[str, str], who: str) -> None:
        """Inputs this decision does not take, or one it requires left out, refused naming `who` gave them."""
        known = {i.name for i in self.inputs}
        unknown = sorted(set(given) - known)
        if unknown:
            raise ValueError(
                f"{who} gives decision {self.name!r} {', '.join(unknown)}; it takes "
                + (", ".join(sorted(known)) or "nothing")
            )
        missing = sorted(i.name for i in self.inputs if i.required and i.name not in given)
        if missing:
            raise ValueError(f"{who} makes decision {self.name!r} without {', '.join(missing)}, which it requires")


class HttpInbox(Model):
    """One inbox in the agent's own product, reached over HTTP and JSON. Its `name` is what the items seen in it are
    recorded under, as a provider's key or a captured host's name is, and may be neither.

    An MCP inbox (a server whose tools list and settle what waits on a person) would be a second member with its own
    `kind`, beside this one in `Inbox`; the people, the ledger and the checks read only what is seen, never how."""

    kind: Literal["http"] = "http"
    name: ProviderKey
    as_person: AsPerson | None = Field(default=None, description="None: Minutehand sends no credential as anyone")
    pending: Listing
    decisions: list[Decision] = Field(min_length=1)

    @model_validator(mode="after")
    def _decisions_differ(self) -> Self:
        names = [d.name for d in self.decisions]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(f"inbox {self.name!r} declares decision {', '.join(twice)} twice")
        return self

    def decision(self, name: str) -> Decision | None:
        return next((d for d in self.decisions if d.name == name), None)


Inbox = HttpInbox
"""Every kind of inbox; a discriminated union on `kind` once there is more than one."""


def refuse_repeated_inboxes(inboxes: list[HttpInbox]) -> None:
    names = [i.name for i in inboxes]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise ValueError(f"inbox declared twice: {', '.join(twice)}")


class ListedItem(Model):
    """One item as an inbox listed it, read out of the product's answer."""

    item_id: str
    summary: str
    waits_on: str | None = Field(default=None, description="As the item names them; None: the list is the person's")
    category: str | None = None
    decisions: list[str] | None = Field(default=None, description="As the item lists them; None: it lists none")
    gates: str | None = None


class Listed(Model):
    """What one reading of one person's inbox found. `read` False: the product did not answer a list (refused,
    unreachable, not JSON, an item without its id), and nothing can be concluded about what is or is not there."""

    items: list[ListedItem] = []
    read: bool = True
    problem: str | None = None


class DecideAnswer(Model):
    """What the product answered a decision Minutehand made as a person."""

    accepted: bool
    status: int | None = Field(description="None: nothing answered")
    answer: str = Field(description="The product's answer, as text, cut to 500 characters")


def item_words(item: InboxItemSnapshot, actor: Actor, who: str) -> str:
    """One change to an item as the record reads to a person: `who` is the name of the person it waits on.

    "asked Nadia Ek to approve or reject: Send Owen the booking", "Nadia Ek approved: Send Owen the booking (reason:
    ...)", "withdrew the request to Nadia Ek: ...", "Nadia Ek tried to approve, and the product refused (409: ...)"."""
    given = "; ".join(f"{name}: {value}" for name, value in item.inputs.items())
    with_inputs = f" ({given})" if given else ""
    if actor is Actor.PERSON and item.status is ItemStatus.DECIDED:
        return f"{who} {item.said or item.decision}: {item.summary}{with_inputs}"
    if actor is Actor.PERSON:
        return f"{who} tried to {item.decision}{with_inputs}, and the product refused ({item.refused}): {item.summary}"
    if item.status is ItemStatus.WITHDRAWN:
        return f"withdrew the request to {who}: {item.summary}"
    return f"asked {who} to {' or '.join(item.decisions) or 'decide'}: {item.summary}"
