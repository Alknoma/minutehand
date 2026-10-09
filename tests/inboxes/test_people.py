"""Who decides: a model-written person through the model port, and a fork that replays a decision made before it or,
with the approver changed, makes another."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.agent.inboxes import HttpInboxReach
from minutehand.adapters.model.openai_compatible import OpenAICompatible
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import checkpoints
from minutehand.application.inboxes import Inboxes
from minutehand.application.orchestrator import Reach, Services, run_scenario
from minutehand.application.people import TRANSITION_PROMPT_VERSION, WrittenField, WrittenTransition
from minutehand.application.replier import PeopleReplier
from minutehand.application.rewind import fork_run
from minutehand.application.run_clock import RunClock
from minutehand.application.traffic import SeenCall
from minutehand.domain.agent import AgentReport, AgentStatus, AgentUnderTest, Command, WakeRequest
from minutehand.domain.experiment import Fork, Override, PersonChange, ReplyAt
from minutehand.domain.scenario import Answers, DelayRange, Take
from minutehand.domain.world import Actor, InboxItemSnapshot, WorldEvent
from tests.inboxes.product import Product, serving
from tests.inboxes.support import NADIA, T0, TOKENS, deciding, hours, inbox, play, scenario
from tests.model.fake_completions import Received, fake_completions

PASS = [sys.executable, "-c", "pass"]


@pytest.fixture
def product() -> Iterator[Product]:
    with serving(Product(tokens=dict(TOKENS))) as served:
        yield served


@pytest.fixture(autouse=True)
def nadias_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NADIA_TOKEN", TOKENS[NADIA])


def decided(events: list[WorldEvent]) -> list[tuple[str | None, datetime]]:
    return [
        (e.after.decision, e.sim_time)
        for e in events
        if e.actor is Actor.PERSON and isinstance(e.after, InboxItemSnapshot)
    ]


async def test_a_model_written_approver_is_shown_the_item_and_its_decisions_and_picks_one(
    tmp_path: Path, product: Product
) -> None:
    def rule(received: Received) -> WrittenTransition:
        return WrittenTransition(take="reject", fields=[WrittenField(name="reason", value="We booked one already")])

    nadia = Answers(delay=DelayRange(shortest=hours(3), longest=hours(3)))

    def raising(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
        if n == 1:
            product.raise_approval("a1", NADIA, "Send Owen the Lakeside booking", "tell-1")
            return request.now + timedelta(days=1), False
        return None, True

    from tests.inboxes.support import Agent

    async with fake_completions(rule) as fake:
        model = OpenAICompatible(base_url=fake.base_url, api_key="sk-test", model_id="people-1")
        played = await play(tmp_path, scenario(nadia), inbox(product), Agent(plan=raising), model=model)

    # She is shown the item and each decision with what it takes, as any person is shown what they can do with an
    # item (`person-transition/1`), and her pick goes through the product as she makes it.
    asked = fake.for_schema("WrittenTransition")
    assert len(asked) == 1
    assert "Send Owen the Lakeside booking" in asked[0].last
    assert '"approve"' in asked[0].last and 'field "reason" (required): Why it is turned down' in asked[0].last
    assert decided(played.store.events()) == [("reject", T0 + hours(3))]
    assert product.approvals["a1"].reason == "We booked one already"
    [call] = [c for c in played.store.person_calls() if c.answer is not None]
    assert call.prompt_version == TRANSITION_PROMPT_VERSION


@dataclass
class _Quiet:
    def last_call(self) -> SeenCall | None:
        return None

    def waiting(self) -> list[str]:
        return []


@dataclass
class _Restorable:
    """An agent in this process that raises one approval, and answers its report when asked: its whole state is its
    report and the product's approval, which a test puts back as the restore would."""

    product: Product
    report_now: AgentReport = field(default_factory=lambda: AgentReport(status=AgentStatus.IDLE))
    wakes: int = 0

    async def wake(self, request: WakeRequest) -> None:
        self.wakes += 1
        if self.wakes == 1:
            self.product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
            self.report_now = AgentReport(status=AgentStatus.IDLE, next_wake=request.now + timedelta(days=1))
        else:
            self.report_now = AgentReport(status=AgentStatus.DONE)

    async def settled(self) -> AgentReport:
        return self.report_now

    async def report(self) -> AgentReport:
        return self.report_now


async def _fork(tmp_path: Path, product: Product, change: list[Override]) -> list[tuple[str | None, datetime]]:
    scn = scenario(deciding(Take(take="approve"), hours=2))
    declared = inbox(product)
    agent = AgentUnderTest(name="restorable", wakes=[Command(argv=["in-process"])], inboxes=[declared])
    driver = _Restorable(product)
    reach = HttpInboxReach(declared, {"nadia": TOKENS[NADIA]})
    clock = RunClock(scn.starts_at)
    parent_store = SqliteStore(tmp_path / "world.db", "parent", clock)
    record = await run_scenario(
        scenario=scn,
        agent=agent,
        reach=Reach(main=driver),
        store=parent_store,
        clock=clock,
        services=Services(providers=[]),
        replier=PeopleReplier(scn, None, [declared]),
        traffic=_Quiet(),
        inboxes=Inboxes(scn, [reach]),
    )
    assert decided(parent_store.events()) == [("approve", T0 + hours(2))]
    at_seq, kept = next((s, c) for s, c in checkpoints(parent_store).items() if c.wake == 1)
    # this agent keeps its state in this process and in its product, outside its memory: put back by hand, as
    # they were then, so its report after the fork is the one at the checkpoint
    assert kept.agent.kind == "remembered"
    product.approvals["a1"].state = "pending"
    driver.report_now = AgentReport(status=AgentStatus.IDLE, next_wake=T0 + timedelta(days=1))

    def open_parent(fork_clock: object) -> SqliteStore:
        return SqliteStore(tmp_path / "world.db", "parent", fork_clock)  # type: ignore[arg-type]

    children = await fork_run(
        fork=Fork(parent_run="parent", at_seq=at_seq, overrides=change),
        parent=record,
        open_parent=open_parent,
        run_id="child",
        scenario=scn,
        agent=agent,
        reach=Reach(main=driver),
        services=Services(providers=[]),
        replier_for=lambda s, pins: PeopleReplier(s, None, [declared], pins=pins),
        state_dir=tmp_path / "state",
        traffic=_Quiet(),
        inboxes=Inboxes(scn, [reach]),
    )
    child = SqliteStore(tmp_path / "world.db", children[0].run_id, RunClock(T0))
    return decided([e for e in child.events() if e.seq > at_seq])


async def test_a_fork_before_the_decision_replays_it(tmp_path: Path, product: Product) -> None:
    assert await _fork(tmp_path, product, []) == [("approve", T0 + hours(2))]
    assert product.state("a1") == "approved"


async def test_a_fork_that_changes_the_approver_has_them_reject(tmp_path: Path, product: Product) -> None:
    rejecting = deciding(Take(take="reject", fields={"reason": "Changed my mind"}), hours=5)
    change = PersonChange(person="nadia", reply=rejecting.reply, takes=rejecting.takes)
    assert await _fork(tmp_path, product, [change]) == [("reject", T0 + hours(5))]
    assert product.state("a1") == "rejected"


async def test_gap_7_a_fork_pins_the_moment_of_an_owed_decision(tmp_path: Path, product: Product) -> None:
    pinned = ReplyAt(person="nadia", provider="approvals", to_ask=1, after=timedelta(hours=5))

    # Her first item in the inbox lands exactly five hours after it began to wait, where her delay drew two.
    # Mutation: an engine that leaves item pins to the replier, which pins only messages, keeps two.
    assert await _fork(tmp_path, product, [pinned]) == [("approve", T0 + hours(5))]
