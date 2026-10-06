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
from minutehand.application.replier_model import DECISION_PROMPT_VERSION, PeopleReplier, WrittenDecision, WrittenInput
from minutehand.application.restore import SeenCall
from minutehand.application.rewind import fork_run
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentReport, AgentStatus, AgentUnderTest, Command, StateHooks, WakeRequest
from minutehand.domain.experiment import Fork, Override, PersonChange
from minutehand.domain.scenario import Answers, DelayRange, ScriptedDecision
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
    def rule(received: Received) -> WrittenDecision:
        return WrittenDecision(decision="reject", inputs=[WrittenInput(name="reason", value="We booked one already")])

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

    asked = fake.for_schema("WrittenDecision")
    assert len(asked) == 1
    assert "Send Owen the Lakeside booking" in asked[0].last
    assert '"approve"' in asked[0].last and 'input "reason" (required): Why it is turned down' in asked[0].last
    assert decided(played.store.events()) == [("reject", T0 + hours(3))]
    assert product.approvals["a1"].reason == "We booked one already"
    written = played.store.replies()[0].written_by
    assert written is not None and written.prompt_version == DECISION_PROMPT_VERSION


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
    scn = scenario(deciding(ScriptedDecision(decision="approve"), hours=2))
    declared = inbox(product)
    hooks = StateHooks(snapshot=PASS, restore=PASS, quiet=timedelta(milliseconds=10))
    agent = AgentUnderTest(name="restorable", wakes=[Command(argv=["in-process"])], inboxes=[declared], state=hooks)
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
        state_dir=tmp_path / "state",
        traffic=_Quiet(),
        inboxes=Inboxes(scn, [reach]),
    )
    assert decided(parent_store.events()) == [("approve", T0 + hours(2))]
    at_seq, kept = next((s, c) for s, c in checkpoints(parent_store).items() if c.wake == 1)
    # the restore, as the agent's own hooks would do it: its report, and its product, as they were then
    assert kept.agent.kind == "restorable"
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
        replier_for=lambda s: PeopleReplier(s, None, [declared]),
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
    rejecting = deciding(ScriptedDecision(decision="reject", inputs={"reason": "Changed my mind"}), hours=5)
    assert await _fork(tmp_path, product, [PersonChange(person="nadia", reply=rejecting)]) == [
        ("reject", T0 + hours(5))
    ]
    assert product.state("a1") == "rejected"
