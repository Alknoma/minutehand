"""Work waiting on a person inside the agent's OWN product: an operation to approve in its web app, a question it
raised on its own page. No SaaS fake sees these, so the agent file declares, per inbox, how Minutehand reaches
them as each person does (`docs/inboxes.md`):

    inboxes:
      - name: approvals                                    # what its asks are recorded under
        kind: http                                         # an MCP inbox would be a second kind
        as_person:
          headers: {Authorization: "Bearer {person.credential}"}
        pending:                                           # LIST what waits on one person
          request: {kind: template, url: "http://127.0.0.1:8790/approvals?approver={person.email}"}
          items: "$.items[*]"                              # JSONPath (RFC 9535): every node is one item
          id: "$.id"
          summary: "$.summary"
          paging: {next: "$.next", param: cursor}
        decisions:                                         # DECIDE one, by id, as that person
          - name: approve
            reads: approved
            request: {kind: template, url: "http://127.0.0.1:8790/approvals/{item.id}/decision",
                      body: {decision: approve}}
          - name: reject
            reads: rejected
            request: {kind: template, url: "http://127.0.0.1:8790/approvals/{item.id}/decision",
                      body: {decision: reject, reason: "{input.reason}"}}
            inputs: [{name: reason, description: Why it is turned down}]

Each request is either a template (method, URL, headers, body, filled from named values) or an operation in an
OpenAPI document (`OperationRequest`): the agent's own, or Minutehand's default shape (`document: minutehand`,
`schemas/agent-api.openapi.json`), from which the method, path, parameter locations and the answer's schema are
taken, and the answer is checked against that schema.

Templates use the one placeholder syntax (`domain.templates`); values are read with JSONPath (`domain.jsonpath`).
The shape follows what agent frameworks and workflow engines share: a list per person, each item an id, who it waits
on, a summary, the decisions allowed and what they take; a decision made by id, as that person, with a choice and its
inputs.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from minutehand.domain.jsonpath import JsonPath
from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.templates import PERSON, named, refuse_unknown
from minutehand.domain.world import Actor, InboxItemSnapshot, ItemStatus

CURSOR = "page.cursor"
DECIDING = ("item.id", "clock.now")
"""What a decision's request may name beside the person and its inputs (`input.<name>`)."""

BUILT_IN = "minutehand"
"""The `document` that names Minutehand's own default shape for an agent's side of the calls
(`schemas/agent-api.openapi.json`): an agent that implements it declares no request of its own."""


class TemplateRequest(Model):
    """A request written out: method, URL, headers and a body as structure, each string a template."""

    kind: Literal["template"] = "template"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=1)
    headers: dict[str, str] = {}
    body: JsonValue = Field(default=None, description="As structure, with `{namespace.name}` in its strings")
    form: bool = Field(default=False, description="Sent as application/x-www-form-urlencoded: `body` is flat")

    def refuse_unknown(self, where: str, allowed: tuple[str, ...]) -> None:
        refuse_unknown(f"{where}'s url", self.url, allowed)
        refuse_unknown(f"{where}'s body", self.body, allowed)
        for name, value in self.headers.items():
            refuse_unknown(f"{where}'s header {name!r}", value, allowed)
        if self.form and not (isinstance(self.body, dict) and all(isinstance(v, str) for v in self.body.values())):
            raise ValueError(f"{where} is sent as a form and has a flat body of strings")
        if self.method == "GET" and self.body is not None:
            raise ValueError(f"{where} is a GET and has no body")


class OperationRequest(Model):
    """An operation of an OpenAPI document, by its `operationId`: the method, the path, where each parameter goes
    and the schema of a successful answer come from the document. `parameters` fill the operation's parameters by
    name (path, query or header), `body` its JSON request body; both may name values as a template does.

    `document` is a file (relative to the directory Minutehand runs in), an http(s) URL, or `minutehand` for
    Minutehand's own default shape. `server` is the base URL the operation's path is put after; None: the document's
    first server."""

    kind: Literal["operation"] = "operation"
    document: str = Field(min_length=1)
    operation: str = Field(min_length=1, description="The operation's operationId")
    server: str | None = Field(default=None, min_length=1)
    parameters: dict[str, str] = {}
    headers: dict[str, str] = {}
    body: JsonValue = None

    def refuse_unknown(self, where: str, allowed: tuple[str, ...]) -> None:
        for name, value in [*self.parameters.items(), *self.headers.items()]:
            refuse_unknown(f"{where}'s {name!r}", value, allowed)
        refuse_unknown(f"{where}'s body", self.body, allowed)


InboxRequest = Annotated[TemplateRequest | OperationRequest, Field(discriminator="kind")]


class AsPerson(Model):
    """How Minutehand signs in as a person: headers sent with every call it makes as them. A header naming
    `{person.credential}` makes the people who hold a `Person.credential` the only ones it can act as."""

    headers: dict[str, str] = Field(min_length=1, description="Header name -> template")

    @model_validator(mode="after")
    def _names_known_fields(self) -> Self:
        for name, value in self.headers.items():
            refuse_unknown(f"the as_person header {name!r}", value, PERSON)
        return self

    @property
    def needs_credential(self) -> bool:
        return any("person.credential" in named(v) for v in self.headers.values())


class Paging(Model):
    """A list read page by page: `next` finds the cursor of the next page in an answer (absent, null or empty on the
    last); the cursor is sent as the parameter `param` (a query parameter of a template's URL, or a parameter of the
    operation), or wherever the request names `{page.cursor}`."""

    next: JsonPath
    param: str | None = Field(default=None, min_length=1, description="None: the request places `{page.cursor}`")
    most: int = Field(default=50, ge=1, description="Pages read at most in one reading")


class Listing(Model):
    """How to list what waits on one person: one request, read as that person, and where each item's parts are.

    `waits_on` makes it a list of everyone's items (or a team's): each item names whom it waits on, by email or
    `Person.key`, and only the person read for keeps theirs. Without it the list is that person's own."""

    request: InboxRequest
    items: JsonPath = Field(description="Every node it selects in the answer is one item: `$.items[*]`, or `$[*]`")
    id: JsonPath = Field(description="In an item: its own id")
    summary: JsonPath = Field(description="In an item: what it asks, in words a person reads")
    waits_on: JsonPath | None = Field(default=None, description="In an item: whom it waits on, an email or a key")
    category: JsonPath | None = Field(default=None, description="In an item: its kind in the product's words")
    decisions: JsonPath | None = Field(
        default=None,
        description="In an item: the names of the decisions allowed on it; None: every one declared",
    )
    paging: Paging | None = None

    @model_validator(mode="after")
    def _names_known_fields(self) -> Self:
        paged_in_place = self.paging is not None and self.paging.param is None
        allowed = (*PERSON, CURSOR) if paged_in_place else PERSON
        self.request.refuse_unknown("the list's request", allowed)
        if paged_in_place:
            request = self.request
            spots = (
                [request.url, *request.headers.values()]
                if isinstance(request, TemplateRequest)
                else [*request.parameters.values(), *request.headers.values()]
            )
            if not any(CURSOR in named(spot) for spot in spots):
                raise ValueError("a list paged with no `param` places `{page.cursor}` in its request")
        return self


class DecisionInput(Model):
    """Something a person gives with a decision: a reason, an answer. A request names it as `{input.<name>}`."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, description="What it is; a model-written person reads it")
    required: bool = True


class Succeeds(Model):
    """What a decision's answer looks like when the product took it: one of `statuses` (any 2xx when empty), and,
    with `at`, the JSON answer holding `equals` there."""

    statuses: list[int] = Field(default=[], description="Empty: any 2xx")
    at: JsonPath | None = None
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
    request: InboxRequest
    inputs: list[DecisionInput] = []
    succeeds: Succeeds = Succeeds()

    @model_validator(mode="after")
    def _names_known_fields(self) -> Self:
        names = [i.name for i in self.inputs]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(f"decision {self.name!r} takes an input twice: {', '.join(twice)}")
        self.request.refuse_unknown(f"decision {self.name!r}", (*PERSON, *DECIDING, *(f"input.{n}" for n in names)))
        return self

    @property
    def said(self) -> str:
        return self.reads if self.reads is not None else f"decided {self.name}"

    def refuse_unknown_inputs(self, given: dict[str, str], who: str) -> None:
        """Inputs this decision does not take, refused naming `who` gave them."""
        known = {i.name for i in self.inputs}
        unknown = sorted(set(given) - known)
        if unknown:
            raise ValueError(
                f"{who} gives decision {self.name!r} {', '.join(unknown)}; it takes "
                + (", ".join(sorted(known)) or "nothing")
            )

    def refuse_inputs(self, given: dict[str, str], who: str) -> None:
        """Inputs this decision does not take, or one it requires left out, refused naming `who` gave them."""
        self.refuse_unknown_inputs(given, who)
        missing = sorted(i.name for i in self.inputs if i.required and i.name not in given)
        if missing:
            raise ValueError(f"{who} makes decision {self.name!r} without {', '.join(missing)}, which it requires")


class HttpInbox(Model):
    """One inbox in the agent's own product, reached over HTTP and JSON. Its `name` is what the items seen in it are
    recorded under, as a provider's key or a captured host's name is, and may be neither.

    An MCP inbox (a server whose tools list and settle what waits on a person) would be a second member with its own
    `kind` beside this one; the people, the ledger and the checks read only what is seen, never how."""

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

    def operations(self) -> list[OperationRequest]:
        """Every request it makes by an OpenAPI operation, to be resolved before a run."""
        requests = [self.pending.request, *(d.request for d in self.decisions)]
        return [r for r in requests if isinstance(r, OperationRequest)]


def refuse_repeated_inboxes(inboxes: list[HttpInbox]) -> None:
    names = [i.name for i in inboxes]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise ValueError(f"inbox declared twice: {', '.join(twice)}")


# -- Minutehand's default shape for the agent's side (`document: minutehand`) ----------------------------------------


class PendingItem(Model):
    """One item in the default shape of a list: what `listPending` answers each item as."""

    id: str
    summary: str
    waits_on: str | None = Field(default=None, description="The email of whom it waits on, for a list of everyone's")
    category: str | None = None
    decisions: list[str] | None = Field(default=None, description="The decisions allowed on it; None: every one")


class PendingPage(Model):
    """The default shape of `listPending`'s answer: one page of what waits on the person asked for."""

    items: list[PendingItem]
    next: str | None = Field(default=None, description="The cursor of the next page; None on the last")


class Decider(Model):
    key: str
    email: str
    name: str


class DecisionMade(Model):
    """The default shape of `decide`'s request body: who decided what on which item, and what they gave."""

    item: str = Field(description="The item's own id")
    decision: str
    inputs: dict[str, str] = {}
    person: Decider
    decided_at: str = Field(description="The simulated moment, ISO 8601")


class ListedItem(Model):
    """One item as an inbox listed it, read out of the product's answer."""

    item_id: str
    summary: str
    waits_on: str | None = Field(default=None, description="As the item names them; None: the list is the person's")
    category: str | None = None
    decisions: list[str] | None = Field(default=None, description="As the item lists them; None: it lists none")


class Listed(Model):
    """What one reading of one person's inbox found. `read` False: the product did not answer a list (refused,
    unreachable, not JSON, an item without its id), and nothing can be concluded about what is or is not there."""

    items: list[ListedItem] = []
    read: bool = True
    problem: str | None = None
    contract: str | None = Field(
        default=None, description="Set when the answer departs from the agent's API description: how, naming the field"
    )


class DecideAnswer(Model):
    """What the product answered a decision Minutehand made as a person."""

    accepted: bool
    status: int | None = Field(description="None: nothing answered")
    answer: str = Field(description="The product's answer, as text, cut to 500 characters")
    contract: str | None = Field(
        default=None, description="Set when the answer departs from the agent's API description: how, naming the field"
    )


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
