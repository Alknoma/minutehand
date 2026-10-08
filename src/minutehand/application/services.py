"""Declared services (`docs/services.md`, `docs/design-transitions.md`): each item's state, held and moved only along
the service's machine; every call the agent makes answered from that state and the service's log; its timers and
system actors; its pushes. People respond through the one port (`ports.transitions.ProvidesTransitions`): each
service is a provider of it (`ServiceItems`), and the people engine (`application.people`) plays its responders.

What is Minutehand's and what is the model's:

- Minutehand holds every item's state (`SERVICE_ITEM`), applies a transition only when the machine allows it from
  that state, records every move once (`EntityKind.TRANSITION`), draws who responds to an item from the run's seed,
  and fires timers and system actors at their moments.
- The model proposes, once per run, what the scenario does not declare: the machine (`service-machine/1`) and what
  each route means (`service-route/1`); and it renders each answer and push from the state and the log
  (`service-answer/1`), under the shape the route's first answer (or its OpenAPI document, or a pin) fixed. A
  rendering that shows an item in another state than its own, or departs from the shape, is rejected and rendered
  once more, then refused. A person's response is the engine's (`person-transition/1`).

Every model call goes through the world's record (`Store.written`), and every answer rendered is kept with the
state it was rendered in (`ServiceRecordKind.ANSWER`): polling a service whose state has not changed answers from
that record and calls no model, in a run, a rerun or a fork.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from minutehand.application.checkpoint import PendingService
from minutehand.application.refusals import RunRefused
from minutehand.application.replier import context_key
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.common import ProviderKey, Window
from minutehand.domain.conversation import ModelMessage, PersonCall, Speaker, Wrote
from minutehand.domain.model import Model
from minutehand.domain.outbound import IdFormat
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.services import ROUTE, Machine, MachineTransition, Meaning, RouteMeaning, Service, Trigger
from minutehand.domain.shapes import problems, shape_of, strings_in, values_at
from minutehand.domain.transitions import Offer, OfferField, Transition, Waiting, transition_change
from minutehand.domain.world import (
    Actor,
    AnsweredBy,
    Change,
    EntityKind,
    EntityRef,
    Operation,
    PushSnapshot,
    RecordSource,
    ServiceEventKind,
    ServiceEventSnapshot,
    ServiceItemSnapshot,
    ServiceRecordKind,
    ServiceRecordSnapshot,
    Stored,
    TransitionSnapshot,
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.model import ModelFailed
from minutehand.ports.services import Call, DeliversPushes, ServiceAnswer
from minutehand.ports.store import Store

MACHINE_PROMPT_VERSION = "service-machine/1"
ROUTE_PROMPT_VERSION = "service-route/1"
ANSWER_PROMPT_VERSION = "service-answer/1"

AnswerT = TypeVar("AnswerT", bound=BaseModel)

LOG_SHOWN = 200
"""Entries of a service's log shown to the model, the latest kept."""

CREATE = "create"
"""The name of the move that files an item, from no state into the machine's first."""
NOTE = "note"
"""What a person's response carries besides the fields its transition requires: their words, if any."""

_IDS = uuid.UUID("0b8d7c1e-4f2a-4e57-9c3d-5a6b7c8d9e0f")
WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_OPENAPI_METHODS = ("get", "post", "put", "patch", "delete")  # enum-lint: exempt OpenAPI's own path-item keys
_SCAN = 500
_ACTOR = {
    Trigger.AGENT: Actor.AGENT,
    Trigger.PERSON: Actor.PERSON,
    Trigger.TIMER: Actor.TIMER,
    Trigger.SYSTEM: Actor.SYSTEM,
}
_CONTENT: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])

MACHINE_PROMPT = """\
You describe a web service a program under test files things with, so that a simulation can hold the state of what \
is filed there. People respond through the service; the program sees their responses only through the service.

Write the service's state machine as JSON in "machine": {{"initial": <state>, "states": [<state>...], \
"transitions": [{{"name": <name>, "from": [<state>...], "to": <state>, "by": "agent" | "person" | "timer" | \
"system", "actor": <name, for system only, else omit>, "after": <ISO 8601 duration, for timer and system only>, \
"requires": [<field the call or response must carry>...]}}...]}}.
- An item is created in "initial". "agent" transitions are what the program may ask for; "person" transitions are \
how the people who respond through the service respond; "timer" and "system" ones happen by themselves.
- Use the service's own words for states, as its answers would show them (a status field's values).
- Every state but the final ones has a way out. Keep it to what the description and the calls imply.

What the service is: {describe}
"""

ROUTE_PROMPT = """\
You read one route of a web service, for a simulation that holds the service's state. Say what a call to it does.

"means": one of "create" (files a new item), "transition" (asks the service to move an item it names along one of \
the transitions below), "update" (changes what an item says, not its state), "read" (changes nothing), "subscribe" \
(registers an address the service will call), "other".
"transition": for "transition", the transition's name exactly as listed, else null. Only "agent" transitions.
"item_at": where its answer shows an item's id, as a dotted path ("id", "items[*].id", "data.request.id"), or null.
"state_at": where its answer shows that item's state, beside the id ("status", "items[*].status"), or null.
"url_at": for "subscribe", where the call's body holds the address, as a dotted path, else null.

What the service is: {describe}
Its items' states: {states}
Its transitions:
{transitions}
"""

ANSWER_PROMPT = """\
You are the web service at {host}, answering a program under test. A simulation holds your state: the items filed \
with you, the state each is in, and your log of everything that happened. You write the HTTP answer to one call, \
from that state and that log, and from nothing else.

What the service is: {describe}

Rules:
- "status": the HTTP status you answer with. "body": the body, as JSON text.
- Every item you show is in exactly the state listed for it below, spelled as listed. You never show a state, an \
item, a response or a field the state and the log do not hold.
- {shaped}
- Show people's responses the way your API would: a status field, a list of responses, events, a job's result.
"""


class WrittenMachine(Model):
    """A service's state machine, as the model proposes it: JSON text of a `domain.services.Machine`."""

    machine: str


class WrittenRoute(Model):
    """What one route of a service means, as the model reads it."""

    means: str
    transition: str | None
    item_at: str | None
    state_at: str | None
    url_at: str | None


class WrittenAnswer(Model):
    """What a service answers a call, or pushes: a status and a JSON body as text."""

    status: int
    body: str


@dataclass(frozen=True)
class Item:
    id: str
    state: str
    entered: int
    """The seq of the version that put it in its state."""
    number: int
    """Its place among the service's items, from 1, in the order filed."""


@dataclass(frozen=True)
class Logged:
    """One line of a service's log: a move of an item, or a write that moved none."""

    seq: int
    line: str
    item: str | None
    content: str
    create: bool


def item_ref(service: ProviderKey, item: str) -> EntityRef:
    return EntityRef(provider=service, kind=EntityKind.SERVICE_ITEM, external_id=item)


class ServiceDesk:
    """Every service a run or a standing world declares, over its world's log."""

    def __init__(
        self,
        scenario: Scenario,
        model: LanguageModel | None,
        *,
        documents: Mapping[str, JsonValue] | None = None,
        pushes: DeliversPushes | None = None,
        undeclared: bool = False,
    ) -> None:
        """`undeclared` answers every host no provider claims and nothing declares as a service with no description
        (`--capture-unknown model`), its machine proposed from the agent's first call to it."""
        if (scenario.services or undeclared) and model is None:
            named = ", ".join(s.key for s in scenario.services) or "hosts nobody declared"
            raise RunRefused(f"a model renders what {named} answers, and no model is configured")
        self.scenario = scenario
        self._model = model
        self._documents = dict(documents or {})
        self._pushes = pushes
        self._undeclared = undeclared
        self._people = {p.key: p for p in scenario.people}

    def _document(self, service: Service) -> JsonValue:
        return self._documents[service.key] if service.key in self._documents else None

    # -- what the proxy asks ---------------------------------------------------------------------------------------

    def declared(self, host: str) -> Service | None:
        """The service the scenario declares at `host`, if any."""
        return next((s for s in self.scenario.services if s.host == host.lower()), None)

    def undeclared(self, host: str) -> Service:
        """`host`, which nobody declared, as a service with no description and no responders: its machine proposed
        from the agent's first call to it, its answers rendered from what the agent did there."""
        return Service(host=host.lower(), responders=[], within=Window(min=timedelta(0), max=timedelta(0)))

    @property
    def answers_undeclared(self) -> bool:
        return self._undeclared

    def by_key(self, key: ProviderKey) -> Service:
        found = next((s for s in self.scenario.services if s.key == key), None)
        if found is None:
            raise LookupError(f"the scenario declares no service {key}")
        return found

    def provider(self, key: ProviderKey) -> ServiceItems:
        """The service `key` as a provider of the transitions port, which the people engine plays."""
        return ServiceItems(self, self.by_key(key))

    async def answer(self, service: Service, call: Call, world: Store, clock: Clock) -> ServiceAnswer:
        """Apply what the call asks, if the machine allows it, then render the answer from the state and the log.
        A model that fails, or renders twice what the state contradicts, is answered 502 naming it."""
        try:
            return await self._answer(service, call, world, clock)
        except ModelFailed as e:
            return _failed(service, f"the model rendering this service failed: {e}")

    async def _answer(self, service: Service, call: Call, world: Store, clock: Clock) -> ServiceAnswer:
        machine = await self.machine(service, world, clock, first=call)
        route, item_id = self._route(service, call, world)
        meaning = await self._meaning(service, route, call, machine, world, clock)
        writing = call.method.upper() in WRITES
        item = self._item(service, item_id, world) if item_id is not None else None
        content = _json_text(call.body)
        created: str | None = None
        if writing and meaning.means is Meaning.CREATE:
            created = self._new_id(service, world)
            self._create(service, machine, created, content, world, clock)
        elif writing and meaning.means is Meaning.TRANSITION:
            if item is None:
                return await self._refuse(service, call, route, f"no item {item_id} is filed here", world, clock)
            transition = machine.transition(meaning.transition or "")
            assert transition is not None
            why = _illegal(transition, item.state, content)
            if why is not None:
                return await self._refuse(service, call, route, why, world, clock)
            self.move(service, item, transition, Actor.AGENT, None, content, world, clock)
        elif writing and meaning.means is Meaning.UPDATE and item is None and item_id is not None:
            return await self._refuse(service, call, route, f"no item {item_id} is filed here", world, clock)
        elif writing:
            kinds = {Meaning.UPDATE: ServiceEventKind.UPDATE, Meaning.SUBSCRIBE: ServiceEventKind.SUBSCRIBE}
            kind = kinds[meaning.means] if meaning.means in kinds else ServiceEventKind.OTHER
            self._log(service, kind, item.id if item is not None else None, content, route, world)
        return await self._render(service, machine, meaning, call, created, world, clock)

    async def filed(self, service: Service, item: str, call: Call, route: str, world: Store, clock: Clock) -> None:
        """An item the agent filed through one of the service's `collections`: it enters the machine's first state."""
        del route
        machine = await self.machine(service, world, clock, first=call)
        self._create(service, machine, item, _json_text(call.body), world, clock)

    # -- the machine and the routes, fixed once ----------------------------------------------------------------

    def kept_machine(self, service: Service, world: Store) -> Machine | None:
        """The machine the run fixed for the service, if it has needed one yet."""
        kept = self._record(service, "machine", world)
        return Machine.model_validate_json(kept.text) if kept is not None else None

    async def machine(self, service: Service, world: Store, clock: Clock, *, first: Call | None = None) -> Machine:
        """The service's machine: kept in the log the first time it is needed, declared, read from its OpenAPI
        document, or proposed by the model from its description and the agent's first call."""
        kept = self.kept_machine(service, world)
        if kept is not None:
            return kept
        if service.machine is not None:
            declared = service.machine.model_dump_json()
            self._keep(service, ServiceRecordKind.MACHINE, None, declared, RecordSource.DECLARED, world)
            return service.machine
        document = self._document(service)
        shown = f"The program's first call to it:\n{_call_text(first)}" if first is not None else ""
        if document is not None:
            shown = f"The service's OpenAPI document:\n{_cut(json.dumps(document), 20000)}\n\n{shown}"
        system = MACHINE_PROMPT.format(describe=service.describe or "(no description; read it from the calls)")
        error: str | None = None
        for _ in range(2):
            messages = [ModelMessage(speaker=Speaker.ASKER, text=shown or "(no call yet)")]
            if error is not None:
                messages.append(ModelMessage(speaker=Speaker.ASKER, text=f"Your last machine was refused: {error}"))
            written = await self._ask(
                service, Wrote.SERVICE_MACHINE, system, messages, WrittenMachine, MACHINE_PROMPT_VERSION, world, clock
            )
            try:
                machine = Machine.model_validate_json(written.machine)
            except ValidationError as e:
                error = str(e)
                continue
            unknown = set(service.odds) - {t.name for t in machine.taken_by(Trigger.PERSON)}
            if unknown:
                error = f"the declared odds name {', '.join(sorted(unknown))}, which no person may take"
                continue
            source = RecordSource.OPENAPI if document is not None else RecordSource.MODEL
            self._keep(service, ServiceRecordKind.MACHINE, None, machine.model_dump_json(), source, world)
            return machine
        raise ModelFailed(f"the model proposed no machine for {service.key} that holds: {error}")

    def _route(self, service: Service, call: Call, world: Store) -> tuple[str, str | None]:
        """The call's route (a method and a template) and the item it names: a declared or documented template
        that matches, else the path with every id the service gave an item read as `{id}`."""
        method = call.method.upper()
        path = urlsplit(call.path).path.rstrip("/") or "/"
        segments = path.split("/")
        ids = {i.id for i in self.items(service, world)}
        for template in self._templates(service):
            found = _matched(template, segments)
            if found is not None:
                named = next((v for v in found.values() if v in ids), None)
                return f"{method} {template}", named or next(iter(found.values()), None)
        named = next((s for s in segments if s in ids), None)
        templated = "/".join("{id}" if s in ids else s for s in segments)
        return f"{method} {templated}", named

    def _templates(self, service: Service) -> list[str]:
        declared = [m["path"] for r in service.routes if (m := ROUTE.match(r.route)) is not None]
        documented = list(_operations(self._document(service)))
        return [t for t in dict.fromkeys([*declared, *(p for p, _ in documented)]) if "{" in t]

    async def _meaning(
        self, service: Service, route: str, call: Call, machine: Machine, world: Store, clock: Clock
    ) -> RouteMeaning:
        kept = self._record(service, f"route:{route}", world)
        if kept is not None:
            return RouteMeaning.model_validate_json(kept.text)
        pinned = next((r for r in service.routes if r.route == route), None)
        operation = _operation(self._document(service), route)
        shape = pinned.shape if pinned is not None and pinned.shape is not None else _answer_schema(operation)
        if pinned is not None and pinned.means is not None:
            meaning = RouteMeaning(
                route=route,
                means=pinned.means,
                transition=pinned.transition,
                item_at=pinned.item_at,
                state_at=pinned.state_at,
                url_at=pinned.url_at,
                shape=shape,
            )
            self._keep(service, ServiceRecordKind.ROUTE, route, meaning.model_dump_json(), RecordSource.DECLARED, world)
            return meaning
        system = ROUTE_PROMPT.format(
            describe=service.describe or "(none given)",
            states=", ".join(machine.states),
            transitions="\n".join(_transition_line(t) for t in machine.transitions) or "(none)",
        )
        shown = f"The route: {route}\nA call to it:\n{_call_text(call)}"
        if operation is not None:
            shown += f"\n\nThe route in the service's OpenAPI document:\n{_cut(json.dumps(operation), 8000)}"
        error: str | None = None
        for _ in range(2):
            messages = [ModelMessage(speaker=Speaker.ASKER, text=shown)]
            if error is not None:
                messages.append(ModelMessage(speaker=Speaker.ASKER, text=f"Your last reading was refused: {error}"))
            written = await self._ask(
                service, Wrote.SERVICE_ROUTE, system, messages, WrittenRoute, ROUTE_PROMPT_VERSION, world, clock
            )
            try:
                means = Meaning(written.means)
                meaning = RouteMeaning(
                    route=route,
                    means=means,
                    transition=written.transition if means is Meaning.TRANSITION else None,
                    item_at=written.item_at or None,
                    state_at=written.state_at if written.item_at else None,
                    url_at=written.url_at or None,
                    shape=shape,
                )
            except (ValidationError, ValueError) as e:
                error = str(e)
                continue
            if meaning.means is Meaning.TRANSITION:
                transition = machine.transition(meaning.transition or "")
                if transition is None or transition.by is not Trigger.AGENT:
                    error = f"{meaning.transition!r} is not a transition the program may ask for"
                    continue
            source = RecordSource.OPENAPI if operation is not None else RecordSource.MODEL
            self._keep(service, ServiceRecordKind.ROUTE, route, meaning.model_dump_json(), source, world)
            return meaning
        raise ModelFailed(f"the model read no meaning for {route} on {service.key} that holds: {error}")

    # -- items, moves and the log ------------------------------------------------------------------------------

    def items(self, service: Service, world: Store) -> list[Item]:
        """Every item filed with the service, in the order filed, as it stands at the head of the log."""
        held: list[Stored] = []
        after: str | None = None
        while True:
            page = world.children(service.key, EntityKind.SERVICE_ITEM, service.key, after=after, limit=_SCAN)
            held += page
            if len(page) < _SCAN:
                break
            after = page[-1].entity.external_id
        firsts = sorted(held, key=lambda s: world.versions(s.entity)[0].seq)
        found: list[Item] = []
        for number, stored in enumerate(firsts, start=1):
            snapshot = ServiceItemSnapshot.model_validate_json(stored.body)
            found.append(Item(id=snapshot.item, state=snapshot.state, entered=stored.seq, number=number))
        return found

    def _item(self, service: Service, item: str, world: Store) -> Item | None:
        return next((i for i in self.items(service, world) if i.id == item), None)

    def item(self, service: Service, item: str, world: Store) -> Item:
        found = self._item(service, item, world)
        if found is None:
            raise LookupError(f"no item {item} is filed with {service.key}")
        return found

    def log(self, service: Service, world: Store) -> list[Logged]:
        """The service's log, oldest first: every move of its items, and every write that moved none."""
        found: list[Logged] = []
        for e in world.events():
            if e.entity.provider != service.key:
                continue
            after = e.after
            if isinstance(after, TransitionSnapshot):
                moved = f"{after.from_state or '-'} -> {after.to_state}"
                who = after.who or e.actor.value
                said = f"- {after.name} {moved} item {after.item.external_id} by {who}: {_cut(after.content, 2000)}"
                found.append(
                    Logged(
                        seq=e.seq,
                        line=said,
                        item=after.item.external_id,
                        content=after.content,
                        create=after.from_state is None,
                    )
                )
            elif isinstance(after, ServiceEventSnapshot):
                said = f"- {after.event.value} item {after.item or '-'} by agent: {_cut(after.content, 2000)}"
                found.append(Logged(seq=e.seq, line=said, item=after.item, content=after.content, create=False))
        return found

    def _new_id(self, service: Service, world: Store) -> str:
        seq = world.head() + 1
        if service.ids.format is IdFormat.PREFIXED:
            return f"{service.ids.prefix}{seq}"
        if service.ids.format is IdFormat.INTEGER:
            return str(seq)
        return str(uuid.uuid5(_IDS, f"{service.host}#{seq}"))

    def _create(self, service: Service, machine: Machine, item: str, content: str, world: Store, clock: Clock) -> None:
        self._set_state(service, item, machine.initial, world, Actor.AGENT, Operation.CREATE)
        self._record_move(service, item, CREATE, None, machine.initial, Actor.AGENT, None, content, world, clock)

    def move(
        self,
        service: Service,
        item: Item,
        transition: MachineTransition,
        by: Actor,
        who: str | None,
        content: str,
        world: Store,
        clock: Clock,
    ) -> Transition:
        """Move the item along `transition`, as `by` (`who`: a person's key, or a system actor's name), recording
        the item's new state and the move once."""
        self._set_state(service, item.id, transition.to, world, by, Operation.UPDATE)
        return self._record_move(
            service, item.id, transition.name, item.state, transition.to, by, who, content, world, clock
        )

    def _record_move(
        self,
        service: Service,
        item: str,
        name: str,
        from_state: str | None,
        to_state: str,
        by: Actor,
        who: str | None,
        content: str,
        world: Store,
        clock: Clock,
    ) -> Transition:
        moved = Transition(
            provider=service.key,
            item=item_ref(service.key, item),
            name=name,
            from_state=from_state,
            to_state=to_state,
            by=by,
            who=who,
            content=content,
            at=clock.now(),
        )
        event = world.apply(transition_change(moved, at_seq=world.head() + 1))
        return moved.model_copy(update={"seq": event.seq})

    def _set_state(
        self, service: Service, item: str, state: str, world: Store, actor: Actor, operation: Operation
    ) -> None:
        snapshot = ServiceItemSnapshot(service=service.key, item=item, state=state)
        world.apply(
            Change(
                entity=item_ref(service.key, item),
                operation=operation,
                actor=actor,
                body=snapshot.model_dump_json(),
                parent=service.key,
                after=snapshot,
            )
        )

    def _log(
        self, service: Service, kind: ServiceEventKind, item: str | None, content: str, route: str, world: Store
    ) -> None:
        snapshot = ServiceEventSnapshot(service=service.key, event=kind, item=item, content=content, route=route)
        world.apply(
            Change(
                entity=EntityRef(
                    provider=service.key, kind=EntityKind.SERVICE_EVENT, external_id=f"event:{world.head() + 1}"
                ),
                operation=Operation.CREATE,
                actor=Actor.AGENT,
                body=snapshot.model_dump_json(),
                parent=service.key,
                after=snapshot,
            )
        )

    def _record(self, service: Service, name: str, world: Store) -> ServiceRecordSnapshot | None:
        held = world.get(EntityRef(provider=service.key, kind=EntityKind.SERVICE_RECORD, external_id=name))
        return ServiceRecordSnapshot.model_validate_json(held.body) if held is not None else None

    def _keep(
        self,
        service: Service,
        record: ServiceRecordKind,
        route: str | None,
        text: str,
        source: RecordSource,
        world: Store,
        *,
        name: str | None = None,
    ) -> None:
        if name is None:
            name = "machine" if record is ServiceRecordKind.MACHINE else "errors" if route is None else f"route:{route}"
        snapshot = ServiceRecordSnapshot(service=service.key, record=record, route=route, text=text, source=source)
        ref = EntityRef(provider=service.key, kind=EntityKind.SERVICE_RECORD, external_id=name)
        world.apply(
            Change(
                entity=ref,
                operation=Operation.CREATE if world.get(ref) is None else Operation.UPDATE,
                actor=Actor.SCENARIO,
                body=snapshot.model_dump_json(),
                parent=service.key,
                after=snapshot,
            )
        )

    # -- rendering ---------------------------------------------------------------------------------------------

    async def _render(
        self,
        service: Service,
        machine: Machine,
        meaning: RouteMeaning,
        call: Call,
        created: str | None,
        world: Store,
        clock: Clock,
    ) -> ServiceAnswer:
        """The answer to a call, rendered from the state and the log; the same call in the same state is answered
        from the record of its first rendering, with no model call."""
        state = self._state_text(service, machine, world)
        asked = f"The call to answer now ({meaning.route}, which {_what(meaning)}):\n{_call_text(call)}"
        if created is not None:
            asked += f"\n\nThis call filed the new item {created}; it is in state {machine.initial}."
        rendered = f"answer:{_digest([meaning.route, call.method.upper(), call.path, call.body or '', state])}"
        kept = self._record(service, rendered, world) if created is None else None
        if kept is not None:
            again = WrittenAnswer.model_validate_json(kept.text)
            return ServiceAnswer(status=again.status, body=again.body, answered_by=AnsweredBy.MODEL)
        written, why = await self._rendered(
            service, meaning.shape, meaning, state, asked, world, clock, ok=range(200, 300)
        )
        if written is None:
            return _failed(service, f"the answer to {meaning.route} could not be rendered: {why}")
        if meaning.shape is None and written.body.strip():
            fixed = meaning.model_copy(update={"shape": shape_of(json.loads(written.body))})
            route_kept = self._record(service, f"route:{meaning.route}", world)
            source = route_kept.source if route_kept is not None else RecordSource.MODEL
            self._keep(service, ServiceRecordKind.ROUTE, meaning.route, fixed.model_dump_json(), source, world)
        if created is None:
            self._keep(
                service,
                ServiceRecordKind.ANSWER,
                meaning.route,
                written.model_dump_json(),
                RecordSource.MODEL,
                world,
                name=rendered,
            )
        return ServiceAnswer(status=written.status, body=written.body, answered_by=AnsweredBy.MODEL)

    async def _refuse(
        self, service: Service, call: Call, route: str, why: str, world: Store, clock: Clock
    ) -> ServiceAnswer:
        """The service's refusal of a call the machine does not allow, in its error shape: fixed by its document's
        error answers, else by its first refusal."""
        machine = await self.machine(service, world, clock)
        kept = self._record(service, "errors", world)
        shape: JsonValue = json.loads(kept.text) if kept is not None else _error_schema(self._document(service))
        asked = (
            f"The call ({route}):\n{_call_text(call)}\n\nYou refuse it, because {why}. Write your error answer: a "
            "4xx status and your error body."
        )
        state = self._state_text(service, machine, world)
        written, failure = await self._rendered(service, shape, None, state, asked, world, clock, ok=range(400, 500))
        if written is None:
            return _failed(service, f"refused ({why}), and the refusal could not be rendered: {failure}")
        if kept is None:
            fixed = shape if shape is not None else shape_of(json.loads(written.body))
            source = RecordSource.OPENAPI if shape is not None else RecordSource.MODEL
            self._keep(service, ServiceRecordKind.ERRORS, None, json.dumps(fixed), source, world)
        return ServiceAnswer(status=written.status, body=written.body, answered_by=AnsweredBy.MODEL, note=why)

    async def _rendered(
        self,
        service: Service,
        shape: JsonValue,
        meaning: RouteMeaning | None,
        state: str,
        asked: str,
        world: Store,
        clock: Clock,
        *,
        ok: range,
    ) -> tuple[WrittenAnswer | None, str]:
        """The model's rendering, checked against the shape and the state; once more when it fails either."""
        shaped = (
            f"Your body conforms to this JSON Schema: {json.dumps(shape)}"
            if shape is not None
            else "Your body has the shape your API gives this route; it is fixed from this answer on."
        )
        system = ANSWER_PROMPT.format(host=service.host, describe=service.describe or "(none given)", shaped=shaped)
        error: str | None = None
        for _ in range(2):
            messages = [ModelMessage(speaker=Speaker.ASKER, text=f"{state}\n\n{asked}")]
            if error is not None:
                messages.append(ModelMessage(speaker=Speaker.ASKER, text=f"Your last answer was refused: {error}"))
            written = await self._ask(
                service, Wrote.SERVICE_ANSWER, system, messages, WrittenAnswer, ANSWER_PROMPT_VERSION, world, clock
            )
            error = self._contradiction(service, written, shape, meaning, world, ok)
            if error is None:
                return written, ""
        return None, error or ""

    def _contradiction(
        self,
        service: Service,
        written: WrittenAnswer,
        shape: JsonValue,
        meaning: RouteMeaning | None,
        world: Store,
        ok: range,
    ) -> str | None:
        if written.status not in ok:
            return f"status {written.status} is not one of {ok.start}-{ok.stop - 1}"
        if not written.body.strip():
            return None if shape is None else "the body is empty, and the route's answers have a shape"
        try:
            body: JsonValue = json.loads(written.body)
        except json.JSONDecodeError as e:
            return f"the body is not JSON: {e}"
        if shape is not None:
            departs = problems(body, shape, self._document(service))
            if departs:
                return "it departs from the route's shape: " + "; ".join(departs[:8])
        if meaning is not None and meaning.item_at is not None and meaning.state_at is not None:
            states = {i.id: i.state for i in self.items(service, world)}
            ids = values_at(body, meaning.item_at)
            shown = values_at(body, meaning.state_at)
            for item, state in zip(ids, shown, strict=False):
                if (
                    isinstance(item, str | int)
                    and str(item) in states
                    and str(state).casefold() != states[str(item)].casefold()
                ):
                    return f"it shows item {item} as {state!r}, and it is {states[str(item)]!r}"
        return None

    def _state_text(self, service: Service, machine: Machine, world: Store) -> str:
        items = self.items(service, world)
        log = self.log(service, world)[-LOG_SHOWN:]
        lines = [f"The items filed with you and the state each is in (states: {', '.join(machine.states)}):"]
        lines += [f"- {i.id}: {i.state}" for i in items] or ["- (none yet)"]
        lines.append("\nYour log, oldest first:")
        lines += [e.line for e in log] or ["- (nothing yet)"]
        return "\n".join(lines)

    # -- what the service does by itself -----------------------------------------------------------------------

    def bookings(self, event: WorldEvent, world: Store) -> list[PendingService]:
        """What the service owes an item that just entered a state: each timer's and system actor's transition out
        of it. A responder's response is the people engine's."""
        after = event.after
        if not isinstance(after, ServiceItemSnapshot) or not any(
            s.key == after.service for s in self.scenario.services
        ):
            return []
        service = self.by_key(after.service)
        machine = self.kept_machine(service, world)
        if machine is None:
            return []
        booked: list[PendingService] = []
        ref = f"service:{service.key}:{after.item}:{event.seq}"
        for transition in [*machine.legal(after.state, Trigger.TIMER), *machine.legal(after.state, Trigger.SYSTEM)]:
            if transition.after is not None:
                at = event.sim_time + transition.after
            else:
                assert transition.within is not None
                span = transition.within.max - transition.within.min
                drawn = timedelta(seconds=self.drawn(service, after.item, event.seq, transition.name, span))
                at = event.sim_time + transition.within.min + drawn
            booked.append(
                PendingService(
                    due=Due(at=at, kind=DueKind.SERVICE, ref=f"{ref}:{transition.name}"),
                    service=service.key,
                    item=after.item,
                    entered=event.seq,
                    transition=transition.name,
                )
            )
        return booked

    async def fire(self, pending: PendingService, world: Store, clock: Clock) -> None:
        """A timer's or system actor's transition, if the item is still in the state it was booked from."""
        service = self.by_key(pending.service)
        machine = await self.machine(service, world, clock)
        item = self._item(service, pending.item, world)
        transition = machine.transition(pending.transition)
        if item is None or item.entered != pending.entered or transition is None or item.state not in transition.from_:
            return
        self.move(service, item, transition, _ACTOR[transition.by], transition.actor, "{}", world, clock)
        await self.push(service, machine, self.item(service, item.id, world), world, clock)

    def heard(self, pending: PendingService, world: Store) -> bool:
        """Whether a timer's or system's move pushes to the agent: an address it gave for the item or the service."""
        return bool(self.targets(self.by_key(pending.service), pending.item, world))

    def drawn(self, service: Service, item: str, entered: int, salt: str, span: timedelta) -> int:
        said = f"{self.scenario.seed}|{service.key}|{item}|{entered}|{salt}"
        digest = hashlib.sha256(said.encode()).digest()
        return int.from_bytes(digest[:8], "big") % (int(span.total_seconds()) + 1)

    def responder(self, service: Service, item: Item, world: Store) -> Person | None:
        """Who responds to the item in its state: drawn by the seed, the service, the item and the state's entry
        from the responders the item names (an email or a key anywhere in what the agent filed), else from all of
        them. With no responders, nobody does."""
        filed = [e for e in self.log(service, world) if e.item == item.id and e.create]
        said = " ".join(strings_in(json.loads(filed[0].content))).casefold() if filed else ""
        named = [k for k in service.responders if self._people[k].email.casefold() in said or k in said.split()]
        among = named or service.responders
        if not among:
            return None
        n = self.drawn(service, item.id, item.entered, "responder", timedelta(seconds=len(among) - 1))
        return self._people[among[n]]

    def targets(self, service: Service, item: str, world: Store) -> list[str]:
        """Where the service pushes news of the item: each http(s) address in the agent's create of it (a callback),
        and each the agent subscribed to the service."""
        found: list[str] = []
        for event in world.events():
            after = event.after
            if event.entity.provider != service.key:
                continue
            content: str | None = None
            if (isinstance(after, ServiceEventSnapshot) and after.event is ServiceEventKind.SUBSCRIBE) or (
                isinstance(after, TransitionSnapshot) and after.from_state is None and after.item.external_id == item
            ):
                content = after.content
            if content is not None:
                found += [s for s in strings_in(json.loads(content)) if re.match(r"^https?://", s)]
        return list(dict.fromkeys(found))

    async def push(self, service: Service, machine: Machine, item: Item, world: Store, clock: Clock) -> None:
        """Tell every address the agent gave of the item's move, the body rendered from the state and the log."""
        if self._pushes is None:
            return
        for url in self.targets(service, item.id, world):
            asked = f"Push news of item {item.id}, now {item.state}, to {url}: write the body you POST there."
            route = f"POST {url}"
            kept = self._record(service, f"route:{route}", world)
            shape: JsonValue = RouteMeaning.model_validate_json(kept.text).shape if kept is not None else None
            meaning = RouteMeaning(route=f"POST {urlsplit(url).path or '/'}", means=Meaning.OTHER, shape=shape)
            state = self._state_text(service, machine, world)
            try:
                written, why = await self._rendered(
                    service, shape, meaning, state, asked, world, clock, ok=range(200, 300)
                )
            except ModelFailed as e:
                written, why = None, f"the model failed: {e}"
            status: int | None = None
            failure: str | None
            if written is None:
                body, failure = "", f"the push could not be rendered: {why}"
            else:
                body = written.body
                if kept is None and body.strip():
                    fixed = RouteMeaning(route=meaning.route, means=Meaning.OTHER, shape=shape_of(json.loads(body)))
                    text = fixed.model_dump_json()
                    self._keep(service, ServiceRecordKind.ROUTE, route, text, RecordSource.MODEL, world)
                delivered = await self._pushes.push(url, body)
                status, failure = delivered.status, delivered.failure
            snapshot = PushSnapshot(
                service=service.key, item=item.id, url=url, body=body, status=status, failure=failure
            )
            world.apply(
                Change(
                    entity=EntityRef(
                        provider=service.key, kind=EntityKind.PUSH, external_id=f"push:{world.head() + 1}"
                    ),
                    operation=Operation.CREATE,
                    actor=Actor.SCENARIO,
                    body=snapshot.model_dump_json(),
                    parent=service.key,
                    after=snapshot,
                )
            )

    def odds(self, service: Service, item: Item, legal: Sequence[MachineTransition]) -> MachineTransition | None:
        """The person transition the service's odds draw for the item in its state, seeded; None without odds."""
        weighed = [(t, service.odds[t.name]) for t in legal if t.name in service.odds]
        total = sum(w for _, w in weighed)
        if not weighed or total <= 0:
            return None
        said = f"{self.scenario.seed}|{service.key}|{item.id}|{item.entered}|odds"
        point = int.from_bytes(hashlib.sha256(said.encode()).digest()[:8], "big") / 2**64 * total
        for transition, weight in weighed:
            if point < weight:
                return transition
            point -= weight
        return weighed[-1][0]

    # -- the model, through the world's record -------------------------------------------------------------------

    async def _ask(
        self,
        service: Service,
        wrote: Wrote,
        system: str,
        messages: Sequence[ModelMessage],
        answer: type[AnswerT],
        prompt_version: str,
        world: Store,
        clock: Clock,
    ) -> AnswerT:
        model = self._model
        assert model is not None
        named = model.model_id
        key = context_key(named, prompt_version, system, messages, answer.__name__, 0)
        call = {
            "key": key,
            "person": None,
            "service": service.key,
            "wrote": wrote,
            "model": named,
            "prompt_version": prompt_version,
            "sim_time": clock.now(),
            "wake": clock.wake(),
        }
        kept = world.written(key)
        if kept is not None:
            world.record_person_call(PersonCall.model_validate({**call, "answer": kept, "replayed": True}))
            return answer.model_validate_json(kept)
        try:
            answered = await model.answer(system, messages, answer, model=named, temperature=0)
        except ModelFailed as e:
            world.record_person_call(PersonCall.model_validate({**call, "failure": str(e)}))
            raise
        world.record_person_call(
            PersonCall.model_validate(
                {
                    **call,
                    "answer": answered.answer.model_dump_json(),
                    "input_tokens": answered.input_tokens,
                    "output_tokens": answered.output_tokens,
                }
            )
        )
        return answered.answer


class ServiceItems:
    """One declared service as a provider of the transitions port (`ports.transitions.ProvidesTransitions`), which
    the people engine plays: an item waits on the responder drawn for it in a state a person may move it from; the
    offers are the machine's person transitions from that state; a response is a move by that person, and the
    service pushes news of it. Also `SteersPeople`: the service's `within`, `bias` and `odds`."""

    def __init__(self, desk: ServiceDesk, service: Service) -> None:
        self._desk = desk
        self.service = service

    def _machine(self, world: Store) -> Machine | None:
        return self._desk.kept_machine(self.service, world)

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        machine = self._machine(world)
        if machine is None:
            return []
        waiting: list[Waiting] = []
        for item in self._desk.items(self.service, world):
            if not machine.legal(item.state, Trigger.PERSON):
                continue
            drawn = self._desk.responder(self.service, item, world)
            if drawn is None or drawn.key != person.key:
                continue
            ref = item_ref(self.service.key, item.id)
            waiting.append(Waiting(item=ref, state=item.state, shown=self._shown(item, world)))
        return waiting

    def _shown(self, item: Item, world: Store) -> str:
        history = [e.line for e in self._desk.log(self.service, world) if e.item == item.id]
        what = self.service.describe or f"the service at {self.service.host}"
        told = f"The item {item.id}, in state {item.state}. What has happened to it, oldest first:"
        return "\n".join([what, told, *history])

    def _located(self, item: EntityRef, world: Store) -> Item:
        if item.provider != self.service.key or item.kind is not EntityKind.SERVICE_ITEM:
            raise ValueError(f"{item.provider} {item.kind.value} {item.external_id} is no item of {self.service.key}")
        return self._desk.item(self.service, item.external_id, world)

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        del who
        held = self._located(item, world)
        machine = self._machine(world)
        if machine is None or by is not Actor.PERSON:
            return []
        return [
            Offer(
                name=t.name,
                to_state=t.to,
                fields=[
                    *(
                        OfferField(name=f, required=True, description=f"what the service needs: {f}")
                        for f in t.requires
                    ),
                    OfferField(name=NOTE, description="what you say with it, in your words"),
                ],
            )
            for t in machine.legal(held.state, Trigger.PERSON)
        ]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        held = self._located(item, world)
        machine = await self._desk.machine(self.service, world, clock)
        transition = machine.transition(offer)
        if by is not Actor.PERSON or who is None or transition is None or transition.by is not Trigger.PERSON:
            raise ValueError(f"{self.service.key} offers no person's transition {offer!r}")
        try:
            _CONTENT.validate_json(content)
        except ValidationError as e:
            raise ValueError(f"a response's content is a JSON object of text fields: {content!r}") from e
        why = _illegal(transition, held.state, content)
        if why is not None:
            raise ValueError(why)
        moved = self._desk.move(self.service, held, transition, Actor.PERSON, who.key, content, world, clock)
        await self._desk.push(self.service, machine, self._desk.item(self.service, held.id, world), world, clock)
        return moved

    def heard_of(self, item: EntityRef, world: Store, clock: Clock) -> bool:
        del clock
        return bool(self._desk.targets(self.service, item.external_id, world))

    def within(self, item: EntityRef, world: Store) -> Window | None:
        """When a responder responds: the service's own window of their available time."""
        del item, world
        return self.service.within

    def leaning(self, item: EntityRef, world: Store) -> str | None:
        """How the service's responders tend to respond, in plain words."""
        del item, world
        return self.service.bias

    def drawn(self, item: EntityRef, offers: Sequence[Offer], world: Store) -> Offer | None:
        """The offer the service's odds draw for the item in its state: the model then writes only its content."""
        machine = self._machine(world)
        if machine is None:
            return None
        held = self._located(item, world)
        picked = self._desk.odds(self.service, held, machine.legal(held.state, Trigger.PERSON))
        return next((o for o in offers if picked is not None and o.name == picked.name), None)


def unvoiced(scenario: Scenario) -> list[str]:
    """Every service a model must speak for: all of them."""
    return [f"service {s.key} (its answers)" for s in scenario.services]


def _illegal(transition: MachineTransition, state: str, content: str) -> str | None:
    if state not in transition.from_:
        return f"{transition.name} is not allowed from {state} (only from {', '.join(transition.from_)})"
    try:
        carried: JsonValue = json.loads(content)
    except json.JSONDecodeError:
        carried = None
    missing = [f for f in transition.requires if not values_at(carried, f) or values_at(carried, f) == [None]]
    if missing:
        return f"{transition.name} requires {', '.join(missing)}"
    return None


def _digest(parts: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(list(parts)).encode()).hexdigest()


def _json_text(text: str | None) -> str:
    """A body as JSON text: as sent when it is JSON, else as a JSON string of it, or `{}` for none."""
    if text is None or not text.strip():
        return "{}"
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return json.dumps(text)
    return text


def _matched(template: str, segments: list[str]) -> dict[str, str] | None:
    parts = template.rstrip("/").split("/")
    if len(parts) != len(segments):
        return None
    found: dict[str, str] = {}
    for part, segment in zip(parts, segments, strict=True):
        named = re.fullmatch(r"\{([^}]+)\}", part)
        if named is not None:
            found[named.group(1)] = segment
        elif part != segment:
            return None
    return found


def _operations(document: JsonValue) -> list[tuple[str, str]]:
    """Every (path template, method) the OpenAPI document holds, its servers' base path before each."""
    if not isinstance(document, dict) or "paths" not in document or not isinstance(document["paths"], dict):
        return []
    base = ""
    servers = document["servers"] if "servers" in document else None
    first = servers[0] if isinstance(servers, list) and servers else None
    if isinstance(first, dict) and "url" in first and isinstance(first["url"], str):
        base = urlsplit(first["url"]).path.rstrip("/")
    paths = document["paths"]
    found: list[tuple[str, str]] = []
    for path, item in paths.items():
        if isinstance(item, dict):
            found += [(base + path, m.upper()) for m in item if m in _OPENAPI_METHODS]
    return found


def _operation(document: JsonValue, route: str) -> JsonValue:
    found = ROUTE.match(route)
    if found is None or not isinstance(document, dict) or "paths" not in document:
        return None
    paths = document["paths"]
    if not isinstance(paths, dict):
        return None
    for template, method in _operations(document):
        if method == found["method"] and template == found["path"]:
            for path, item in paths.items():
                if template.endswith(path) and isinstance(item, dict) and method.lower() in item:
                    return item[method.lower()]
    return None


def _answer_schema(operation: JsonValue) -> JsonValue:
    """The JSON Schema of an operation's success answer."""
    return _schema_of(operation, lambda code: code.startswith("2"))


def _error_schema(document: JsonValue) -> JsonValue:
    """The JSON Schema of the document's refusals: the first 4xx answer any operation describes."""
    if not isinstance(document, dict) or not isinstance(document["paths"] if "paths" in document else None, dict):
        return None
    paths = document["paths"]
    assert isinstance(paths, dict)
    for item in paths.values():
        if not isinstance(item, dict):
            continue
        for operation in item.values():
            found = _schema_of(operation, lambda code: code.startswith("4"))
            if found is not None:
                return found
    return None


def _schema_of(operation: JsonValue, wanted: Callable[[str], bool]) -> JsonValue:
    if not isinstance(operation, dict) or "responses" not in operation or not isinstance(operation["responses"], dict):
        return None
    responses = operation["responses"]
    assert isinstance(responses, dict)
    for code, answer in responses.items():
        if not wanted(str(code)) or not isinstance(answer, dict):
            continue
        content = answer["content"] if "content" in answer else None
        if isinstance(content, dict):
            for media, held in content.items():
                if "json" in media and isinstance(held, dict) and "schema" in held:
                    return held["schema"]
    return None


def _transition_line(transition: MachineTransition) -> str:
    return f'- "{transition.name}": from {", ".join(transition.from_)} to {transition.to}, by {transition.by.value}'


def _what(meaning: RouteMeaning) -> str:
    if meaning.means is Meaning.TRANSITION:
        return f"asks for the transition {meaning.transition}"
    return {
        Meaning.CREATE: "files a new item",
        Meaning.UPDATE: "changes what an item says",
        Meaning.READ: "reads",
        Meaning.SUBSCRIBE: "registers an address you push to",
        Meaning.OTHER: "does something your state holds nothing of",
        Meaning.TRANSITION: "",
    }[meaning.means]


def _call_text(call: Call | None) -> str:
    if call is None:
        return "(none)"
    return f"{call.method.upper()} {call.path}\n{_cut(call.body or '', 4000)}"


def _cut(text: str, most: int) -> str:
    return text if len(text) <= most else text[:most] + " …(cut)"


def _failed(service: Service, why: str) -> ServiceAnswer:
    body = json.dumps({"error": why, "host": service.host}, ensure_ascii=False)
    return ServiceAnswer(status=502, body=body, answered_by=AnsweredBy.REFUSAL, note=why)
