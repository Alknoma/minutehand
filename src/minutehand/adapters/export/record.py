"""`minutehand export`: one run as a record a reader outside Minutehand can tell its story from (docs/exporting.md).

The record holds facts only, in the run's own ids: who was in it, every conversation and message, the agent's wakes,
what was already written down, what the agent drafted for a person's approval and what it did itself. It is read
from the run's world as it stood at its end and from its scenario; what only a provider can read from its own JSON
(a channel's name, a message's author, an issue's key) the provider says (`slack.present`, `jira.present`).

Conversations are Slack's: a run whose people talk with the agent anywhere else is refused, never shown in part.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from minutehand import session
from minutehand.adapters.providers.jira import present as jira_present
from minutehand.adapters.providers.slack import present as slack_present
from minutehand.adapters.providers.slack.manifest import MANIFEST as SLACK
from minutehand.adapters.proxy.registry import Registry
from minutehand.application.inboxes import items_in
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.checks import WakeRecord
from minutehand.domain.presentation import (
    ApprovalStatus,
    ConversationKind,
    PresentedAuthor,
    PresentedConversation,
    RecordAction,
    RecordAgent,
    RecordApproval,
    RecordChannel,
    RecordConversation,
    RecordDirect,
    RecordGroup,
    RecordItem,
    RecordMessage,
    RecordPerson,
    RecordRun,
    RecordWake,
    ReplayRecord,
)
from minutehand.domain.scenario import Person, Scenario, TicketState
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    EntityKind,
    InboxItemSnapshot,
    ItemStatus,
    MessageSnapshot,
    Operation,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.ports.store import Store
from minutehand.session import RunRefused

CHANGES = (Operation.CREATE, Operation.UPDATE, Operation.DELETE)
SPREADSHEET = "spreadsheet"
FOLDER = ".folder"


@dataclass(frozen=True)
class Exported:
    record: ReplayRecord
    left_out: list[str]
    """What the record does not hold, and why, one line each: for the person who runs the export."""


def export(state: Path, run_id: str) -> Exported:
    """The run's record. Refused for a run another chat than Slack holds conversations in."""
    entry = session.find(state, run_id)
    scenario = session.scenario_of(state, run_id)
    agent = session.agent_of(state, run_id)
    with session.reading(state, run_id) as world:
        events = world.events(
            kinds=[
                EntityKind.MESSAGE,
                EntityKind.TICKET,
                EntityKind.COMMENT,
                EntityKind.DOCUMENT,
                EntityKind.INBOX_ITEM,
            ]
        )
        wakes = session.wakes_of(state, run_id, world)
        ended = session.load(state, run_id).record.ended_at if entry.finished else _last(world, scenario)
        return _record(run_id, scenario, agent, world, events, wakes, ended, _ran_on(world))


def _last(world: Store, scenario: Scenario) -> datetime:
    """A run still being written, or one that died before its record: its last event's moment, as the viewer reads
    it (`web.reading`)."""
    last = world.events(since=world.head() - 1)
    return max([e.sim_time for e in last] + [scenario.starts_at])


def _ran_on(world: Store) -> datetime:
    """When the run was performed: its first event's moment by the machine's clock."""
    first = world.events(until=1)
    if not first:
        raise RunRefused("the run's world holds no event yet")
    return first[0].wall_time


def _record(
    run_id: str,
    scenario: Scenario,
    agent: AgentUnderTest | None,
    world: Store,
    events: list[WorldEvent],
    wakes: list[WakeRecord],
    ended: datetime,
    ran_on: datetime,
) -> Exported:
    left_out: list[str] = []
    titles = {m.key: m.title for m in Registry.installed().manifests}
    keys = [p.key for p in scenario.people]
    by_email = {p.email.lower(): p.key for p in scenario.people if p.email is not None}

    elsewhere = sorted({e.entity.provider for e in events if e.entity.kind is EntityKind.MESSAGE} - {SLACK.key})
    if elsewhere:
        raise RunRefused(
            f"the run's people talk with the agent in {', '.join(elsewhere)}, and the export shows Slack's "
            "conversations only (docs/exporting.md)"
        )

    on_slack = any(e.entity.provider == SLACK.key for e in events)
    shown_as = slack_present.agent(world) if on_slack else None
    # The agent file names the agent; its Slack bot shows Minutehand's default name unless the scenario seeds another.
    named = agent.name if agent is not None else (shown_as.name if shown_as is not None else None) or "agent"
    agent_key = _key(agent.name if agent is not None else named, taken=set(keys))
    accounts = dict(slack_present.accounts(world, keys)) if on_slack else {}

    messages, conversations = _messages(world, events, keys, agent_key, on_slack, left_out)
    return Exported(
        record=ReplayRecord(
            run=RecordRun(
                id=run_id,
                scenario=scenario.name,
                timezone=_timezone(scenario),
                start=scenario.starts_at,
                end=ended,
                ranOn=ran_on.date(),
                chat=titles[SLACK.key],
                markup="slack",
            ),
            agent=RecordAgent(key=agent_key, name=named, chatId=shown_as.account if shown_as is not None else None),
            people=[
                RecordPerson(
                    key=p.key, name=p.name, title=p.title, chatId=accounts[p.key] if p.key in accounts else None
                )
                for p in scenario.people
            ],
            conversations=conversations,
            messages=messages,
            wakes=_wakes(wakes, ended),
            actions=_actions(events, titles, world, left_out),
            items=_items(events, titles, world, by_email),
            approvals=_approvals(events, left_out),
        ),
        left_out=left_out,
    )


def _key(name: str, taken: set[str]) -> str:
    """The agent's key in the record: its name in lower case, unless a person has it."""
    key = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    key = key if re.match(r"^[a-z]", key) else f"agent-{key}".rstrip("-")
    return key if key not in taken else "agent"


def _timezone(scenario: Scenario) -> str:
    """The scenario owner's timezone, else its first person's: the company's own clock, which every time is shown in."""
    owner: Person | None = next((p for p in scenario.people if p.key == scenario.owner), None)
    person = owner if owner is not None else scenario.people[0]
    return person.working_hours.timezone if person.working_hours is not None else "UTC"


def _wakes(wakes: list[WakeRecord], ended: datetime) -> list[RecordWake]:
    """Each wake from its moment to the next one's; the last to the run's end."""
    ordered = sorted(wakes, key=lambda w: w.index)
    return [
        RecordWake.model_validate(
            {"n": w.index, "from": w.sim_time, "to": ordered[i + 1].sim_time if i + 1 < len(ordered) else ended}
        )
        for i, w in enumerate(ordered)
    ]


def _messages(
    world: Store,
    events: list[WorldEvent],
    keys: list[str],
    agent_key: str,
    on_slack: bool,
    left_out: list[str],
) -> tuple[list[RecordMessage], list[RecordConversation]]:
    """One record per message still in the workspace: when it was first sent (or, for one already there, when Slack
    dates it), its last words, and when its words last changed."""
    if not on_slack:
        return [], []
    authors: dict[str, PresentedAuthor] = {a.message: a for a in slack_present.authors(world, keys)}
    versions: dict[str, list[WorldEvent]] = {}
    for event in events:
        if (
            event.entity.provider == SLACK.key
            and event.entity.kind is EntityKind.MESSAGE
            and event.operation in CHANGES
        ):
            versions.setdefault(event.entity.external_id, []).append(event)
    found: list[RecordMessage] = []
    gone = nobody = unlisted = 0
    for ts, written in versions.items():
        said = [e for e in written if isinstance(e.after, MessageSnapshot)]
        if ts not in authors or any(e.operation is Operation.DELETE for e in written) or not said:
            gone += 1
            continue
        author = authors[ts]
        if not author.listed:
            unlisted += 1
            continue
        by = agent_key if author.agent else author.person
        if by is None:
            nobody += 1
            continue
        first, last = said[0], said[-1]
        assert isinstance(first.after, MessageSnapshot) and isinstance(last.after, MessageSnapshot)
        edits = [
            later
            for earlier, later in pairwise(said)
            if isinstance(earlier.after, MessageSnapshot)
            and isinstance(later.after, MessageSnapshot)
            and later.after.text != earlier.after.text
        ]
        before = first.wake == 0 and first.actor is Actor.SCENARIO
        found.append(
            RecordMessage(
                id=ts,
                at=author.written_at if before else first.sim_time,
                by=by,
                conversation=last.after.channel,
                replyTo=last.after.thread_of if last.after.thread_of not in (None, ts) else None,
                text=last.after.text,
                wake=first.wake,
                edited=bool(edits),
                editedUntil=edits[-1].sim_time if edits else None,
            )
        )
    if gone:
        left_out.append(f"{gone} messages deleted, or gone from the workspace, by the run's end")
    if unlisted:
        left_out.append(f"{unlisted} ephemeral messages, each shown to one member once")
    if nobody:
        left_out.append(f"{nobody} messages by an account that is neither a person of the scenario nor the agent")
    found.sort(key=lambda m: (m.at, m.id))
    used = {m.conversation for m in found}
    places = [_conversation(c) for c in slack_present.conversations(world, keys) if c.id in used]
    return found, [p for p in places if p is not None]


def _conversation(c: PresentedConversation) -> RecordConversation | None:
    if c.kind is ConversationKind.DIRECT:
        return RecordDirect.model_validate({"id": c.id, "with": c.people[0]}) if c.people else None
    if c.kind is ConversationKind.GROUP:
        return RecordGroup.model_validate({"id": c.id, "with": c.people}) if len(c.people) >= 2 else None
    return RecordChannel(id=c.id, name=c.name if c.name is not None else c.id)


def _seeded(event: WorldEvent) -> bool:
    return event.wake == 0 and event.actor is Actor.SCENARIO


def _references(world: Store) -> dict[str, str]:
    return {r.item: r.reference for r in jira_present.references(world)}


def _items(
    events: list[WorldEvent], titles: dict[str, str], world: Store, by_email: dict[str, str]
) -> list[RecordItem]:
    """What was already written down when the run began: the scenario's tickets, documents and sheets, as seeded."""
    refs = _references(world)
    seeded: dict[str, WorldEvent] = {}
    for event in events:
        if _seeded(event) and isinstance(event.after, (TicketSnapshot, DocumentSnapshot)):
            seeded[f"{event.entity.provider}/{event.entity.external_id}"] = event
    folders = {
        e.after.title: e.after.parent
        for e in seeded.values()
        if isinstance(e.after, DocumentSnapshot)
        and e.after.mime_type is not None
        and e.after.mime_type.endswith(FOLDER)
    }
    found: list[RecordItem] = []
    for event in seeded.values():
        tool = titles[event.entity.provider] if event.entity.provider in titles else event.entity.provider
        snapshot = event.after
        if isinstance(snapshot, TicketSnapshot):
            email = snapshot.assignee_email.lower() if snapshot.assignee_email is not None else None
            found.append(
                RecordItem(
                    id=event.entity.external_id,
                    kind="Ticket",
                    ref=refs[event.entity.external_id] if event.entity.external_id in refs else None,
                    tool=tool,
                    place=snapshot.project or tool,
                    title=snapshot.title,
                    text=snapshot.body or None,
                    state="open" if snapshot.state is TicketState.OPEN else "done",
                    assignee=by_email[email] if email is not None and email in by_email else None,
                )
            )
        elif isinstance(snapshot, DocumentSnapshot) and snapshot.title not in folders:
            sheet = snapshot.mime_type is not None and SPREADSHEET in snapshot.mime_type
            found.append(
                RecordItem(
                    id=event.entity.external_id,
                    kind="Spreadsheet" if sheet else "Document",
                    tool=tool,
                    place=_path(snapshot.parent, folders) or snapshot.space or tool,
                    title=snapshot.title,
                    text=None if sheet else snapshot.text,
                    rows=[line.split("\t") for line in snapshot.text.splitlines()] if sheet and snapshot.text else None,
                )
            )
    return found


def _path(parent: str | None, folders: dict[str, str | None]) -> str | None:
    """The folders a document sits in, outermost first."""
    path: list[str] = []
    while parent is not None and parent not in path:
        path.insert(0, parent)
        parent = folders[parent] if parent in folders else None
    return " / ".join(path) or None


def _approvals(events: list[WorldEvent], left_out: list[str]) -> list[RecordApproval]:
    """Each item the agent put in a person's inbox: when it was first seen there, and where it stands at the end."""
    first: dict[str, WorldEvent] = {}
    for event in events:
        if isinstance(event.after, InboxItemSnapshot) and event.after.item_id not in first:
            first[event.after.item_id] = event
    found: list[RecordApproval] = []
    nobody = 0
    for item in items_in(events).values():
        if item.person is None:
            nobody += 1
            continue
        found.append(
            RecordApproval.model_validate(
                {
                    "id": item.item_id,
                    "at": first[item.item_id].sim_time,
                    "for": item.person,
                    "status": _status(item),
                    "summary": item.summary,
                    "decision": item.decision if item.status is ItemStatus.DECIDED else None,
                }
            )
        )
    if nobody:
        left_out.append(f"{nobody} inbox items that wait on nobody in the scenario")
    return sorted(found, key=lambda a: (a.at, a.id))


def _status(item: InboxItemSnapshot) -> ApprovalStatus:
    if item.status is ItemStatus.PENDING:
        return ApprovalStatus.PENDING
    if item.status is ItemStatus.WITHDRAWN:
        return ApprovalStatus.WITHDRAWN
    decision = (item.decision or "").lower()
    if decision.startswith("approve"):
        return ApprovalStatus.APPROVED
    if decision.startswith("reject"):
        return ApprovalStatus.REJECTED
    return ApprovalStatus.DECIDED


_DID = {
    (EntityKind.TICKET, Operation.CREATE): "Created a ticket",
    (EntityKind.TICKET, Operation.UPDATE): "Changed a ticket",
    (EntityKind.TICKET, Operation.DELETE): "Deleted a ticket",
    (EntityKind.DOCUMENT, Operation.CREATE): "Created a document",
    (EntityKind.DOCUMENT, Operation.UPDATE): "Changed a document",
    (EntityKind.DOCUMENT, Operation.DELETE): "Deleted a document",
    (EntityKind.COMMENT, Operation.CREATE): "Commented",
    (EntityKind.COMMENT, Operation.UPDATE): "Changed a comment",
    (EntityKind.COMMENT, Operation.DELETE): "Deleted a comment",
}


def _actions(events: list[WorldEvent], titles: dict[str, str], world: Store, left_out: list[str]) -> list[RecordAction]:
    """What the agent changed itself in the team's tools outside the chat: a ticket, a document, a comment."""
    refs = _references(world)
    named: dict[str, str] = {}
    for event in events:
        if isinstance(event.after, (TicketSnapshot, DocumentSnapshot)):
            named[event.entity.external_id] = event.after.title
    found: list[RecordAction] = []
    untitled = 0
    for event in events:
        if event.actor is not Actor.AGENT or (event.entity.kind, event.operation) not in _DID:
            continue
        tool = titles[event.entity.provider] if event.entity.provider in titles else event.entity.provider
        after = event.after
        if isinstance(after, TicketSnapshot):
            title, place = after.title, after.project
        elif isinstance(after, DocumentSnapshot):
            title, place = after.title, after.parent
        elif isinstance(after, MessageSnapshot) and after.channel in named:
            title, place = named[after.channel], None
        elif event.entity.external_id in named:
            title, place = named[event.entity.external_id], None
        else:
            untitled += 1
            continue
        found.append(
            RecordAction(
                id=str(event.seq),
                at=event.sim_time,
                wake=max(event.wake, 1),
                did=_DID[(event.entity.kind, event.operation)],
                title=title,
                tool=tool,
                place=place,
                ref=refs[event.entity.external_id] if event.entity.external_id in refs else None,
            )
        )
    if untitled:
        left_out.append(f"{untitled} changes by the agent to things the run names no title for")
    return found
