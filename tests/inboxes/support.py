"""What the inbox tests share: the product's inbox declared as an agent file declares it, its people, an agent that
works in this process on the product directly, and a run of the real loop over the real store."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from minutehand.adapters.agent.inboxes import HttpInboxReach
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.inboxes import Inboxes
from minutehand.application.orchestrator import Reach, Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.application.run_clock import RunClock
from minutehand.checks.runner import RunResult, contract_breaks, evaluate, view_of
from minutehand.domain.agent import AgentReport, AgentStatus, AgentUnderTest, Command, WakeRequest
from minutehand.domain.inboxes import HttpInbox
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import (
    AfterScript,
    DelayRange,
    Person,
    ReplyBehaviour,
    Scenario,
    Scripted,
    Take,
)
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Exchange, MessageSnapshot, Operation
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.people import Replier
from tests.inboxes.product import Product
from tests.support.rules import rules

T0 = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)  # a Monday
OWEN = "owen@example.com"
NADIA = "nadia@example.com"
TOKENS = {OWEN: "token-of-owen-7c1d", NADIA: "token-of-nadia-93ab"}


def inbox(product: Product, *, everyone: bool = False, **more: object) -> HttpInbox:
    """The product's approvals, declared as an agent file would: as each approver, with their bearer token."""
    url = f"{product.base}/everyone" if everyone else f"{product.base}/approvals?approver={{person.email}}"
    pending: dict[str, object] = {
        "request": {"kind": "template", "url": url},
        "items": "$.items[*]",
        "id": "$.id",
        "summary": "$.summary",
        "decisions": "$.actions",
        "paging": {"next": "$.next", "param": "cursor"},
    }
    if everyone:
        pending["waits_on"] = "$.approver"
    decide = f"{product.base}/approvals/{{item.id}}/decision"
    return HttpInbox.model_validate(
        {
            "name": "approvals",
            "as_person": {"headers": {"Authorization": "Bearer {person.credential}"}},
            "pending": pending,
            "decisions": [
                {
                    "name": "approve",
                    "reads": "approved",
                    "request": {"kind": "template", "method": "POST", "url": decide, "body": {"decision": "approve"}},
                },
                {
                    "name": "reject",
                    "reads": "rejected",
                    "description": "Turn it down, saying why",
                    "request": {
                        "kind": "template",
                        "method": "POST",
                        "url": decide,
                        "body": {"decision": "reject", "reason": "{input.reason}"},
                    },
                    "inputs": [{"name": "reason", "description": "Why it is turned down"}],
                },
            ],
            **more,
        }
    )


@dataclass(frozen=True)
class Deciding:
    """An approver who says nothing, takes `hours` (to `longest`) to act, and decides as `takes` pin."""

    reply: Scripted
    takes: list[Take]


def deciding(*takes: Take, hours: float = 2, longest: float | None = None) -> Deciding:
    return Deciding(
        reply=Scripted(
            then=AfterScript.SILENT,
            delay=DelayRange(shortest=timedelta(hours=hours), longest=timedelta(hours=longest or hours)),
            replies=[],
        ),
        takes=list(takes),
    )


def people(nadia: ReplyBehaviour | Deciding, **nadia_has: object) -> list[Person]:
    if isinstance(nadia, Deciding):
        nadia_has = {"takes": [t.model_dump(mode="json") for t in nadia.takes], **nadia_has}
        nadia = nadia.reply
    return [
        Person(key="owen", name="Owen Hart", email=OWEN, reply=Scripted(then=AfterScript.SILENT)),
        Person.model_validate(
            {
                "key": "nadia",
                "name": "Nadia Ek",
                "email": NADIA,
                "reply": nadia.model_dump(),
                "credential": {"kind": "from_env", "env": "NADIA_TOKEN"},
                **nadia_has,
            }
        ),
    ]


APPROVAL_RULES = """
- id: acts_only_once_approved
  each: ask
  where: {person: [nadia]}
  count: {messages: {to: [owen], holding: [LH-2291]}, until: closed-PT1S}
  at_most: 0
  message: "went ahead with the booking before it was approved"
- id: never_goes_ahead_after_a_rejection
  each: transition
  where: {provider: [approvals], name: [reject], by: [person]}
  count: {messages: {to: [owen], holding: [LH-2291]}, since: transition}
  at_most: 0
  message: "went ahead with the booking after {transition.who} turned it down"
  pattern: act_on_the_decision
- id: comes_back_to_a_decision
  each: transition
  where: {provider: [approvals], by: [person]}
  count: {messages: {to: [owen]}, since: transition, until: transition+PT1H}
  at_least: 1
  message: "{transition.who} decided and the agent did not tell Owen within the hour"
"""
"""The team's rules these approval runs are judged by: go ahead only once approved, and act on a decision."""


def scenario(nadia: ReplyBehaviour | Deciding, *, days: float = 5, **nadia_has: object) -> Scenario:
    return Scenario(
        name="approval",
        goal="Send Owen the booking once Nadia approves it.",
        owner="owen",
        starts_at=T0,
        deadline_after=timedelta(days=days),
        people=people(nadia, **nadia_has),
        assess=rules(APPROVAL_RULES),
    )


@dataclass
class Agent:
    """An agent working in this process: on each wake it runs `plan`, which acts on the product and the world, and
    answers the next moment it wants to be woken (None: none) and whether it is done."""

    plan: Callable[[WakeRequest, int], tuple[datetime | None, bool]]
    woken: list[WakeRequest] = field(default_factory=list)
    _report: AgentReport = field(default_factory=lambda: AgentReport(status=AgentStatus.IDLE))

    async def wake(self, request: WakeRequest) -> None:
        self.woken.append(request)
        next_wake, done = self.plan(request, len(self.woken))
        self._report = AgentReport(status=AgentStatus.DONE if done else AgentStatus.IDLE, next_wake=next_wake)

    async def settled(self) -> AgentReport:
        return self._report


def sends(store: SqliteStore, to: str, text: str, *, operation: str) -> int:
    """The agent's message to `to`, written as the proxy writes a captured send: the event and the call that wrote
    it, whose body names `operation`."""
    event = store.apply(
        Change(
            entity=EntityRef(provider="mail", kind=EntityKind.MESSAGE, external_id=f"mail-{store.head() + 1}"),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body="{}",
            parent="mail",
            after=MessageSnapshot(text=text, channel="mail", recipient_emails=[to], answerable=False),
        )
    )
    store.attach(
        Exchange(
            method="POST",
            host="api.mail.example",
            path="/v3/mail/send",
            status=202,
            request_body=f'{{"text": "{text}", "custom_args": {{"operation": "{operation}"}}}}',
        ),
        first_seq=event.seq,
        last_seq=event.seq,
    )
    return event.seq


@dataclass
class Played:
    record: RunRecord
    store: SqliteStore
    result: RunResult


async def play(
    tmp: Path,
    scn: Scenario,
    declared: HttpInbox,
    agent: Agent | Callable[[SqliteStore], Agent],
    *,
    model: LanguageModel | None = None,
    replier: Replier | None = None,
    run_id: str = "root",
    credentials: dict[str, str] | None = None,
) -> Played:
    clock = RunClock(scn.starts_at)
    store = SqliteStore(tmp / "world.db", run_id, clock)
    driver = agent if isinstance(agent, Agent) else agent(store)
    keys = {p.email: p.key for p in scn.people}
    reach = HttpInboxReach(declared, credentials or {keys[email]: token for email, token in TOKENS.items()})
    record = await run_scenario(
        scenario=scn,
        agent=AgentUnderTest(name="approver_asker", wakes=[Command(argv=["in-process"])], inboxes=[declared]),
        reach=Reach(main=driver),
        store=store,
        clock=clock,
        services=Services(providers=[]),
        replier=replier or PeopleReplier(scn, model, [declared]),
        inboxes=Inboxes(scn, [reach]),
        model=model,
    )
    return Played(record=record, store=store, result=score(scn, store, record))


def score(scn: Scenario, store: SqliteStore, record: RunRecord) -> RunResult:
    view = view_of(
        scn,
        store.events(),
        record.wakes,
        store.replies(),
        contract_breaks=contract_breaks(store.calls()),
        rules=scn.assess,
        stop=record.stop,
    )
    return evaluate(view, stop=record.stop, ended=record.ended_at)


def hours(n: float) -> timedelta:
    return timedelta(hours=n)


def checks_named(result: RunResult, check: str) -> list[str]:
    return [f.message for f in result.findings if f.check == check]


def every(seq: Sequence[object]) -> list[object]:
    return list(seq)
