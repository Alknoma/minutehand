"""The run loop reads what waits on people in the agent's own product, and has them decide it.

Each test runs the real loop over the real store against a product served over real HTTP: an agent in this process
raises approvals in the product on its wakes, Minutehand reads each approver's list as them, and the approver's
replier decides. Every assertion is on a recorded moment."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import WakeReason, WakeRequest
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import Absence, ScriptedDecision, Silent, WorkingHours
from minutehand.domain.world import Actor, InboxItemSnapshot, ItemStatus, Operation, WorldEvent
from tests.inboxes.product import Product, serving
from tests.inboxes.support import NADIA, OWEN, T0, TOKENS, Agent, deciding, hours, inbox, play, scenario, sends

APPROVE = ScriptedDecision(decision="approve")
REJECT = ScriptedDecision(decision="reject", inputs={"reason": "Over budget"})


@pytest.fixture
def product() -> Iterator[Product]:
    with serving(Product(tokens=dict(TOKENS))) as served:
        yield served


@pytest.fixture(autouse=True)
def nadias_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NADIA_TOKEN", TOKENS[NADIA])


def items(events: list[WorldEvent]) -> list[WorldEvent]:
    return [e for e in events if isinstance(e.after, InboxItemSnapshot)]


def snapshot(event: WorldEvent) -> InboxItemSnapshot:
    assert isinstance(event.after, InboxItemSnapshot)
    return event.after


def gated_agent(
    product: Product, *, send_anyway: bool = False, approvals: int = 1, remind: bool = False
) -> Callable[[SqliteStore], Agent]:
    """Raises `approvals` approvals on its first wake; sends Owen the booking once the first is approved (or decided
    at all, `send_anyway`), and is done; done without sending on a rejection. With `remind`, messages Nadia on each
    wake while it waits."""

    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                for i in range(1, approvals + 1):
                    product.raise_approval(f"a{i}", NADIA, f"Send Owen booking {i}", f"tell-{i}")
                return request.now + timedelta(days=1), False
            state = product.state("a1")
            if state == "approved" or (send_anyway and state == "rejected"):
                sends(store, OWEN, "The venue is booked: LH-2291", operation="tell-1")
                return None, True
            if state == "rejected":
                return None, True
            if remind:
                sends(store, NADIA, "The booking still waits for your approval", operation="")
            return request.now + timedelta(days=1), False

        return Agent(plan=plan)

    return made


async def test_an_approval_raised_in_the_product_is_asked_and_approved_after_the_delay(
    tmp_path: Path, product: Product
) -> None:
    scn = scenario(deciding(APPROVE, hours=2))
    played = await play(tmp_path, scn, inbox(product), gated_agent(product))

    seen = items(played.store.events())
    asked, decided = seen[0], seen[1]
    assert (asked.actor, asked.operation, asked.sim_time) == (Actor.AGENT, Operation.CREATE, T0)
    assert snapshot(asked).person == "nadia" and snapshot(asked).decisions == ["approve", "reject"]
    assert (decided.actor, decided.sim_time, snapshot(decided).status) == (
        Actor.PERSON,
        T0 + hours(2),
        ItemStatus.DECIDED,
    )
    assert product.state("a1") == "approved"
    posted = [s for s in product.sent if s.method == "POST"]
    assert [p.authorization for p in posted] == [f"Bearer {TOKENS[NADIA]}"]
    assert played.result.verdict.kind is VerdictKind.PASSED, played.result.findings
    card = played.result.effectiveness
    assert (card.decisions_asked, card.decisions_made, card.decisions_pending) == (1, 1, 0)


async def test_the_decision_wakes_the_agent_as_a_reply_does(tmp_path: Path, product: Product) -> None:
    scn = scenario(deciding(APPROVE, hours=2))
    woken: list[WakeRequest] = []

    def made(store: SqliteStore) -> Agent:
        agent = gated_agent(product)(store)
        inner = agent.plan

        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            woken.append(request)
            return inner(request, n)

        return Agent(plan=plan)

    await play(tmp_path, scn, inbox(product), made)
    assert [(w.reason, w.now) for w in woken] == [(WakeReason.START, T0), (WakeReason.PERSON_REPLIED, T0 + hours(2))]


async def test_a_decision_due_outside_working_hours_lands_when_they_open(tmp_path: Path, product: Product) -> None:
    hours_kept = WorkingHours(opens=time(9), closes=time(17))
    scn = scenario(deciding(APPROVE, hours=10), working_hours=hours_kept.model_dump())
    played = await play(tmp_path, scn, inbox(product), gated_agent(product))

    decided = [e for e in items(played.store.events()) if e.actor is Actor.PERSON]
    assert [e.sim_time for e in decided] == [T0 + timedelta(days=1)]  # Monday 19:00 -> Tuesday 09:00


async def test_an_absent_approver_decides_when_back(tmp_path: Path, product: Product) -> None:
    away = Absence(lasts=timedelta(days=2))
    scn = scenario(deciding(APPROVE, hours=2), absences=[away.model_dump()])
    played = await play(tmp_path, scn, inbox(product), gated_agent(product))

    decided = [e for e in items(played.store.events()) if e.actor is Actor.PERSON]
    assert [e.sim_time for e in decided] == [T0 + timedelta(days=2)]


async def test_a_decision_for_the_nth_item_wins_over_one_for_every_item(tmp_path: Path, product: Product) -> None:
    second = ScriptedDecision(to_item=2, decision="reject", inputs={"reason": "Twice is once too many"})
    scn = scenario(deciding(APPROVE, second, hours=2))
    played = await play(tmp_path, scn, inbox(product), gated_agent(product, approvals=2))

    decided = {snapshot(e).item_id: snapshot(e) for e in items(played.store.events()) if e.actor is Actor.PERSON}
    assert (decided["a1"].decision, decided["a2"].decision) == ("approve", "reject")
    assert decided["a2"].inputs == {"reason": "Twice is once too many"}
    assert product.approvals["a2"].reason == "Twice is once too many"


async def test_a_silent_approver_never_decides_and_the_ask_stays_open(tmp_path: Path, product: Product) -> None:
    scn = scenario(Silent(), days=3)
    played = await play(tmp_path, scn, inbox(product), gated_agent(product))

    assert [e.actor for e in items(played.store.events())] == [Actor.AGENT]
    assert not [s for s in product.sent if s.method == "POST"]
    assert played.result.effectiveness.waits_open_at_end == 1
    assert played.result.effectiveness.decisions_pending == 1
    assert played.result.verdict.kind in (VerdictKind.UNFINISHED, VerdictKind.FAILED)


async def test_an_approver_with_nothing_said_about_deciding_is_refused_naming_them(
    tmp_path: Path, product: Product
) -> None:
    from minutehand.domain.scenario import Scripted

    scn = scenario(Scripted(replies=[]))
    with pytest.raises(RunRefused, match="nadia can receive items in inbox approvals"):
        await play(tmp_path, scn, inbox(product), gated_agent(product))


async def test_a_decision_the_product_refuses_is_recorded_with_its_answer_and_the_wait_stays_open(
    tmp_path: Path, product: Product
) -> None:
    product.refuse = True
    scn = scenario(deciding(APPROVE, hours=2), days=2)
    played = await play(tmp_path, scn, inbox(product), gated_agent(product))

    tried = [e for e in items(played.store.events()) if e.actor is Actor.PERSON]
    assert len(tried) == 1
    assert snapshot(tried[0]).status is ItemStatus.PENDING
    refused = snapshot(tried[0]).refused
    assert refused is not None and "cannot be decided now" in refused
    assert played.result.effectiveness.waits_open_at_end == 1
    assert played.result.effectiveness.decisions_made == 0


async def test_an_item_taken_back_undecided_is_withdrawn_and_its_decision_never_made(
    tmp_path: Path, product: Product
) -> None:
    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                return request.now + hours(1), False
            product.take_back("a1")
            return None, True

        return Agent(plan=plan)

    scn = scenario(deciding(APPROVE, hours=5))
    played = await play(tmp_path, scn, inbox(product), made)

    seen = items(played.store.events())
    assert [(e.actor, snapshot(e).status, e.sim_time) for e in seen] == [
        (Actor.AGENT, ItemStatus.PENDING, T0),
        (Actor.AGENT, ItemStatus.WITHDRAWN, T0 + hours(1)),
    ]
    assert not [s for s in product.sent if s.method == "POST"]
    assert played.result.effectiveness.waits_open_at_end == 0


async def test_a_list_of_everyones_items_keeps_each_for_whom_it_waits_on(tmp_path: Path, product: Product) -> None:
    scn = scenario(deciding(APPROVE, hours=2))
    played = await play(tmp_path, scn, inbox(product, everyone=True), gated_agent(product))

    asked = [snapshot(e) for e in items(played.store.events()) if e.actor is Actor.AGENT]
    assert [(i.item_id, i.person, i.waits_on) for i in asked] == [("a1", "nadia", NADIA)]
