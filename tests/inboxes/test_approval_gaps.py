"""The approval gaps the transitions design closes by construction (`docs/approvals.md`, Gaps), each held to a run:
every decision is a person's transition, recorded before the product acts on it; taking a request back is the
agent's own transition; a rule reads them as facts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.domain.agent import WakeRequest
from minutehand.domain.scenario import Scenario, Take
from minutehand.domain.world import Actor, TransitionSnapshot
from tests.inboxes.product import Approval, Product, serving
from tests.inboxes.support import (
    NADIA,
    OWEN,
    TOKENS,
    Agent,
    checks_named,
    deciding,
    inbox,
    play,
    scenario,
    sends,
)
from tests.support.rules import rules

FIRST_APPROVAL = """
- id: acts_only_once_approved
  each: transition
  where: {provider: [approvals], name: [approve], by: [person], first: true}
  count: {messages: {to: [owen], holding: [LH-2291]}, until: transition-PT1S}
  at_most: 0
  message: "went ahead before the approval"
"""

DECIDED_ON = """
- id: decided_by_a_person
  count: {transitions: {provider: [approvals], by: [person]}}
  at_most: 0
  message: "{rule.count} decision(s)"
- id: taken_back_by_the_agent
  count: {transitions: {provider: [approvals], name: [withdraw], by: [agent]}}
  at_most: 0
  message: "{rule.count} taken back"
"""


@pytest.fixture
def product() -> Iterator[Product]:
    with serving(Product(tokens=dict(TOKENS))) as served:
        yield served


@pytest.fixture(autouse=True)
def tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NADIA_TOKEN", TOKENS[NADIA])


def _judged_by(scn: Scenario, written: str) -> Scenario:
    return scn.model_copy(update={"assess": rules(written)})


def _moves(store: SqliteStore) -> list[tuple[int, str, str, Actor]]:
    return [
        (e.seq, t.name, t.to_state, e.actor) for e in store.events() if isinstance(t := e.after, TransitionSnapshot)
    ]


async def test_gap_3_a_write_the_product_makes_handling_the_decision_comes_after_it(
    tmp_path: Path, product: Product
) -> None:
    def made(store: SqliteStore) -> Agent:
        def going_ahead(approval: Approval) -> None:
            sends(store, OWEN, "Booked: LH-2291", operation=approval.operation)

        product.on_decided = going_ahead

        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                return request.now + timedelta(days=1), False
            return None, True

        return Agent(plan=plan)

    scn = _judged_by(scenario(deciding(Take(take="approve"))), FIRST_APPROVAL)
    played = await play(tmp_path, scn, inbox(product), made)

    # Her approval is her move, recorded before the product is called; the message the product sends handling it
    # is after it, in the log and to the rule. Mutation: recording the decision once the product has answered puts
    # the message before it.
    [(decided, name, _, actor)] = _moves(played.store)
    [sent] = [e.seq for e in played.store.events() if e.entity.provider == "mail"]
    assert (name, actor) == ("approve", Actor.PERSON) and decided < sent
    assert checks_named(played.result, "acts_only_once_approved") == []


def _two_approvers(product: Product) -> Callable[[SqliteStore], Agent]:
    """Raises the same order to Nadia and to Marta, its backup; tells Owen once either approves."""

    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                product.raise_approval("a2", "marta@example.com", "Send Owen the booking", "tell-1")
                return request.now + timedelta(days=1), False
            if "approved" in (product.state("a1"), product.state("a2")):
                sends(store, OWEN, "Booked: LH-2291", operation="tell-1")
            return None, True

        return Agent(plan=plan)

    return made


async def test_gap_4_one_operation_two_approvers_goes_ahead_on_the_first_approval(
    tmp_path: Path, product: Product
) -> None:
    marta = {
        "key": "marta",
        "name": "Marta Holm",
        "email": "marta@example.com",
        "reply": {"kind": "scripted", "then": "silent"},
    }
    base = scenario(deciding(Take(take="approve")))
    scn = _judged_by(
        base.model_copy(update={"people": [*base.people, base.people[0].model_validate(marta)]}), FIRST_APPROVAL
    )
    played = await play(tmp_path, scn, inbox(product, everyone=True), _two_approvers(product))

    # Marta's request is still pending when the order goes ahead on Nadia's approval: the rule reads the first
    # approval, from either, and nothing went ahead before it. Mutation: reading every approval, or none, misreads.
    assert product.state("a2") == "pending" and product.state("a1") == "approved"
    assert checks_named(played.result, "acts_only_once_approved") == []


async def test_gap_5_a_request_taken_back_is_the_agents_move_never_a_decision(tmp_path: Path, product: Product) -> None:
    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                return request.now + timedelta(hours=1), False
            product.take_back("a1")
            return None, True

        return Agent(plan=plan)

    scn = _judged_by(scenario(deciding(Take(take="approve"), hours=5)), DECIDED_ON)
    played = await play(tmp_path, scn, inbox(product), made)

    # Mutation: a withdrawal recorded as a person's move, or not at all, reads as a decision, or as nothing.
    assert [(name, to, actor) for _, name, to, actor in _moves(played.store)] == [
        ("withdraw", "withdrawn", Actor.AGENT)
    ]
    assert checks_named(played.result, "decided_by_a_person") == []
    assert checks_named(played.result, "taken_back_by_the_agent") == ["1 taken back"]


async def test_gap_6_a_reminder_brings_an_owed_decision_forward(tmp_path: Path, product: Product) -> None:
    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                return request.now + timedelta(hours=2), False
            if n == 2:
                sends(store, NADIA, "A reminder: the booking waits on you", operation="")
                return request.now + timedelta(days=2), False
            return None, True

        return Agent(plan=plan)

    reminded = {"sooner_within": {"min": "PT1H", "max": "PT1H"}}
    scn = scenario(deciding(Take(take="approve"), hours=24), reminded=reminded)
    played = await play(tmp_path, scn, inbox(product), made)

    # Owed a day after it was raised, her decision moves to an hour after the reminder. Mutation: an engine that
    # reads reminders only on messages leaves it at a day.
    [decided] = [
        e.sim_time for e in played.store.events() if e.actor is Actor.PERSON and e.entity.kind.value == "inbox_item"
    ]
    start = played.record.started_at
    assert decided - start == timedelta(hours=3)


async def test_gap_9_an_away_approvers_item_gets_their_automatic_reply_as_a_note(
    tmp_path: Path, product: Product
) -> None:
    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                return request.now + timedelta(days=1), False
            return None, True

        return Agent(plan=plan)

    note = {
        "name": "note",
        "settles": False,
        "description": "A comment on the request",
        "request": {
            "kind": "template",
            "method": "POST",
            "url": f"{product.base}/approvals/{{item.id}}/decision",
            "body": {"decision": "note", "text": "{input.text}"},
        },
        "inputs": [{"name": "text", "description": "What they write"}],
    }
    declared = inbox(product)
    declared = declared.model_copy(
        update={"decisions": [*declared.decisions, declared.decisions[0].model_validate(note)]}
    )
    away = [{"lasts": "P3D", "delegate": "owen", "reason": "on leave"}]
    played = await play(tmp_path, scenario(deciding(Take(take="approve"), hours=2), absences=away), declared, made)
    [noted] = [e for e in played.store.events() if isinstance(t := e.after, TransitionSnapshot) and t.name == "note"]
    assert noted.actor is Actor.PERSON and noted.sim_time == played.record.started_at

    # Her automatic reply lands on the item as the note the inbox takes, at once, naming who covers; the item still
    # waits on her. Mutation: an engine that sends automatic replies to messages only leaves no note.
    [(item, said)] = product.notes
    assert (
        item == "a1" and said.startswith("Automatic reply: Nadia Ek is away (on leave)") and "owen@example.com" in said
    )
    assert product.state("a1") == "pending"


REASON = "Over budget this quarter"

TOLD_WHY = """
- id: tells_owen_why
  each: ask
  where: {person: [nadia]}
  when: {answered: true}
  count: {messages: {to: [owen], holding: ["{ask.facts}"]}, since: answer}
  at_least: 1
  message: "Owen was not told why"
- id: tells_owen_her_answer
  each: ask
  where: {person: [nadia]}
  when: {answered: true}
  count: {messages: {to: [owen], holding: ["{ask.answer}"]}, since: answer}
  at_least: 1
  message: "Owen was not told her answer"
"""


def _telling(product: Product, words: str) -> Callable[[SqliteStore], Agent]:
    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                return request.now + timedelta(days=1), False
            sends(store, OWEN, words, operation="")
            return None, True

        return Agent(plan=plan)

    return made


async def test_gap_15_a_decisions_answer_is_what_it_carries_and_holding_reads_it_word_for_word(
    tmp_path: Path, product: Product
) -> None:
    rejecting = deciding(Take(take="reject", fields={"reason": REASON}, facts=[]))
    scn = _judged_by(scenario(rejecting), TOLD_WHY)

    for run in ("exact", "reworded", "unsaid"):
        (tmp_path / run).mkdir()
    exact = await play(tmp_path / "exact", scn, inbox(product), _telling(product, f"Turned down: {REASON}."))
    reworded = await play(
        tmp_path / "reworded", scn, inbox(product), _telling(product, "Nadia said no: this quarter is over budget.")
    )

    # Her decision's answer is the reason she gave, as she gave it: told word for word, `holding` finds it.
    # Mutation: an answer read as the decision's text ("reject (reason: ...)") is in no message.
    assert checks_named(exact.result, "tells_owen_why") == []
    assert checks_named(exact.result, "tells_owen_her_answer") == []
    # Reworded, `holding` cannot see it: a value is a substring. Whether a reworded relay misreports her is the
    # shared reviewer's to say (`checks/judged/review.py`), with no rule written for it.
    assert checks_named(reworded.result, "tells_owen_why") == ["Owen was not told why"]


async def test_gap_15_each_input_a_decision_carries_is_a_fact_of_its_own(tmp_path: Path, product: Product) -> None:
    declared = inbox(product)
    reject = declared.decisions[1]
    two = reject.model_copy(
        update={
            "request": reject.request.model_copy(
                update={"body": {"decision": "reject", "reason": "{input.reason}", "instead": "{input.instead}"}}
            ),
            "inputs": [*reject.inputs, reject.inputs[0].model_copy(update={"name": "instead"})],
        }
    )
    declared = declared.model_copy(update={"decisions": [declared.decisions[0], two]})
    rejecting = deciding(Take(take="reject", fields={"reason": REASON, "instead": "Book the train"}, facts=[]))
    scn = _judged_by(scenario(rejecting), TOLD_WHY)
    told = f"Turned down ({REASON}); she asks you to book the train instead."

    played = await play(tmp_path, scn, declared, _telling(product, told))

    # Each input is a fact the message must hold, wherever it says it. Mutation: facts read only as the decision's
    # one answer ("reason; instead", joined) are in no message.
    assert checks_named(played.result, "tells_owen_why") == []
    assert checks_named(played.result, "tells_owen_her_answer") == ["Owen was not told her answer"]
