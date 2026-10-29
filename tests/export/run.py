"""A run written through the real store and the real Slack, Jira and Google Workspace seeds, for `minutehand export`.

A team of three: Mira owns the release and asks the agent for help in her DM, by Friday; Teo and Una work on it. The workspace
already holds #release with a thread from two days before, a group DM with Teo and Una, a Jira ticket in Platform
and a sheet in the Delivery folder. In wake 1 the agent answers Mira, then rewrites its answer; asks Teo in his DM;
writes to the group; drafts an item for Mira's approval; and creates a document itself. In wake 2, an hour later,
Teo answers and Mira approves. Nothing records an end for the run unless the test writes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from minutehand.adapters.providers.google_workspace.provider import build as build_google
from minutehand.adapters.providers.jira.provider import build as build_jira
from minutehand.adapters.providers.slack import seed as slack_seed
from minutehand.adapters.providers.slack import state as slack_state
from minutehand.adapters.providers.slack import wire as slack_wire
from minutehand.adapters.providers.slack.provider import build as build_slack
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import Checkpoint, Remembered, write_checkpoint
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import (
    DocumentKind,
    Person,
    Scenario,
    SeededChannel,
    SeededDocument,
    SeededPost,
    SeededTicket,
    WorkingHours,
)
from minutehand.domain.world import (
    Actor,
    Change,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    ItemStatus,
    MessageSnapshot,
    Operation,
)
from minutehand.session import RUNS, SCENARIO, WORLD

T0 = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)  # a Monday
RUN = "export000001"
GOAL = "Can you get the release checklist signed off by Friday?"
FIRST_ANSWER = "On it."
ANSWER = "On it: I'll ask Teo about the migration and Una about the notes."
TO_TEO = "Teo, is the migration ready to run on Thursday?"
TO_GROUP = "Teo, Una: the checklist is in the Delivery folder."
TEO_ANSWERS = "Yes, it runs Thursday at 10."
OLD_POST = "Release train leaves Friday."
OLD_REPLY = "Notes are half done."
DRAFT = "Create ticket: Run the migration on Thursday"
MADE = "Release notes, first draft"


def scenario() -> Scenario:
    return Scenario(
        name="release_checklist",
        goal=GOAL,
        owner="mira",
        starts_at=T0,
        runs_for=timedelta(days=5),
        deadline_after=timedelta(days=4),
        people=[
            Person(
                key="mira",
                name="Mira Holt",
                email="mira@example.com",
                title="Release manager",
                working_hours=WorkingHours(timezone="Europe/Lisbon", opens=time(9), closes=time(18)),
            ),
            Person(key="teo", name="Teo Varga", email="teo@example.com", title="Engineer"),
            Person(key="una", name="Una Berg", email="una@example.com"),
        ],
        channels=[
            SeededChannel(
                provider="slack",
                name="release",
                members=["mira", "teo", "una"],
                history=[
                    SeededPost(
                        by="mira",
                        text=OLD_POST,
                        ago=timedelta(days=2),
                        replies=[SeededPost(by="una", text=OLD_REPLY, ago=timedelta(days=2) - timedelta(hours=1))],
                    )
                ],
            ),
            SeededChannel(provider="slack", members=["teo", "una"]),
        ],
        tickets=[
            SeededTicket(provider="jira", project="Platform", title="Migrate the orders table", assignee="teo"),
        ],
        documents=[
            SeededDocument(
                provider="google_workspace",
                title="Release checklist",
                kind=DocumentKind.SPREADSHEET,
                rows=[["Step", "Owner"], ["Migration", "Teo"], ["Notes", "Una"]],
                folder="Delivery",
            ),
        ],
    )


@dataclass(frozen=True)
class Written:
    state: Path
    run_id: str
    goal: str
    """The ts of Mira's goal in her DM."""
    answer: str
    to_teo: str
    to_group: str
    teo_answers: str
    answer_edited_at: datetime
    wake_two: datetime
    ended: datetime
    """The run's last event's moment."""


def _post(slack: SlackWorld, channel: str, author: str, text: str, actor: Actor, at: datetime) -> str:
    return slack_seed.write_post(slack, channel, author, text, None, [], at=int(at.timestamp()), actor=actor)


def _checkpoint(store: SqliteStore, clock: RunClock) -> None:
    write_checkpoint(
        store,
        Checkpoint(wake=clock.wake(), now=clock.now(), replies=0, pending=[], agent=Remembered(memory="0" * 64)),
    )


def write_run(state: Path) -> Written:
    played = scenario()
    directory = state / RUNS / RUN
    directory.mkdir(parents=True)
    (directory / SCENARIO).write_text(played.model_dump_json(), encoding="utf-8")
    clock = RunClock(T0)
    store = SqliteStore(directory / WORLD, RUN, clock)
    for provider in (build_slack(), build_jira(), build_google()):
        provider.seed(played, store)
    _checkpoint(store, clock)
    slack = SlackWorld(store)
    bot = slack.team.bot_user_id
    mira, teo = slack_state.user_id("mira"), slack_state.user_id("teo")
    to_mira = slack_state.conversation_id([bot, mira])
    to_teo = slack_state.conversation_id([bot, teo])
    group = slack_state.conversation_id([bot, teo, slack_state.user_id("una")])

    clock.begin_wake()
    slack.open_conversation([bot, mira], created=int(T0.timestamp()), actor=Actor.PERSON)
    goal = _post(slack, to_mira, mira, GOAL, Actor.PERSON, clock.now())
    answer = _post(slack, to_mira, bot, FIRST_ANSWER, Actor.AGENT, clock.now())
    clock.jump(T0 + timedelta(minutes=2))
    edited = slack_wire.SlackMessage(ts=answer, user=bot, text=ANSWER, team=slack.team.id)
    slack.write(
        slack_state.message_ref(answer),
        edited,
        operation=Operation.UPDATE,
        actor=Actor.AGENT,
        parent=to_mira,
        after=MessageSnapshot(text=ANSWER, channel=to_mira, recipient_emails=["mira@example.com"]),
    )
    answer_edited_at = clock.now()
    slack.open_conversation([bot, teo], created=int(clock.now().timestamp()), actor=Actor.AGENT)
    asked = _post(slack, to_teo, bot, TO_TEO, Actor.AGENT, clock.now())
    grouped = _post(slack, group, bot, TO_GROUP, Actor.AGENT, clock.now())
    store.apply(
        Change(
            entity=EntityRef(provider="approvals", kind=EntityKind.INBOX_ITEM, external_id="item-1"),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body="{}",
            after=InboxItemSnapshot(
                inbox="approvals",
                item_id="item-1",
                person="mira",
                waits_on="mira@example.com",
                summary=DRAFT,
                decisions=["approve", "reject"],
                status=ItemStatus.PENDING,
            ),
        )
    )
    store.apply(
        Change(
            entity=EntityRef(provider="google_workspace", kind=EntityKind.DOCUMENT, external_id="doc-made"),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body="{}",
            after=DocumentSnapshot(title=MADE, parent="Delivery"),
        )
    )
    _checkpoint(store, clock)

    clock.jump(T0 + timedelta(hours=1))
    wake_two = clock.now()
    clock.begin_wake()
    answered = _post(slack, to_teo, teo, TEO_ANSWERS, Actor.PERSON, clock.now())
    store.apply(
        Change(
            entity=EntityRef(provider="approvals", kind=EntityKind.INBOX_ITEM, external_id="item-1"),
            operation=Operation.UPDATE,
            actor=Actor.PERSON,
            body="{}",
            after=InboxItemSnapshot(
                inbox="approvals",
                item_id="item-1",
                person="mira",
                waits_on="mira@example.com",
                summary=DRAFT,
                decisions=["approve", "reject"],
                status=ItemStatus.DECIDED,
                decision="approve",
            ),
        )
    )
    clock.jump(T0 + timedelta(hours=1, minutes=5))
    _checkpoint(store, clock)
    store.close()
    return Written(
        state=state,
        run_id=RUN,
        goal=goal,
        answer=answer,
        to_teo=asked,
        to_group=grouped,
        teo_answers=answered,
        answer_edited_at=answer_edited_at,
        wake_two=wake_two,
        ended=T0 + timedelta(hours=1, minutes=5),
    )
