"""A declared service (`docs/services.md`) at its desk: the state Minutehand holds and enforces, the timers it books
and fires, what the agent is answered, rendered by the recipes' fake model, and its people responding through the
people engine (`application.people`), all over the real `SqliteStore` and `RunClock`."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import PendingService
from minutehand.application.people import Booking, WrittenTransition
from minutehand.application.run_clock import RunClock
from minutehand.application.services import ServiceDesk, WrittenAnswer
from minutehand.domain.conversation import ModelMessage, Wrote
from minutehand.domain.scenario import Scenario
from minutehand.domain.services import Service
from minutehand.domain.transitions import Transition
from minutehand.domain.world import (
    Actor,
    EntityKind,
    PendingSnapshot,
    PendingStatus,
    ServiceRecordKind,
    ServiceRecordSnapshot,
    TransitionSnapshot,
)
from minutehand.ports.model import Answered, AnswerT
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.services import Call, Delivered
from tests.support.people import people_engine, people_model

START = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
HOST = "api.approvals.example"

APPROVAL = {
    "initial": "pending",
    "states": ["pending", "approved", "rejected", "needs_info"],
    "transitions": [
        {"name": "approve", "from": ["pending"], "to": "approved", "by": "person"},
        {"name": "reject", "from": ["pending"], "to": "rejected", "by": "person"},
        {"name": "ask_back", "from": ["pending"], "to": "needs_info", "by": "person"},
        {"name": "resubmit", "from": ["needs_info"], "to": "pending", "by": "agent", "requires": ["cost_centre"]},
    ],
}


def scenario(
    *,
    facts: list[str] | None = None,
    seed: int = 7,
    machine: object = APPROVAL,
    extra: dict[str, object] | None = None,
    nadia: dict[str, object] | None = None,
) -> Scenario:
    service: dict[str, object] = {
        "host": HOST,
        "name": "approvals",
        "responders": ["nadia"],
        "within": {"min": "PT2H", "max": "PT6H"},
        "describe": "Purchase approvals.",
        "ids": {"format": "prefixed", "prefix": "req_"},
        **(extra or {}),
    }
    if machine is not None:
        service["machine"] = machine
    return Scenario.model_validate(
        {
            "name": "approvals",
            "goal": "Order the laptops once approved.",
            "owner": "owen",
            "starts_at": START.isoformat(),
            "seed": seed,
            "people": [
                {"key": "owen", "name": "Owen Hart", "email": "owen@example.com", "reply": {"kind": "silent"}},
                {
                    "key": "nadia",
                    "name": "Nadia Ek",
                    "email": "nadia@example.com",
                    "facts": facts or ["I approve this", "the Q3 budget covers it"],
                    "reply": {"kind": "answers"},
                    **(nadia or {}),
                },
            ],
            "services": [service],
        }
    )


class Pushes:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def push(self, url: str, body: str) -> Delivered:
        self.sent.append((url, body))
        return Delivered(status=200, failure=None)


@dataclass(frozen=True)
class Fired:
    at: datetime
    responder: str | None = None
    acted: Transition | None = None
    failure: str | None = None
    timer: PendingService | None = None


class Desk:
    """A desk and the people engine over a world of their own, as the proxy and the run loop drive them."""

    def __init__(self, tmp: Path, played: Scenario, name: str = "root", model: LanguageModel | None = None) -> None:
        self.clock = RunClock(START)
        self.store = SqliteStore(tmp / "world.db", name, self.clock)
        self.pushes = Pushes()
        self.played = played
        self.use(model or people_model())
        self.service: Service = played.services[0]
        self._seen = 0
        self.owed_people: list[Booking] = []
        self.owed_timers: list[PendingService] = []

    def use(self, model: LanguageModel, documents: dict[str, object] | None = None) -> None:
        self.desk = ServiceDesk(self.played, model, pushes=self.pushes, documents=documents or {})  # type: ignore[arg-type]
        self.engine = people_engine(
            self.played, {s.key: self.desk.provider(s.key) for s in self.played.services}, model
        )

    async def call(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        text = json.dumps(body) if body is not None else None
        answered = await self.desk.answer(
            self.service, Call(method=method, path=path, body=text), self.store, self.clock
        )
        return answered.status, json.loads(answered.body) if answered.body else None

    async def look(self) -> None:
        """What is owed now, as the run loop books it after a wake: the service's timers, people's responses."""
        new = self.store.events(since=self._seen)
        if new:
            self._seen = new[-1].seq
        self.owed_timers += [b for e in new for b in self.desk.bookings(e, self.store)]
        looked = await self.engine.look(self.store, self.clock)
        self.owed_people = [b for b in self.owed_people if b.pending not in looked.gone]
        self.owed_people += [b for b in looked.booked if b.at is not None]

    async def fire_next(self) -> Fired:
        """Fire the earliest owed response or timer, at its moment."""
        await self.look()
        owed: list[tuple[datetime, Booking | PendingService]] = [
            *((b.at, b) for b in self.owed_people if b.at is not None),
            *((t.due.at, t) for t in self.owed_timers),
        ]
        assert owed, "nothing is owed"
        at, first = min(owed, key=lambda o: o[0])
        self.clock.jump(max(at, self.clock.now()))
        if isinstance(first, PendingService):
            self.owed_timers.remove(first)
            await self.desk.fire(first, self.store, self.clock)
            return Fired(at=at, timer=first)
        self.owed_people.remove(first)
        acted = await self.engine.act(first.pending, self.store, self.clock)
        return Fired(at=at, responder=first.person, acted=acted.transition, failure=acted.failure)

    def moves(self) -> list[tuple[str, str | None, str, Actor, str | None]]:
        """Every move of the service's items: its name, from, to, by and who."""
        return [
            (t.name, t.from_state, t.to_state, e.actor, t.who)
            for e in self.store.events()
            if isinstance(t := e.after, TransitionSnapshot) and e.entity.provider == self.service.key
        ]

    def records(self) -> list[ServiceRecordSnapshot]:
        """What the service is, as each record last stood."""
        latest: dict[str, ServiceRecordSnapshot] = {}
        for e in self.store.events():
            if e.entity.kind is EntityKind.SERVICE_RECORD and isinstance(e.after, ServiceRecordSnapshot):
                latest[e.entity.external_id] = e.after
        return list(latest.values())


@pytest.fixture
def desk(tmp_path: Path) -> Desk:
    return Desk(tmp_path, scenario())


async def test_a_create_files_an_item_in_the_first_state_and_a_person_responds_at_a_drawn_moment(desk: Desk) -> None:
    status, filed = await desk.call("POST", "/v1/requests", {"po": "PO-7731", "amount": 40})
    assert status == 201 and isinstance(filed, dict)
    assert filed["status"] == "pending" and filed["po"] == "PO-7731"
    item = str(filed["id"])
    fired = await desk.fire_next()
    assert fired.responder == "nadia" and timedelta(hours=2) <= fired.at - START <= timedelta(days=3)
    status, read = await desk.call("GET", f"/v1/requests/{item}")
    assert status == 200 and isinstance(read, dict)
    assert read["status"] == "approved"
    assert read["responses"][0]["by"] == "nadia" and "Q3 budget" in read["responses"][0]["note"]
    assert desk.moves() == [
        ("create", None, "pending", Actor.AGENT, None),
        ("approve", "pending", "approved", Actor.PERSON, "nadia"),
    ]


async def file_and_respond(desk: Desk, body: object | None = None) -> str:
    """The agent files a request; the responder responds at their moment. The item's id."""
    status, filed = await desk.call(
        "POST", "/v1/requests", body or {"po": "PO-7731", "callback_url": "http://127.0.0.1:9/hook"}
    )
    assert status == 201 and isinstance(filed, dict)
    await desk.fire_next()
    return str(filed["id"])


async def test_one_response_reads_alike_through_a_status_field_a_responses_list_a_feed_and_a_push(desk: Desk) -> None:
    item = await file_and_respond(desk)
    response = next(t for e in desk.store.events() if isinstance(t := e.after, TransitionSnapshot) and t.who)
    content = json.loads(response.content)
    _, status_read = await desk.call("GET", f"/v1/requests/{item}")
    _, listed = await desk.call("GET", f"/v1/requests/{item}/responses")
    _, feed = await desk.call("GET", "/v1/events?since=0")
    assert isinstance(status_read, dict) and isinstance(listed, dict) and isinstance(feed, dict)
    assert status_read["status"] == "approved"
    assert listed["responses"] == [{"by": "nadia", "response": "approve", **content}]
    assert [(e["type"], e["item"], e["by"]) for e in feed["events"]] == [
        ("item.pending", item, "agent"),
        ("item.approved", item, "nadia"),
    ]
    assert feed["events"][1]["content"] == content
    ((url, body),) = desk.pushes.sent
    pushed = json.loads(body)
    assert url == "http://127.0.0.1:9/hook" and pushed["event"] == "item.approved"
    assert pushed["item"]["status"] == "approved" and pushed["item"]["responses"] == listed["responses"]


async def test_a_routes_first_answer_fixes_its_shape_for_the_run(desk: Desk) -> None:
    item = await file_and_respond(desk)
    await desk.call("GET", f"/v1/requests/{item}")
    route = next(
        r for r in desk.records() if r.route == "GET /v1/requests/{id}" and r.record is ServiceRecordKind.ROUTE
    )
    shape = json.loads(route.text)["shape"]
    assert shape["required"] == ["po", "callback_url", "id", "status", "responses"]
    assert shape["additionalProperties"] is False


async def test_polling_an_unchanged_service_makes_no_model_call_after_its_first_answer(desk: Desk) -> None:
    item = await file_and_respond(desk)
    status, first = await desk.call("GET", f"/v1/requests/{item}")
    assert status == 200
    before = len(desk.store.person_calls())
    for _ in range(5):
        status, read = await desk.call("GET", f"/v1/requests/{item}")
        assert status == 200 and read == first
    assert len(desk.store.person_calls()) == before, "answered from the record of its first rendering: no call"
    await desk.call("POST", f"/v1/requests/{item}/resubmit", {"cost_centre": "CC-1"})  # refused: no state change
    _, again = await desk.call("GET", f"/v1/requests/{item}")
    assert again == first


async def test_an_illegal_transition_is_refused_in_the_services_error_shape_and_changes_nothing(tmp_path: Path) -> None:
    desk = Desk(tmp_path, scenario())
    _, filed = await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
    assert isinstance(filed, dict)
    status, refused = await desk.call("POST", f"/v1/requests/{filed['id']}/resubmit", {"cost_centre": "CC-12"})
    assert status == 409 and isinstance(refused, dict)
    assert "resubmit is not allowed from pending" in refused["error"]["message"]
    assert [i.state for i in desk.desk.items(desk.service, desk.store)] == ["pending"]
    errors = next(r for r in desk.records() if r.record is ServiceRecordKind.ERRORS)
    assert json.loads(errors.text)["required"] == ["error"]


async def test_asking_back_waits_on_the_agent_whose_resubmit_brings_a_second_response(tmp_path: Path) -> None:
    desk = Desk(tmp_path, scenario(facts=["I ask back: which cost centre pays for this?"]))
    _, filed = await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
    assert isinstance(filed, dict)
    item = str(filed["id"])
    await desk.fire_next()
    _, read = await desk.call("GET", f"/v1/requests/{item}")
    assert isinstance(read, dict) and read["status"] == "needs_info"
    await desk.look()
    assert desk.owed_people == [], "nothing waits on a person while the item waits on the agent"
    status, _ = await desk.call("POST", f"/v1/requests/{item}/resubmit", {"note": "which one?"})
    assert status == 409, "resubmit requires the cost centre"
    status, resubmitted = await desk.call("POST", f"/v1/requests/{item}/resubmit", {"cost_centre": "CC-12"})
    assert status == 200 and isinstance(resubmitted, dict) and resubmitted["status"] == "pending"
    second = await desk.fire_next()
    assert second.responder == "nadia"
    assert [(m[0], m[3], m[4]) for m in desk.moves()[1:]] == [
        ("ask_back", Actor.PERSON, "nadia"),
        ("resubmit", Actor.AGENT, None),
        ("ask_back", Actor.PERSON, "nadia"),
    ]


async def test_a_choice_among_options_is_the_responders_pick_carried_as_its_required_field(tmp_path: Path) -> None:
    machine = {
        "initial": "offered",
        "states": ["offered", "chosen"],
        "transitions": [
            {"name": "choose", "from": ["offered"], "to": "chosen", "by": "person", "requires": ["option"]}
        ],
    }
    desk = Desk(tmp_path, scenario(facts=["option: B", "the afternoon slot suits the team"], machine=machine))
    _, filed = await desk.call("POST", "/v1/polls", {"question": "Which slot?", "options": ["A", "B", "C"]})
    assert isinstance(filed, dict)
    await desk.fire_next()
    _, read = await desk.call("GET", f"/v1/polls/{filed['id']}")
    assert isinstance(read, dict) and read["status"] == "chosen"
    assert read["responses"][0]["option"] == "B"


async def test_missing_data_is_supplied_as_the_fields_its_transition_requires(tmp_path: Path) -> None:
    machine = {
        "initial": "incomplete",
        "states": ["incomplete", "complete"],
        "transitions": [
            {"name": "supply", "from": ["incomplete"], "to": "complete", "by": "person", "requires": ["cost_centre"]}
        ],
    }
    desk = Desk(tmp_path, scenario(facts=["cost_centre: CC-4410"], machine=machine))
    _, filed = await desk.call("POST", "/v1/forms", {"form": "purchase", "missing": ["cost_centre"]})
    assert isinstance(filed, dict)
    fired = await desk.fire_next()
    assert fired.acted is not None and json.loads(fired.acted.content) == {"cost_centre": "CC-4410"}


async def test_a_model_proposed_machine_is_kept_once_and_enforced(tmp_path: Path) -> None:
    desk = Desk(tmp_path, scenario(machine=None))
    for _ in range(3):
        status, _ = await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
        assert status == 201
    machines = [c for c in desk.store.person_calls() if c.prompt_version == "service-machine/1"]
    assert len(machines) == 1 and not machines[0].replayed
    kept = next(r for r in desk.records() if r.record is ServiceRecordKind.MACHINE)
    assert kept.source.value == "model" and "ask_back" in kept.text
    item = desk.desk.items(desk.service, desk.store)[0].id
    status, _ = await desk.call("POST", f"/v1/requests/{item}/resubmit", {"cost_centre": "CC-1"})
    assert status == 409


async def test_the_same_seed_draws_the_same_responder_moment_and_response(tmp_path: Path) -> None:
    def played(seed: int) -> Scenario:
        two = scenario(seed=seed).model_dump(by_alias=True)
        two["people"].append(
            {
                "key": "marta",
                "name": "Marta Holm",
                "email": "marta@example.com",
                "facts": ["I reject it"],
                "reply": {"kind": "answers"},
            }
        )
        two["services"][0]["responders"] = ["nadia", "marta"]
        return Scenario.model_validate(two)

    async def once(seed: int, name: str) -> tuple[str | None, datetime, str | None]:
        (tmp_path / name).mkdir()
        desk = Desk(tmp_path / name, played(seed), name)
        await file_and_respond(desk, {"po": "PO-7731"})
        response = next(m for m in desk.moves() if m[3] is Actor.PERSON)
        return response[4], desk.clock.now(), response[0]

    assert await once(7, "a") == await once(7, "b")
    seen = {await once(seed, f"s{seed}") for seed in range(12)}
    assert len({by for by, _, _ in seen}) == 2, "different seeds draw different responders"
    assert {(by, said) for by, _, said in seen} == {("nadia", "approve"), ("marta", "reject")}
    assert len({at for _, at, _ in seen}) > 6, "and different moments"


async def test_odds_draw_each_transition_about_as_often_as_weighed(tmp_path: Path) -> None:
    approve = 0
    for seed in range(100):
        (tmp_path / f"s{seed}").mkdir()
        played = scenario(seed=seed, extra={"odds": {"approve": 0.7, "reject": 0.3}})
        desk = Desk(tmp_path / f"s{seed}", played, f"s{seed}")
        await file_and_respond(desk, {"po": "PO-7731"})
        said = [m[0] for m in desk.moves() if m[3] is Actor.PERSON]
        assert said in (["approve"], ["reject"]), "drawn by the odds whatever the person's facts say"
        approve += said == ["approve"]
    assert 55 <= approve <= 85, approve


async def test_nobody_responds_through_a_service_with_no_responders_or_a_silent_one(tmp_path: Path) -> None:
    for name, played in (
        ("none", scenario(extra={"responders": []})),
        ("silent", scenario(nadia={"reply": {"kind": "silent"}})),
    ):
        (tmp_path / name).mkdir()
        desk = Desk(tmp_path / name, played, name)
        status, filed = await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
        assert status == 201 and isinstance(filed, dict)
        await desk.look()
        assert desk.owed_people == [] and desk.owed_timers == [], name
        assert [m[0] for m in desk.moves()] == ["create"], name
        _, read = await desk.call("GET", f"/v1/requests/{filed['id']}")
        assert isinstance(read, dict) and read["status"] == "pending", name


class Tampering:
    """The fake model, but an answer is replaced by what `tamper` makes of it: a model saying what the state does
    not hold, or picking what is not offered."""

    def __init__(self, tamper: Callable[[BaseModel], BaseModel | None]) -> None:
        self._model = people_model()
        self.model_id = self._model.model_id
        self._tamper = tamper

    async def answer(
        self,
        system: str,
        messages: Sequence[ModelMessage],
        answer: type[AnswerT],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Answered[AnswerT]:
        given = await self._model.answer(system, messages, answer, model=model, temperature=temperature)
        changed = self._tamper(given.answer)
        if changed is None:
            return given
        assert isinstance(changed, answer)
        return Answered(answer=changed, model=given.model)


async def test_a_rendered_answer_showing_an_impossible_state_is_rejected_and_never_reaches_the_agent(
    tmp_path: Path,
) -> None:
    def approved_too_soon(said: BaseModel) -> BaseModel | None:
        if isinstance(said, WrittenAnswer) and '"status": "pending"' in said.body:
            return WrittenAnswer(
                status=said.status, body=said.body.replace('"status": "pending"', '"status": "approved"')
            )
        return None

    desk = Desk(tmp_path, scenario(), model=Tampering(approved_too_soon))
    status, filed = await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
    assert status == 502 and isinstance(filed, dict)
    assert "shows item req_" in filed["error"] and "as 'approved', and it is 'pending'" in filed["error"]
    renders = [c for c in desk.store.person_calls() if c.prompt_version == "service-answer/1"]
    assert len(renders) == 2, "rendered once more before it is refused"


async def test_a_response_the_state_does_not_offer_is_asked_again_then_left_owed_and_refused(tmp_path: Path) -> None:
    def resubmits(said: BaseModel) -> BaseModel | None:
        return WrittenTransition(take="resubmit") if isinstance(said, WrittenTransition) else None

    desk = Desk(tmp_path, scenario(), model=Tampering(resubmits))
    await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
    fired = await desk.fire_next()
    assert fired.acted is None and fired.failure is not None and "'resubmit'" in fired.failure
    assert [i.state for i in desk.desk.items(desk.service, desk.store)] == ["pending"]
    picks = [c for c in desk.store.person_calls() if c.wrote is Wrote.TRANSITION]
    assert len(picks) == 2, "asked once more before the move is left owed"
    held = [e.after for e in desk.store.events() if isinstance(e.after, PendingSnapshot)]
    assert held[-1].status is PendingStatus.PENDING and held[-1].failure is not None


ORDER = {
    "initial": "placed",
    "states": ["placed", "approved", "rejected", "shipping_requested", "shipped", "expired"],
    "transitions": [
        {"name": "approve", "from": ["placed"], "to": "approved", "by": "person"},
        {"name": "reject", "from": ["placed"], "to": "rejected", "by": "person"},
        {"name": "request_ship", "from": ["approved"], "to": "shipping_requested", "by": "agent"},
        {
            "name": "ship",
            "from": ["shipping_requested"],
            "to": "shipped",
            "by": "system",
            "actor": "warehouse",
            "after": "P2D",
        },
        {"name": "expire", "from": ["placed"], "to": "expired", "by": "timer", "after": "PT1H"},
    ],
}


async def test_an_order_moves_by_its_people_its_agent_its_timer_and_its_warehouse(tmp_path: Path) -> None:
    desk = Desk(tmp_path, scenario(machine=ORDER))
    _, early = await desk.call("POST", "/v1/orders", {"sku": "laptop", "callback_url": "http://127.0.0.1:9/orders"})
    assert isinstance(early, dict)
    expired = await desk.fire_next()
    assert expired.timer is not None and expired.timer.transition == "expire"
    assert expired.at == START + timedelta(hours=1)
    states = {i.id: i.state for i in desk.desk.items(desk.service, desk.store)}
    assert states[str(early["id"])] == "expired"
    await desk.look()
    assert desk.owed_people == [], "expired: the response owed is gone"
    status, refused = await desk.call("POST", f"/v1/orders/{early['id']}/request_ship")
    assert status == 409 and isinstance(refused, dict)

    (tmp_path / "second").mkdir()
    desk = Desk(tmp_path / "second", scenario(machine={**ORDER, "transitions": ORDER["transitions"][:-1]}), "second")
    _, order = await desk.call("POST", "/v1/orders", {"sku": "laptop"})
    assert isinstance(order, dict)
    await desk.fire_next()
    status, shipping = await desk.call("POST", f"/v1/orders/{order['id']}/request_ship")
    assert status == 200 and isinstance(shipping, dict) and shipping["status"] == "shipping_requested"
    shipped = await desk.fire_next()
    assert shipped.timer is not None and shipped.timer.transition == "ship"
    assert [(m[0], m[3], m[4]) for m in desk.moves()] == [
        ("create", Actor.AGENT, None),
        ("approve", Actor.PERSON, "nadia"),
        ("request_ship", Actor.AGENT, None),
        ("ship", Actor.SYSTEM, "warehouse"),
    ]


async def test_a_timer_does_not_fire_on_an_item_that_left_its_state(tmp_path: Path) -> None:
    late = {**ORDER["transitions"][-1], "after": "P5D"}  # type: ignore[dict-item]
    desk = Desk(tmp_path, scenario(machine={**ORDER, "transitions": [*ORDER["transitions"][:-1], late]}))
    await desk.call("POST", "/v1/orders", {"sku": "laptop"})
    first = await desk.fire_next()
    assert first.responder == "nadia", "the response comes within hours, the timer in five days"
    [timer] = desk.owed_timers
    desk.clock.jump(timer.due.at)
    await desk.desk.fire(timer, desk.store, desk.clock)
    assert [i.state for i in desk.desk.items(desk.service, desk.store)] == ["approved"]
    assert [m[0] for m in desk.moves()] == ["create", "approve"]


OPENAPI = {
    "openapi": "3.0.3",
    "servers": [{"url": "https://api.approvals.example/v2"}],
    "paths": {
        "/requests": {
            "post": {
                "responses": {
                    "201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Request"}}}}
                }
            }
        },
        "/requests/{requestId}": {
            "get": {
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Request"}}}},
                    "404": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Problem"}}}},
                }
            }
        },
    },
    "components": {
        "schemas": {
            "Request": {
                "type": "object",
                "required": ["id", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "approved", "rejected", "needs_info"]},
                    "po": {"type": "string"},
                    "responses": {"type": "array", "items": {"type": "object"}},
                },
                "additionalProperties": False,
            },
            "Problem": {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}},
        }
    },
}


async def test_an_openapi_document_pins_each_routes_shape_before_any_answer(tmp_path: Path) -> None:
    desk = Desk(tmp_path, scenario())
    desk.use(people_model(), documents={"approvals": OPENAPI})
    status, filed = await desk.call("POST", "/v2/requests", {"po": "PO-7731"})
    assert status == 201 and isinstance(filed, dict)
    await desk.fire_next()
    status, read = await desk.call("GET", f"/v2/requests/{filed['id']}")
    assert status == 200 and isinstance(read, dict) and read["status"] == "approved"
    route = next(
        r for r in desk.records() if r.route == "GET /v2/requests/{requestId}" and r.record is ServiceRecordKind.ROUTE
    )
    assert json.loads(route.text)["shape"] == {"$ref": "#/components/schemas/Request"}
    assert route.source.value == "openapi"
    status, unshaped = await desk.call("POST", "/v2/requests", {"po": "PO-7731", "extra": {"nested": True}})
    assert status == 502 and isinstance(unshaped, dict)
    assert "$.extra: is not a field this shape has" in unshaped["error"]


async def test_a_fork_holds_each_item_as_it_stood_and_responds_after_it_on_its_own(tmp_path: Path) -> None:
    desk = Desk(tmp_path, scenario())
    _, filed = await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
    assert isinstance(filed, dict)
    await desk.look()
    [owed] = desk.owed_people
    at = desk.store.head()
    clock = RunClock(START)
    child = desk.store.fork("child", at_seq=at, clock=clock)
    await desk.fire_next()
    assert [i.state for i in desk.desk.items(desk.service, desk.store)] == ["approved"]

    assert [i.state for i in desk.desk.items(desk.service, child)] == ["pending"], "the fork holds it as it stood"
    assert (await desk.engine.look(child, clock)).booked == [], "and what it owed then: nothing new to book"
    assert owed.at is not None
    clock.jump(owed.at)
    acted = await desk.engine.act(owed.pending, child, clock)
    assert acted.transition is not None and acted.transition.to_state == "approved"
    assert [i.state for i in desk.desk.items(desk.service, child)] == ["approved"]
    assert len([m for m in desk.moves() if m[3] is Actor.PERSON]) == 1, "the parent's log is its own"


async def test_an_item_naming_one_of_the_responders_waits_on_them_alone(tmp_path: Path) -> None:
    for seed in range(6):
        two = scenario(seed=seed).model_dump(by_alias=True)
        two["people"].append(
            {"key": "marta", "name": "Marta Holm", "email": "marta@example.com", "facts": ["I reject it"]}
        )
        two["services"][0]["responders"] = ["nadia", "marta"]
        (tmp_path / f"s{seed}").mkdir()
        desk = Desk(tmp_path / f"s{seed}", Scenario.model_validate(two), f"s{seed}")
        await desk.call("POST", "/v1/requests", {"po": "PO-7731", "approver": "marta@example.com"})
        await desk.look()
        assert [b.person for b in desk.owed_people] == ["marta"], seed
        assert desk.engine.held("nadia", desk.store) == [] and desk.engine.held("owen", desk.store) == [], seed


async def test_a_field_the_agent_sends_later_comes_back_though_the_routes_shape_was_learned_before_it(
    tmp_path: Path,
) -> None:
    """A route's shape is learned from its first answer, which cannot know a field the agent sends later: what the
    agent wrote comes back as it wrote it, where a field it never sent would still be refused (the trial's
    resubmission of a quote, whose every read failed before)."""
    desk = Desk(tmp_path, scenario(facts=["I ask back: which cost centre pays for this?"]))
    _, filed = await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
    assert isinstance(filed, dict)
    item = str(filed["id"])
    _, before = await desk.call("GET", f"/v1/requests/{item}")  # the read's shape is learned here, without a quote
    await desk.fire_next()
    status, _ = await desk.call(
        "POST", f"/v1/requests/{item}/resubmit", {"cost_centre": "CC-12", "quote": "$1,150 each"}
    )
    assert status == 200

    status, read = await desk.call("GET", f"/v1/requests/{item}")

    assert isinstance(before, dict) and "quote" not in before
    assert status == 200 and isinstance(read, dict), read


async def test_takes_pinned_on_the_same_item_are_its_moves_in_order(tmp_path: Path) -> None:
    """Nadia asks back first, and approves the resubmission: two takes on one item, played in the order written."""
    takes = [{"provider": "approvals", "take": "ask_back"}, {"provider": "approvals", "take": "approve"}]
    desk = Desk(tmp_path, scenario(nadia={"takes": takes}))
    _, filed = await desk.call("POST", "/v1/requests", {"po": "PO-7731"})
    assert isinstance(filed, dict)
    item = str(filed["id"])
    await desk.fire_next()
    status, resubmitted = await desk.call("POST", f"/v1/requests/{item}/resubmit", {"cost_centre": "CC-12"})
    assert status == 200 and isinstance(resubmitted, dict)
    await desk.fire_next()

    assert [m[0] for m in desk.moves()[1:]] == ["ask_back", "resubmit", "approve"]
