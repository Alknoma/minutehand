"""The workspaces a scenario starts in: in each, its people, the agent's bot user, `#general`, an IM with each person
who can be messaged, who installed the app; the channels and history the scenario seeds; each person's absences, so
their status shows them; and the faults Slack's own seed (`SlackSeed`, the scenario's `ProviderSeed` for `slack`)
declares.

A world is one workspace unless `SlackSeed.workspaces` names several (one agent installed in each). A seeded channel
is in the first workspace every one of its members and authors belongs to."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Annotated, ClassVar, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.domain.provider import Keyed
from minutehand.domain.scenario import (
    AbsenceTrigger,
    Account,
    Model,
    Person,
    Scenario,
    SeededChannel,
    SeededFile,
    SeededPost,
)
from minutehand.domain.world import Actor, DocumentSnapshot, EntityKind, MessageSnapshot, Operation
from minutehand.ports.store import Store


class RateLimited(Model):
    kind: Literal["rate_limited"] = "rate_limited"
    retry_after: timedelta = Field(default=timedelta(seconds=1), gt=timedelta(0))


class Refused(Model):
    kind: Literal["refused"] = "refused"
    error: str = Field(min_length=1, description="Slack's own error code, as it sends it")


class FaultSeed(Model):
    """A Web API call Slack fails on purpose, the way Slack fails it."""

    call: str | None = Field(default=None, description="Slack's own method name; None is every call")
    answer: Annotated[RateLimited | Refused, Field(discriminator="kind")]
    times: int | None = Field(default=1, ge=1, description="How many calls it fails; None is every one")
    after: timedelta = Field(default=timedelta(0), ge=timedelta(0), description="From this offset on")
    only_rich: bool = Field(
        default=False, description="Fail only calls that carry blocks or attachments; the plain retry passes"
    )


class WorkspaceSeed(Model, Keyed):
    """One workspace the agent's app is installed in: who it is there, the bot tokens that are its, and who is in it."""

    IDENTITY: ClassVar[tuple[str, ...]] = ("team_id",)

    team_id: str = Field(default=state.TEAM_ID, pattern=r"^T[A-Z0-9]+$")
    name: str = state.TEAM_NAME
    domain: str = Field(default=state.TEAM_DOMAIN, pattern=r"^[a-z0-9][a-z0-9-]*$")
    bot_user_id: str = Field(default=state.BOT_USER_ID, pattern=r"^[UW][A-Z0-9]+$")
    bot_id: str = Field(default=state.BOT_ID, pattern=r"^B[A-Z0-9]+$")
    app_id: str = Field(default=state.APP_ID, pattern=r"^A[A-Z0-9]+$")
    bot_name: str = state.BOT_NAME
    tokens: list[str] = Field(
        default=[],
        description="Tokens that select this workspace; any other token is answered in the first workspace that "
        "declares none, else the first",
    )
    members: list[str] | None = Field(default=None, description="Person.key of each member; None: every person")
    oauth_code: str | None = Field(default=None, description="The install code `oauth.v2.access` answers it for")


class Joined(Model):
    """A person who is a member of a declared workspace beyond its `members`: how an open world grows one."""

    team_id: str = Field(pattern=r"^T[A-Z0-9]+$", description="WorkspaceSeed.team_id")
    person: str = Field(description="Person.key")


class SlackSeed(Model):
    """What only Slack seeds, as the body of the scenario's `ProviderSeed` for `slack`."""

    workspaces: list[WorkspaceSeed] = Field(default=[], description="None given: the one default workspace")
    joined: list[Joined] = Field(default=[], description="More members of declared workspaces")
    without_email: list[str] = Field(
        default=[],
        description="Person.key of each member whose profile carries no email, as Slack serves one when the app "
        "lacks `users:read.email` or the member has none; `users.lookupByEmail` does not find them",
    )
    single_channel_guests: list[str] = Field(
        default=[], description="Person.key of each guest (account `guest`) who is a single-channel guest"
    )
    faults: list[FaultSeed] = []

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        for field in ("team_id", "bot_user_id", "domain"):
            values = [getattr(w, field) for w in self.workspaces]
            if len(values) != len(set(values)):
                raise ValueError(f"two workspaces share a {field}")
        tokens = [t for w in self.workspaces for t in w.tokens]
        if len(tokens) != len(set(tokens)):
            raise ValueError("two workspaces accept the same token")
        return self


def slack_seed(scenario: Scenario) -> SlackSeed:
    found = scenario.provider_seed(MANIFEST.key)
    return SlackSeed() if found is None else SlackSeed.model_validate_json(found.body)


def workspaces_of(scenario: Scenario) -> list[tuple[wire.SlackWorkspace, list[Person]]]:
    """Each workspace the scenario's Slack holds, with its people, in order."""
    spec = slack_seed(scenario)
    given = spec.workspaces or [WorkspaceSeed()]
    keys = {p.key for p in scenario.people}
    teams = {w.team_id for w in spec.workspaces}
    for join in spec.joined:
        if join.team_id not in teams:
            raise ValueError(f"{join.person} joins workspace {join.team_id}, which the Slack seed does not declare")
    for named in (spec.without_email, spec.single_channel_guests, [j.person for j in spec.joined]):
        unknown = sorted(set(named) - keys)
        if unknown:
            raise ValueError(f"the Slack seed names no such person: {', '.join(unknown)}")
    guests = {p.key for p in scenario.people if p.account is Account.GUEST}
    not_guests = sorted(set(spec.single_channel_guests) - guests)
    if not_guests:
        raise ValueError(f"a single-channel guest is a guest, and {', '.join(not_guests)} is not one")
    found: list[tuple[wire.SlackWorkspace, list[Person]]] = []
    for position, w in enumerate(given):
        unknown = sorted(set(w.members or []) - keys)
        if unknown:
            raise ValueError(f"workspace {w.team_id} names no such person: {', '.join(unknown)}")
        members = (
            None if w.members is None else {*w.members, *(j.person for j in spec.joined if j.team_id == w.team_id)}
        )
        people = [p for p in scenario.people if members is None or p.key in members]
        workspace = wire.SlackWorkspace(
            id=w.team_id,
            name=w.name,
            domain=w.domain,
            bot_user_id=w.bot_user_id,
            bot_id=w.bot_id,
            app_id=w.app_id,
            bot_name=w.bot_name,
            tokens=w.tokens,
            oauth_code=w.oauth_code,
            position=position,
        )
        found.append((workspace, people))
    return found


REACHABLE = (Account.MEMBER, Account.GUEST)
"""Who the agent can open a DM with: a bot cannot be DMed and a deactivated account cannot be reached."""


def _member(person: Person, at: datetime, team: str, spec: SlackSeed) -> wire.SlackUser:
    tz = person.working_hours.timezone if person.working_hours is not None else "UTC"
    offset = ZoneInfo(tz).utcoffset(at)
    bot = person.account is Account.BOT
    return wire.SlackUser(
        id=state.user_id(person.key, team),
        team_id=team,
        name=person.key,
        real_name=person.name,
        deleted=person.account is Account.DEACTIVATED,
        is_restricted=person.account is Account.GUEST,
        is_ultra_restricted=person.key in spec.single_channel_guests,
        is_bot=bot,
        tz=tz,
        tz_offset=int(offset.total_seconds()) if offset is not None else 0,
        profile=wire.SlackProfile(
            real_name=person.name,
            display_name=person.name,
            email=None if bot or person.key in spec.without_email else person.email,
            title=person.title or "",
            bot_id=state.other_bot_id(person.key) if bot else None,
        ),
    )


def _bot(workspace: wire.SlackWorkspace) -> wire.SlackUser:
    return wire.SlackUser(
        id=workspace.bot_user_id,
        team_id=workspace.id,
        name=workspace.bot_name,
        real_name=workspace.bot_name,
        is_bot=True,
        profile=wire.SlackProfile(
            real_name=workspace.bot_name, display_name=workspace.bot_name, bot_id=workspace.bot_id
        ),
    )


def seed(scenario: Scenario, world: Store) -> None:
    created = int(scenario.starts_at.timestamp())
    every = workspaces_of(scenario)
    for workspace, people in every:
        kept = len(every) > 1 or workspace != state.DEFAULT_WORKSPACE
        _workspace(SlackWorld(world, workspace), workspace, people, scenario, created, kept=kept)
    for channel in (c for c in scenario.channels if c.provider == MANIFEST.key):
        named = {*channel.members, *(p.by for p in _every(channel.history))}
        home = next((w for w, people in every if named <= {p.key for p in people}), every[0][0])
        _channel(SlackWorld(world, home), channel, scenario, created)
    write_faults(SlackWorld(world, every[0][0]), slack_seed(scenario).faults, scenario.starts_at)


def _every(posts: list[SeededPost]) -> list[SeededPost]:
    return [found for post in posts for found in (post, *_every(post.replies))]


def _workspace(
    slack: SlackWorld,
    workspace: wire.SlackWorkspace,
    people: list[Person],
    scenario: Scenario,
    created: int,
    *,
    kept: bool,
) -> None:
    """One workspace; `kept` records it, which a world of only the default workspace leaves out."""
    team, bot = workspace.id, workspace.bot_user_id
    if kept:
        slack.write(
            state.workspace_ref(team),
            workspace,
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=state.WORKSPACES,
        )
    spec = slack_seed(scenario)
    users = [_bot(workspace), *(_member(p, scenario.starts_at, team, spec) for p in people)]
    for user in users:
        slack.write(state.user_ref(user.id), user, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=team)
    for person in (p for p in people if p.key in spec.without_email and p.account is not Account.BOT):
        user = state.user_id(person.key, team)
        slack.write(
            state.unlisted_email_ref(user),
            wire.SlackUnlistedEmail(user=user, email=person.email),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=state.EMAILS,
        )
    installer = next((p for p in people if p.key == scenario.owner), people[0] if people else None)
    slack.write(
        state.install_ref(team),
        wire.SlackInstall(installer=state.user_id(installer.key, team) if installer is not None else bot),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=state.APP,
    )

    general = wire.SlackChannel(
        id=state.named_channel_id(state.GENERAL, team),
        name=state.GENERAL,
        is_channel=True,
        is_general=True,
        created=created,
        creator=bot,
    )
    slack.write(state.channel_ref(general.id), general, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=team)
    for member in [bot, *(state.user_id(p.key, team) for p in people if p.account is Account.MEMBER)]:
        _join(slack, general.id, member)
    for person in (p for p in people if p.account in REACHABLE):
        slack.open_conversation([bot, state.user_id(person.key, team)], created=created, actor=Actor.SCENARIO)
    for person in (p for p in people if p.absences):
        write_away(slack, person, scenario.starts_at, team)


def write_away(slack: SlackWorld, person: Person, start: datetime, team: str) -> None:
    """The person's absences beside their account, so their status, presence and do-not-disturb show each one."""
    user = state.user_id(person.key, team)
    slack.write(
        state.away_ref(user),
        wire.SlackAway(
            user=user,
            email=person.email,
            starts_at=int(start.timestamp()),
            stretches=[
                wire.SlackAwayStretch(
                    on_first_ask=a.trigger is AbsenceTrigger.ON_FIRST_ASK,
                    starts_after=int(a.starts_after.total_seconds()),
                    lasts=int(a.lasts.total_seconds()),
                    reason=a.reason,
                )
                for a in person.absences
            ],
        ),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=state.AWAY,
    )


DECLARED = 1_000_000
"""Where the numbers of faults declared on an open world (`provider-faults`) start: above every number a seed gives
its own faults, which count from 0 in the seed's order, so a fault a later seed fragment adds never takes the
number of one declared before it, and the seed's are armed ahead of the declared ones."""


def write_faults(slack: SlackWorld, faults: list[FaultSeed], start: datetime, *, declared: bool = False) -> None:
    """Record each fault after those already recorded, from `start` plus its own offset."""
    first = (DECLARED if declared else 0) + len(slack.bodies(EntityKind.RECORD, state.FAULTS, wire.SlackFault))
    for position, fault in enumerate(faults, start=first):
        answer = fault.answer
        limited = answer if isinstance(answer, RateLimited) else None
        slack.write(
            state.fault_ref(position),
            wire.SlackFault(
                position=position,
                call=fault.call,
                error="ratelimited" if isinstance(answer, RateLimited) else answer.error,
                retry_after=max(1, int(limited.retry_after.total_seconds())) if limited is not None else None,
                remaining=fault.times,
                from_time=int((start + fault.after).timestamp()),
                only_rich=fault.only_rich,
            ),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=state.FAULTS,
        )


def _join(slack: SlackWorld, channel: str, user: str) -> None:
    slack.write(
        state.membership_ref(channel, user),
        wire.SlackMembership(channel=channel, user=user),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=channel,
    )


def _channel(slack: SlackWorld, seeded: SeededChannel, scenario: Scenario, created: int) -> None:
    team, bot = slack.team.id, slack.bot
    members = [state.user_id(k, team) for k in seeded.members]
    if seeded.name is None:
        cid = state.conversation_id([bot, *members])
        if slack.channel(cid) is None:
            slack.open_conversation([bot, *members], created=created, actor=Actor.SCENARIO)
    else:
        creator = members[0] if members else bot
        channel = wire.SlackChannel(
            id=state.named_channel_id(seeded.name, team),
            name=seeded.name,
            is_channel=not seeded.private,
            is_group=seeded.private,
            is_private=seeded.private,
            is_archived=seeded.archived,
            created=created,
            creator=creator,
            topic=wire.SlackTopic(value=seeded.topic, creator=creator, last_set=created) if seeded.topic else None,
            purpose=wire.SlackTopic(value=seeded.purpose, creator=creator, last_set=created)
            if seeded.purpose
            else None,
        )
        cid = channel.id
        slack.write(state.channel_ref(cid), channel, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=team)
        for member in sorted(set(members + ([bot] if seeded.agent_member else []))):
            _join(slack, cid, member)
    seconds: dict[int, int] = {}
    for post in sorted(seeded.history, key=lambda p: p.ago, reverse=True):
        root = _post(slack, cid, post, scenario, None, seconds)
        for reply in sorted(post.replies, key=lambda p: p.ago, reverse=True):
            _post(slack, cid, reply, scenario, root, seconds)


def _post(
    slack: SlackWorld,
    channel: str,
    post: SeededPost,
    scenario: Scenario,
    thread_ts: str | None,
    seconds: dict[int, int],
) -> str:
    """A seeded message, as its author wrote it before the run began, with its files. Its `ts` is its second and
    its order among the channel's seeded messages in that second (`SlackWorld.seeded_ts`)."""
    at = int((scenario.starts_at - post.ago).timestamp())
    seconds[at] = (seconds[at] if at in seconds else 0) + 1
    stamp = slack.seeded_ts(channel, at, seconds[at])
    author = state.user_id(post.by, slack.team.id)
    files = [
        write_file(slack, f, author, at, seed=f"{channel}|{post.ago}|{i}", actor=Actor.SCENARIO)
        for i, f in enumerate(post.files)
    ]
    ts = write_post(slack, channel, author, post.text, thread_ts, files, at=at, actor=Actor.SCENARIO, ts=stamp)
    if post.key is not None:
        remember_post(slack, post.key, channel, ts, actor=Actor.SCENARIO)
    return ts


def write_post(
    slack: SlackWorld,
    channel: str,
    author: str,
    text: str,
    thread_ts: str | None,
    files: list[wire.SlackFile],
    *,
    at: int,
    actor: Actor,
    ts: str | None = None,
) -> str:
    """A person's message in the store, as Slack keeps it, recorded as reaching the channel's other humans; at the
    `ts` given (a seeded message's), or the next free one at `at`."""
    ts = ts if ts is not None else slack.ts_at(at)
    slack.write(
        state.message_ref(ts),
        person_message(ts, author, text, thread_ts, files, slack.team.id),
        operation=Operation.CREATE,
        actor=actor,
        parent=channel,
        after=MessageSnapshot(
            text=text,
            channel=channel,
            recipient_emails=slack.human_emails(channel, besides=author),
            thread_of=thread_ts,
        ),
    )
    return ts


def person_message(
    ts: str, author: str, text: str, thread_ts: str | None, files: list[wire.SlackFile], team: str = state.TEAM_ID
) -> wire.SlackMessage:
    return wire.SlackMessage(
        ts=ts,
        user=author,
        text=text,
        team=team,
        thread_ts=thread_ts,
        client_msg_id=state.client_msg_id(ts),
        subtype="file_share" if files else None,
        files=files or None,
        upload=False if files else None,
    )


def remember_post(slack: SlackWorld, key: str, channel: str, ts: str, *, actor: Actor) -> None:
    slack.write(
        state.post_ref(key),
        wire.SlackPostKey(key=key, channel=channel, ts=ts),
        operation=Operation.CREATE,
        actor=actor,
        parent=state.POSTS,
    )


def write_file(
    slack: SlackWorld, seeded: SeededFile, author: str, at: int, *, seed: str, actor: Actor
) -> wire.SlackFile:
    """A file and its content in the store, as `author` uploaded it at `at`; answers the file as Slack serves it."""
    file = state.file_id(seed)
    extension = seeded.name.rsplit(".", 1)[-1].lower() if "." in seeded.name else "text"
    served = wire.SlackFile(
        id=file,
        created=at,
        timestamp=at,
        name=seeded.name,
        title=seeded.title or seeded.name,
        mimetype=seeded.mime_type,
        filetype=extension,
        pretty_type=extension.upper(),
        user=author,
        user_team=slack.team.id,
        size=len(seeded.text.encode()),
        is_public=False,
        url_private=state.url_private(file, seeded.name, slack.team.id),
        url_private_download=state.url_private_download(file, seeded.name, slack.team.id),
        permalink=f"https://{slack.team.domain}.slack.com/files/{author}/{file}/{seeded.name}",
    )
    slack.write(
        state.file_ref(file),
        served,
        operation=Operation.CREATE,
        actor=actor,
        parent=slack.team.id,
        after=DocumentSnapshot(title=served.title, mime_type=served.mimetype),
    )
    slack.write(
        state.content_ref(file),
        wire.SlackFileContent(
            file=file, mimetype=seeded.mime_type, encoded=base64.b64encode(seeded.text.encode()).decode()
        ),
        operation=Operation.CREATE,
        actor=actor,
        parent=state.FILES,
    )
    return served
