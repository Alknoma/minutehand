"""What Slack alone can say of a run, for its export (`minutehand export`, docs/exporting.md): what each conversation
is called and who is in it, who wrote each message and when Slack dates it, and the agent's own account.

Read from the workspace as the run's store holds it at its head, through `SlackWorld`; nothing here writes.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.domain.presentation import (
    ConversationKind,
    PresentedAgent,
    PresentedAuthor,
    PresentedConversation,
)
from minutehand.domain.world import EntityKind, Stored
from minutehand.ports.store import Store

_SCAN = 1000


def _workspaces(store: Store) -> list[SlackWorld]:
    first = SlackWorld(store)
    return [first.as_team(w) for w in first.workspaces()]


def _people(slack: SlackWorld, people: Sequence[str]) -> dict[str, str]:
    """Each person's member id in this workspace, to their Person.key."""
    return {state.user_id(key, slack.team.id): key for key in people}


def _messages(store: Store, channel: str) -> Iterator[Stored]:
    after: str | None = None
    while True:
        page = store.children(MANIFEST.key, EntityKind.MESSAGE, channel, after=after, limit=_SCAN)
        yield from page
        if len(page) < _SCAN:
            return
        after = page[-1].entity.external_id


def conversations(store: Store, people: Sequence[str]) -> list[PresentedConversation]:
    """Every channel, DM and group DM in every workspace, as it stands at the run's head."""
    found: list[PresentedConversation] = []
    for slack in _workspaces(store):
        members = _people(slack, people)
        for channel in slack.channels_after(None):
            if channel.is_im:
                other = members[channel.user] if channel.user is not None and channel.user in members else None
                found.append(
                    PresentedConversation(id=channel.id, kind=ConversationKind.DIRECT, people=[other] if other else [])
                )
            elif channel.is_mpim:
                inside = [members[m] for m in slack.every_member(channel.id) if m in members]
                found.append(PresentedConversation(id=channel.id, kind=ConversationKind.GROUP, people=inside))
            else:
                found.append(PresentedConversation(id=channel.id, kind=ConversationKind.CHANNEL, name=channel.name))
    return found


def authors(store: Store, people: Sequence[str]) -> list[PresentedAuthor]:
    """Who wrote each message still in the workspace, and when Slack dates it: its `ts`, whose whole seconds are
    when it was posted, before the run began for one the scenario seeded."""
    found: list[PresentedAuthor] = []
    for slack in _workspaces(store):
        members = _people(slack, people)
        for channel in slack.channels_after(None):
            for stored in _messages(store, channel.id):
                message = wire.parse(wire.SlackMessage, stored.body)
                found.append(
                    PresentedAuthor(
                        message=stored.entity.external_id,
                        person=members[message.user] if message.user in members else None,
                        agent=message.user == slack.team.bot_user_id,
                        written_at=datetime.fromtimestamp(int(message.ts.split(".")[0]), UTC),
                        listed=message.ephemeral_to is None,
                    )
                )
    return found


def agent(store: Store) -> PresentedAgent:
    """The agent's bot in the first workspace: its member id, and the name people see it by."""
    slack = SlackWorld(store)
    bot = slack.user(slack.team.bot_user_id)
    shown = (bot.profile.display_name or bot.real_name) if bot is not None else ""
    return PresentedAgent(account=slack.team.bot_user_id, name=shown or slack.team.bot_name or None)


def accounts(store: Store, people: Sequence[str]) -> list[tuple[str, str]]:
    """Each person's member id in the first workspace, which a mention of them (`<@U…>`) names."""
    team = SlackWorld(store).team.id
    return [(key, state.user_id(key, team)) for key in people]
