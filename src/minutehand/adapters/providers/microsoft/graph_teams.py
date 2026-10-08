"""Graph for people and Teams: users, `/me`, teams, channels, chats, their messages and members.

Messages are the connector's activities, read in Graph's `chatMessage` shape: the bot's are from an application,
a person's from a user; an Adaptive Card is an attachment whose `content` is the card's JSON as a string. A
channel's id is the same in both; a 1:1 chat has a connector id (`a:…`) and a Graph id (`19:…@unq.gbl.spaces`).

Query options: `$select` everywhere; `$top` and `@odata.nextLink` on every list; `$expand=replies` on a channel's
messages; `$filter` on users (`mail`, `userPrincipalName`, `displayName`, `id` with `eq`, `startswith` on
`displayName`) and on channels (`displayName eq`). Any other `$filter`, `$orderby` or `$search` is refused 400, as
Graph refuses a clause it does not support.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.providers.microsoft import wire
from minutehand.adapters.providers.microsoft.common import (
    GRAPH_JSON,
    GraphRefusal,
    bad_request,
    graph_caller,
    not_found,
    query,
)
from minutehand.adapters.providers.microsoft.state import (
    GRAPH,
    AwayRecord,
    ConversationRecord,
    MicrosoftWorld,
    TeamRecord,
    UserRecord,
    conversation_ref,
    team_ref,
    user_ref,
)
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Operation
from minutehand.ports.clock import Clock

PAGE_DEFAULT = 20
PAGE_MAX = 50
USERS_PAGE_MAX = 999


def chat_message(world: MicrosoftWorld, conversation: ConversationRecord, activity: wire.Activity) -> wire.ChatMessage:
    user = world.user_by_mri(activity.sender.id)
    if user is not None:
        sender = wire.IdentitySet(
            user=wire.Identity(
                id=user.user.id, displayName=user.user.displayName, userIdentityType="aadUser", tenantId=user.tenant_id
            )
        )
    else:
        app_id = activity.sender.id.removeprefix("28:")
        sender = wire.IdentitySet(
            application=wire.Identity(id=app_id, displayName=activity.sender.name, applicationIdentityType="bot")
        )
    attachments = [
        wire.ChatMessageAttachment(
            id=f"{activity.id}-{n}",
            contentType=a.contentType,
            content=json.dumps(a.content) if a.content is not None else None,
            contentUrl=a.contentUrl,
            name=a.name,
        )
        for n, a in enumerate(activity.attachments or [])
    ]
    mentions = [
        wire.ChatMessageMention(
            id=n,
            mentionText=m.text.removeprefix("<at>").removesuffix("</at>"),
            mentioned=wire.MentionedIdentity(
                application=wire.Identity(id=m.mentioned.id.removeprefix("28:"), displayName=m.mentioned.name)
                if world.user_by_mri(m.mentioned.id) is None
                else None,
                user=wire.Identity(id=mentioned.user.id, displayName=m.mentioned.name)
                if (mentioned := world.user_by_mri(m.mentioned.id)) is not None
                else None,
            ),
        )
        for n, m in enumerate(wire.mentions(activity))
    ]
    team = world.team(conversation.team_id) if conversation.team_id is not None else None
    channel = conversation.type is wire.ConversationType.CHANNEL
    text = activity.text or ""
    for n, mention in enumerate(wire.mentions(activity)):
        text = text.replace(
            mention.text, f'<at id="{n}">{mention.text.removeprefix("<at>").removesuffix("</at>")}</at>'
        )
    return wire.ChatMessage(
        id=activity.id,
        replyToId=activity.replyToId if channel else None,
        etag=activity.id,
        createdDateTime=activity.timestamp,
        lastModifiedDateTime=activity.localTimestamp or activity.timestamp,
        lastEditedDateTime=activity.localTimestamp,
        chatId=None if channel else conversation.graph_id,
        channelIdentity=wire.ChannelIdentity(teamId=team.id, channelId=conversation.graph_id)
        if channel and team
        else None,
        sender=sender,
        body=wire.ItemBody(content=text),
        attachments=attachments,
        mentions=mentions,
        reactions=[],
        webUrl=f"https://teams.microsoft.com/l/message/{conversation.graph_id}/{activity.id}",
    )


def _quoted(text: str) -> str:
    return text.replace("''", "'")


class TeamsGraph:
    def __init__(self, world: MicrosoftWorld, clock: Clock) -> None:
        self._world = world
        self._clock = clock

    # ------------------------------------------------------------------ paging

    @staticmethod
    def _bounds(request: Request, default: int, most: int) -> tuple[int, int]:
        top = query(request, "$top")
        if top is not None and (not top.isdigit() or int(top) < 1):
            raise NotServed(f"$top={top}: the page names 1 and up, and no answer to anything else")
        size = min(int(top), most) if top else default
        if top and int(top) > most:
            raise NotServed(f"$top={top}: past the documented most of {most}, whose answer is not documented")
        skip = query(request, "$skiptoken")
        offset = 0
        if skip:
            try:
                offset = int(base64.urlsafe_b64decode(skip + "=" * (-len(skip) % 4)).decode())
            except (binascii.Error, ValueError) as e:
                raise bad_request("The $skiptoken is not valid.") from e
        return size, offset

    def _page(
        self,
        request: Request,
        items: list[wire.M],
        context: str,
        *,
        default: int = PAGE_DEFAULT,
        most: int = PAGE_MAX,
        keep: frozenset[str] = frozenset(),
    ) -> Response:
        size, offset = self._bounds(request, default, most)
        page = items[offset : offset + size]
        following: str | None = None
        if offset + size < len(items):
            kept = [
                f"{k}={request.query_params[k]}" for k in ("$select", "$expand", "$filter") if k in request.query_params
            ]
            skip = base64.urlsafe_b64encode(str(offset + size).encode()).decode().rstrip("=")
            following = f"{GRAPH}{request.url.path.removeprefix('/v1.0')}?{'&'.join([*kept, f'$top={size}', f'$skiptoken={skip}'])}"
        body = "{" + f'"@odata.context":{json.dumps(context)},"value":[' + ",".join(wire.dump(i) for i in page) + "]"
        body += f',"@odata.nextLink":{json.dumps(following)}' + "}" if following else "}"
        return Response(wire.select_page(body, self._fields(request), keep=keep), media_type=GRAPH_JSON)

    @staticmethod
    def _fields(request: Request) -> list[str] | None:
        return [f for f in (query(request, "$select") or "").split(",") if f] or None

    def _one(self, request: Request, entity: wire.Aliased, context: str) -> Response:
        return Response(
            wire.select(wire.with_context(wire.dump(entity), context), self._fields(request)), media_type=GRAPH_JSON
        )

    @staticmethod
    def _refuse_options(request: Request, allowed: set[str]) -> None:
        for option in ("$filter", "$orderby", "$search", "$expand", "$count"):
            if option in request.query_params and option not in allowed:
                raise NotServed(f"the query option {option} here")

    # ------------------------------------------------------------------ users

    def _user(self, key: str) -> UserRecord:
        found = self._world.user_by(key)
        if found is None:
            raise GraphRefusal(
                404,
                "Request_ResourceNotFound",
                f"Resource '{key}' does not exist or one of its queried reference-property objects are not present.",
            )
        return found

    def _filter_users(self, clause: str) -> list[UserRecord]:
        users = self._world.users()
        equal = re.fullmatch(r"\s*(mail|userPrincipalName|displayName|id)\s+eq\s+'((?:[^']|'')*)'\s*", clause)
        if equal is not None:
            field, value = equal.group(1), _quoted(equal.group(2)).lower()
            return [u for u in users if (getattr(u.user, field) or "").lower() == value]
        starts = re.fullmatch(
            r"\s*startswith\(\s*(displayName|mail|userPrincipalName)\s*,\s*'((?:[^']|'')*)'\s*\)\s*", clause
        )
        if starts is not None:
            field, value = starts.group(1), _quoted(starts.group(2)).lower()
            return [u for u in users if (getattr(u.user, field) or "").lower().startswith(value)]
        raise NotServed(f"the $filter clause {clause!r}")

    async def users(self, request: Request, parts: list[str]) -> Response:
        claims = graph_caller(request, self._world)
        if parts[0] == "me":
            if claims.oid is None:
                raise NotServed("/me with no signed-in user: Graph documents no answer to an application")
            parts = ["users", claims.oid, *parts[1:]]
        if request.method != "GET":
            raise NotServed(f"{request.method} on a user")
        if len(parts) == 1:
            self._refuse_options(request, {"$filter", "$count"})
            clause = query(request, "$filter")
            found = self._filter_users(clause) if clause else self._world.users()
            self._world.saw(user_ref("all"), Operation.SEARCH)
            return self._page(
                request, [u.user for u in found], f"{GRAPH}/$metadata#users", default=100, most=USERS_PAGE_MAX
            )
        user = self._user(parts[1])
        if len(parts) == 2:
            self._refuse_options(request, set())
            self._world.saw(user_ref(user.user.id), Operation.READ)
            return self._one(request, user.user, f"{GRAPH}/$metadata#users/$entity")
        if parts[2:] == ["presence"]:
            self._world.saw(user_ref(user.user.id), Operation.READ)
            return self._one(
                request, self.presence(user), f"{GRAPH}/$metadata#users('{user.user.id}')/presence/$entity"
            )
        if parts[2:] == ["mailboxSettings"]:
            self._world.saw(user_ref(user.user.id), Operation.READ)
            settings = wire.MailboxSettings(automaticRepliesSetting=self._replies(user))
            return self._one(request, settings, f"{GRAPH}/$metadata#users('{user.user.id}')/mailboxSettings")
        if parts[2:] == ["mailboxSettings", "automaticRepliesSetting"]:
            self._world.saw(user_ref(user.user.id), Operation.READ)
            context = f"{GRAPH}/$metadata#users('{user.user.id}')/mailboxSettings/automaticRepliesSetting"
            return self._one(request, self._replies(user), context)
        if parts[2:] == ["chats"]:
            chats = [
                self._chat(c)
                for c in self._world.conversations()
                if c.type is not wire.ConversationType.CHANNEL and user.user.id in c.members
            ]
            return self._page(request, chats, f"{GRAPH}/$metadata#users('{user.user.id}')/chats")
        raise NotServed(f"the segment '{'/'.join(parts[2:])}'")

    # ------------------------------------------------------------------ presence and automatic replies

    def presence(self, user: UserRecord) -> wire.Presence:
        """Out of office while an absence of theirs lasts, with their automatic reply; available otherwise."""
        now = self._clock.now()
        away = self._world.away(user, now)
        if away is None or not away[0] <= now < away[1]:
            return wire.Presence(
                id=user.user.id,
                availability="Available",
                activity="Available",
                outOfOfficeSettings=wire.OutOfOfficeSettings(isOutOfOffice=False),
            )
        return wire.Presence(
            id=user.user.id,
            availability="Away",
            activity="OutOfOffice",
            outOfOfficeSettings=wire.OutOfOfficeSettings(message=_reply_text(user, away), isOutOfOffice=True),
        )

    def _replies(self, user: UserRecord) -> wire.AutomaticRepliesSetting:
        """Scheduled over the absence that lasts now, or the next one known; disabled when none is."""
        away = self._world.away(user, self._clock.now())
        if away is None:
            return wire.AutomaticRepliesSetting(status="disabled")
        message = _reply_text(user, away)
        return wire.AutomaticRepliesSetting(
            status="scheduled",
            scheduledStartDateTime=wire.DateTimeTimeZone(dateTime=_mailbox_time(away[0])),
            scheduledEndDateTime=wire.DateTimeTimeZone(dateTime=_mailbox_time(away[1])),
            internalReplyMessage=message,
            externalReplyMessage=message,
        )

    async def communications(self, request: Request, parts: list[str]) -> Response:
        """`GET /communications/presences/{id}` and `POST /communications/getPresencesByUserId`."""
        if request.method == "GET" and len(parts) == 3 and parts[1] == "presences":
            user = self._user(parts[2])
            self._world.saw(user_ref(user.user.id), Operation.READ)
            return self._one(request, self.presence(user), f"{GRAPH}/$metadata#communications/presences/$entity")
        if request.method == "POST" and parts[1:] == ["getPresencesByUserId"]:
            try:
                asked = wire.read(wire.PresencesByUserId, await request.body())
            except wire.Unreadable as e:
                raise NotServed(
                    f"a request body that cannot be read ({e.message}): Graph's answer is not recorded"
                ) from e
            found = [self.presence(self._user(i)) for i in asked.ids]
            return self._page(request, found, f"{GRAPH}/$metadata#Collection(microsoft.graph.presence)")
        raise NotServed(f"the segment '{'/'.join(parts)}'")

    # ------------------------------------------------------------------ teams and channels

    def _team(self, key: str) -> TeamRecord:
        found = self._world.team(key)
        if found is None:
            raise GraphRefusal(404, "NotFound", f"No team found with Group Id {key}")
        return found

    def _channel(self, team: TeamRecord, key: str) -> ConversationRecord:
        found = next((c for c in self._world.channels_of(team.id) if c.graph_id == key), None)
        if found is None:
            raise GraphRefusal(404, "NotFound", f"Channel {key} was not found in team {team.id}.")
        return found

    def _graph_channel(self, channel: ConversationRecord) -> wire.GraphChannel:
        return wire.GraphChannel(
            id=channel.graph_id,
            displayName=channel.display_name or "General",
            createdDateTime=channel.created,
            webUrl=f"https://teams.microsoft.com/l/channel/{channel.graph_id}",
            tenantId=channel.tenant_id,
        )

    async def teams(self, request: Request, parts: list[str]) -> Response:
        if len(parts) < 2:
            raise NotServed("listing teams")
        team = self._team(parts[1])
        rest = parts[2:]
        if not rest and request.method == "GET":
            self._world.saw(team_ref(team.id), Operation.READ)
            answer = wire.GraphTeam(
                id=team.id,
                displayName=team.display_name,
                internalId=team.general_channel_id,
                webUrl=f"https://teams.microsoft.com/l/team/{team.general_channel_id}",
            )
            return self._one(request, answer, f"{GRAPH}/$metadata#teams/$entity")
        if rest == ["members"]:
            return self._members(request, team.members, team.tenant_id, f"teams('{team.id}')/members")
        if rest[:1] != ["channels"]:
            raise NotServed(f"the segment '{'/'.join(rest)}'")
        if len(rest) == 1:
            self._refuse_options(request, {"$filter"})
            channels = self._world.channels_of(team.id)
            clause = query(request, "$filter")
            if clause:
                equal = re.fullmatch(r"\s*displayName\s+eq\s+'((?:[^']|'')*)'\s*", clause)
                if equal is None:
                    raise NotServed(f"the $filter clause {clause!r}")
                channels = [c for c in channels if (c.display_name or "General") == _quoted(equal.group(1))]
            self._world.saw(team_ref(team.id), Operation.SEARCH)
            return self._page(
                request,
                [self._graph_channel(c) for c in channels],
                f"{GRAPH}/$metadata#teams('{team.id}')/channels",
                default=100,
                most=999,
            )
        channel = self._channel(team, rest[1])
        if len(rest) == 2:
            self._world.saw(conversation_ref(channel.id), Operation.READ)
            return self._one(
                request, self._graph_channel(channel), f"{GRAPH}/$metadata#teams('{team.id}')/channels/$entity"
            )
        if rest[2:] == ["members"]:
            return self._members(
                request,
                channel.members,
                channel.tenant_id,
                f"teams('{team.id}')/channels('{channel.graph_id}')/members",
            )
        if rest[2] == "messages":
            return await self._messages(
                request, channel, rest[3:], f"teams('{team.id}')/channels('{channel.graph_id}')/messages"
            )
        raise NotServed(f"the segment '{'/'.join(rest[2:])}'")

    def _members(self, request: Request, members: list[str], tenant: str, context: str) -> Response:
        found: list[wire.ConversationMember] = []
        for oid in members:
            user = self._world.user(oid)
            if user is None:
                continue
            found.append(
                wire.ConversationMember(
                    id=f"MCMjMSMj{oid}",
                    displayName=user.user.displayName,
                    userId=oid,
                    email=user.user.mail,
                    tenantId=tenant,
                )
            )
        return self._page(request, found, f"{GRAPH}/$metadata#{context}", default=100, most=999)

    # ------------------------------------------------------------------ chats

    def _chat(self, conversation: ConversationRecord) -> wire.GraphChat:
        kind = "oneOnOne" if conversation.type is wire.ConversationType.PERSONAL else "group"
        return wire.GraphChat(
            id=conversation.graph_id,
            topic=conversation.display_name,
            chatType=kind,
            createdDateTime=conversation.created,
            lastUpdatedDateTime=conversation.created,
            tenantId=conversation.tenant_id,
            webUrl=f"https://teams.microsoft.com/l/chat/{conversation.graph_id}/0",
        )

    async def chats(self, request: Request, parts: list[str]) -> Response:
        claims = graph_caller(request, self._world)
        if len(parts) == 1:
            if claims.oid is None:
                raise NotServed(
                    "GET /chats with no signed-in user: Graph lists a signed-in user's chats, and does not document "
                    "its answer to an application"
                )
            mine = [
                self._chat(c)
                for c in self._world.conversations()
                if c.type is not wire.ConversationType.CHANNEL and claims.oid in c.members
            ]
            return self._page(request, mine, f"{GRAPH}/$metadata#chats")
        chat = self._world.conversation_by_graph_id(parts[1])
        if chat is None or chat.type is wire.ConversationType.CHANNEL:
            raise GraphRefusal(404, "NotFound", f"Chat {parts[1]} was not found.")
        rest = parts[2:]
        if not rest:
            self._world.saw(conversation_ref(chat.id), Operation.READ)
            return self._one(request, self._chat(chat), f"{GRAPH}/$metadata#chats/$entity")
        if rest == ["members"]:
            return self._members(request, chat.members, chat.tenant_id, f"chats('{chat.graph_id}')/members")
        if rest[0] == "messages":
            return await self._messages(request, chat, rest[1:], f"chats('{chat.graph_id}')/messages")
        raise NotServed(f"the segment '{'/'.join(rest)}'")

    # ------------------------------------------------------------------ messages

    async def _messages(
        self, request: Request, conversation: ConversationRecord, rest: list[str], context: str
    ) -> Response:
        channel = conversation.type is wire.ConversationType.CHANNEL
        every = self._world.messages(conversation.id)
        roots = [m for m in every if not channel or m.replyToId is None]

        def modified(m: wire.Activity) -> str:
            return m.localTimestamp or m.timestamp

        def replies_of(root: str) -> list[wire.ChatMessage]:
            """Newest first, as the documented example answer of chatmessage-list-replies lists them."""
            replies = sorted(
                reversed([m for m in every if m.replyToId == root]), key=lambda m: m.timestamp, reverse=True
            )
            return [chat_message(self._world, conversation, m) for m in replies]

        def latest(m: wire.Activity) -> str:
            """A channel message by its whole reply chain's last change (channel-list-messages), a chat's by its own
            `lastModifiedDateTime`, the default chat-list-messages documents; newest first either way."""
            if not channel:
                return modified(m)
            return max([modified(m), *(modified(r) for r in every if r.replyToId == m.id)])

        if not rest:
            self._refuse_options(request, {"$expand"} if channel else set())
            expand = query(request, "$expand")
            if expand is not None and expand != "replies":
                raise NotServed(f"$expand={expand}")
            found = []
            for m in sorted(reversed(roots), key=latest, reverse=True):
                message = chat_message(self._world, conversation, m)
                if expand == "replies":
                    message = message.model_copy(
                        update={
                            "replies": replies_of(m.id),
                            "replies_context": f"{GRAPH}/$metadata#{context}('{m.id}')/replies",
                        }
                    )
                found.append(message)
            self._world.saw(conversation_ref(conversation.id), Operation.READ)
            return self._page(request, found, f"{GRAPH}/$metadata#{context}")
        root = next((m for m in every if m.id == rest[0]), None)
        if root is None:
            raise not_found(rest[0])
        if len(rest) == 1:
            self._world.saw(conversation_ref(conversation.id), Operation.READ)
            return self._one(
                request, chat_message(self._world, conversation, root), f"{GRAPH}/$metadata#{context}/$entity"
            )
        if rest[1:] == ["replies"] and channel:
            self._refuse_options(request, set())
            self._world.saw(conversation_ref(conversation.id), Operation.READ)
            return self._page(request, replies_of(root.id), f"{GRAPH}/$metadata#{context}('{root.id}')/replies")
        raise NotServed(f"the segment '{'/'.join(rest[1:])}'")


def _mailbox_time(at: datetime) -> str:
    """A `dateTimeTimeZone`'s `dateTime`: local to its `timeZone` (UTC here), seven digits of fraction."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def _reply_text(user: UserRecord, away: tuple[datetime, datetime, AwayRecord]) -> str:
    """The automatic reply a person away set: the reason the scenario gives, as written, and nothing composed around
    it (Minutehand keeps data as sent); empty when none is given."""
    del user
    return away[2].reason or ""
