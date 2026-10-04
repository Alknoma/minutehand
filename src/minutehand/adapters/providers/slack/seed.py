"""The workspace a scenario starts in: its people, the agent's bot user, an IM with each person, and `#general`."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.state import BOT_ID, BOT_USER_ID, SlackWorld
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.world import Actor, Operation
from minutehand.ports.store import Store


def _member(person: Person, at: datetime) -> wire.SlackUser:
    tz = person.working_hours.timezone if person.working_hours is not None else "UTC"
    offset = ZoneInfo(tz).utcoffset(at)
    return wire.SlackUser(
        id=state.user_id(person.key),
        team_id=state.TEAM_ID,
        name=person.key,
        real_name=person.name,
        tz=tz,
        tz_offset=int(offset.total_seconds()) if offset is not None else 0,
        profile=wire.SlackProfile(
            real_name=person.name,
            display_name=person.name,
            email=person.email,
            title=person.title or "",
        ),
    )


def _bot() -> wire.SlackUser:
    return wire.SlackUser(
        id=BOT_USER_ID,
        team_id=state.TEAM_ID,
        name=state.BOT_NAME,
        real_name=state.BOT_NAME,
        is_bot=True,
        profile=wire.SlackProfile(real_name=state.BOT_NAME, display_name=state.BOT_NAME, bot_id=BOT_ID),
    )


def seed(scenario: Scenario, world: Store) -> None:
    slack = SlackWorld(world)
    created = int(scenario.starts_at.timestamp())
    users = [_bot(), *(_member(p, scenario.starts_at) for p in scenario.people)]
    for user in users:
        slack.write(
            state.user_ref(user.id), user, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=state.TEAM_ID
        )

    general = wire.SlackChannel(
        id=state.named_channel_id(state.GENERAL),
        name=state.GENERAL,
        is_channel=True,
        is_general=True,
        created=created,
        creator=BOT_USER_ID,
    )
    slack.write(
        state.channel_ref(general.id), general, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=state.TEAM_ID
    )
    for user in users:
        slack.write(
            state.membership_ref(general.id, user.id),
            wire.SlackMembership(channel=general.id, user=user.id),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=general.id,
        )

    for user in users[1:]:
        slack.open_conversation([BOT_USER_ID, user.id], created=created, actor=Actor.SCENARIO)
