"""The Bot Framework connector a Teams bot sends to, at the `serviceUrl` every pushed activity names
(`https://smba.trafficmanager.net/teams/`; any region segment is answered the same).

| Route (under `/{region}/v3`) | What it does |
|---|---|
| `POST /conversations` | Proactive 1:1 with a user of the tenant the bot is installed for |
| `POST /conversations/{id}/activities` | Send; a `;messageid=<root>` id, or `replyToId` in a channel, threads it |
| `POST /conversations/{id}/activities/{activityId}` | Reply to an activity |
| `PUT /conversations/{id}/activities/{activityId}` | Update the bot's own activity |
| `DELETE /conversations/{id}/activities/{activityId}` | Delete the bot's own activity |
| `GET /conversations/{id}/members`, `/pagedmembers`, `/members/{member}` | The conversation's roster |
| `GET /teams/{team}`, `/teams/{team}/conversations` | A team's details and channels |

Every call must carry a token the identity platform issued to a bot registered in the world for
`https://api.botframework.com`; anything else is 401, with the connector's own body. A bot not installed in the
conversation is refused 403, an unknown conversation 404. Activities are stored whole: an Adaptive Card stays
the JSON the bot sent (`cards.cards_of` lists its inputs and actions).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Router

from minutehand.adapters.providers.microsoft import cards, tokens, wire
from minutehand.adapters.providers.microsoft.common import JSON, MicrosoftRefusal, bearer, query
from minutehand.adapters.providers.microsoft.state import (
    SERVICE_URL,
    AppRecord,
    ConversationRecord,
    MicrosoftWorld,
    UserRecord,
    bot_mri,
    conversation_ref,
    graph_time,
    message_ref,
)
from minutehand.adapters.providers.microsoft.wire import TokenUse
from minutehand.domain.errors import Rendered
from minutehand.domain.world import Actor, MessageSnapshot, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

# Teams' documented limits: a message of about 100 KB counted as UTF-16, and member pages of 50 to 500, 200 by default
# (bots/build-conversational-capability, bots/how-to/get-teams-context on learn.microsoft.com).
MAX_ACTIVITY_UTF16_BYTES = 100 * 1024
PAGE_DEFAULT = 200
PAGE_MIN = 50
PAGE_MAX = 500
DENIED_MESSAGE = "Authorization has been denied for this request."
DENIED = f'{{"message":"{DENIED_MESSAGE}"}}'


class ConnectorRefusal(MicrosoftRefusal):
    """The connector refused, in its `{"error": {"code", "message"}}`, with `Retry-After` when it says when to try
    again."""

    def __init__(
        self, status: int, code: str, message: str, *, retry_after: int | None = None, deliberate: bool = False
    ) -> None:
        super().__init__(code, message, status=status, deliberate=deliberate)
        self.retry_after = retry_after

    def render(self) -> Rendered:
        body = wire.ConnectorError(error=wire.ConnectorErrorBody(code=self.code, message=self.message))
        headers = [("retry-after", str(self.retry_after))] if self.retry_after is not None else []
        return Rendered(status=self.status, content_type=JSON, body=wire.dump(body).encode(), headers=headers)


class Denied(MicrosoftRefusal):
    """The call carries no token the connector accepts: 401, in the body ASP.NET's authorization filter writes,
    which names no code."""

    def __init__(self) -> None:
        super().__init__("Unauthorized", DENIED_MESSAGE, status=401)

    def render(self) -> Rendered:
        return Rendered(status=401, content_type=JSON, body=DENIED.encode())


def split_conversation(conversation: str) -> tuple[str, str | None]:
    """A connector conversation id and the thread root its `;messageid=` suffix names, if any."""
    base, _, rest = conversation.partition(";messageid=")
    return base, rest or None


def snapshot(
    world: MicrosoftWorld, conversation: ConversationRecord, activity: wire.Activity, author: str | None
) -> MessageSnapshot:
    """A message as the checks read it: who it reaches is every human member but its author."""
    emails, people = reached(world, conversation, author)
    thread = activity.replyToId if conversation.type is wire.ConversationType.CHANNEL else None
    return MessageSnapshot(
        text=cards.visible_text(activity),
        channel=conversation.id,
        recipient_emails=emails,
        recipients=people,
        thread_of=thread,
        actions=cards.message_actions(activity),
    )


def reached(world: MicrosoftWorld, conversation: ConversationRecord, author: str | None) -> tuple[list[str], list[str]]:
    """Whom a message in `conversation` reaches, every human member but its author: their emails (those who have
    one, shown or not) and their person keys."""
    emails: list[str] = []
    people: list[str] = []
    for oid in conversation.members:
        if oid == author:
            continue
        user = world.user(oid)
        if user is None:
            continue
        if user.reached_as is not None:
            emails.append(user.reached_as)
        if user.person_key is not None:
            people.append(user.person_key)
    return emails, people


def member_of(user: UserRecord) -> wire.TeamsChannelAccount:
    return wire.TeamsChannelAccount(
        id=user.mri,
        name=user.user.displayName,
        aadObjectId=user.user.id,
        email=user.user.mail,
        userPrincipalName=user.user.userPrincipalName,
        givenName=user.user.givenName,
        surname=user.user.surname,
        tenantId=user.tenant_id,
    )


def bot_account(app: AppRecord) -> wire.ChannelAccount:
    return wire.ChannelAccount(id=bot_mri(app.app_id), name=app.display_name)


class Connector:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = MicrosoftWorld(store)
        self._clock = clock

    # ------------------------------------------------------------------ lookups

    def _bot(self, request: Request) -> AppRecord:
        token = bearer(request)
        if token is None:
            raise Denied()
        try:
            claims = tokens.decode(token, use=TokenUse.ACCESS)
        except tokens.TokenRefused as e:
            raise Denied() from e
        app = self._world.app(claims.appid)
        if claims.aud != tokens.BOT_FRAMEWORK_AUDIENCE or app is None:
            raise Denied()
        return app

    def _conversation(self, conversation: str, app: AppRecord) -> ConversationRecord:
        found = self._world.conversation(conversation)
        if found is None:
            raise ConnectorRefusal(404, "ConversationNotFound", "Conversation not found.")
        if not found.bot_installed or self._world.tenant(found.tenant_id) is None or app.tenant_id != found.tenant_id:
            raise ConnectorRefusal(403, "BotNotInConversationRoster", "The bot is not part of the conversation roster.")
        if found.type is wire.ConversationType.PERSONAL and not self._reachable(found):
            raise ConnectorRefusal(403, "BotNotInConversationRoster", "The bot is not part of the conversation roster.")
        return found

    def _reachable(self, personal: ConversationRecord) -> bool:
        """A 1:1 chat reaches its person while their account is there and enabled: a removed or disabled user's
        chat refuses the bot, as Teams does once the directory has dropped them."""
        users = [self._world.user(oid) for oid in personal.members]
        return all(u is not None and u.user.accountEnabled is not False for u in users)

    def _activity_body(self, request_body: bytes) -> wire.SentActivity:
        if len(request_body.decode("utf-8", errors="replace").encode("utf-16-le")) > MAX_ACTIVITY_UTF16_BYTES:
            raise ConnectorRefusal(413, "MessageSizeTooBig", "Message size too large.")
        try:
            return wire.read(wire.SentActivity, request_body)
        except wire.Unreadable as e:
            raise ConnectorRefusal(400, "BadArgument", f"The activity could not be read: {e.message}.") from e

    # ------------------------------------------------------------------ send, reply

    async def send(self, request: Request) -> Response:
        app = self._bot(request)
        base, root = split_conversation(request.path_params["conversation"])
        conversation = self._conversation(base, app)
        sent = self._activity_body(await request.body())
        reply_to = ("activity" in request.path_params and request.path_params["activity"]) or sent.replyToId or root
        activity = self.post(app, conversation, sent, reply_to=reply_to)
        return Response(wire.dump(wire.ResourceResponse(id=activity.id)), status_code=201, media_type=JSON)

    def post(
        self, app: AppRecord, conversation: ConversationRecord, sent: wire.SentActivity, *, reply_to: str | None
    ) -> wire.Activity:
        if sent.type != wire.ActivityType.MESSAGE.value:
            raise ConnectorRefusal(400, "BadArgument", f"Activity type '{sent.type}' cannot be sent to a conversation.")
        if not sent.text and not sent.attachments:
            raise ConnectorRefusal(400, "BadArgument", "Activity must have text or attachments.")
        thread: str | None = None
        if reply_to is not None:
            found = self._world.message(reply_to)
            if found is None or found[0] != conversation.id:
                raise ConnectorRefusal(404, "ActivityNotFoundInConversation", "Conversation not found.")
            parent = found[1]
            thread = parent.replyToId or parent.id if conversation.type is wire.ConversationType.CHANNEL else parent.id
        activity = wire.Activity(
            type=wire.ActivityType.MESSAGE,
            id=self._world.next_activity_id(self._clock),
            timestamp=graph_time(self._clock.now()),
            serviceUrl=SERVICE_URL,
            sender=bot_account(app),
            conversation=wire.ConversationAccount(
                id=conversation.id,
                conversationType=conversation.type,
                tenantId=conversation.tenant_id,
                isGroup=conversation.type is not wire.ConversationType.PERSONAL,
            ),
            recipient=wire.ChannelAccount(id=conversation.id),
            text=sent.text,
            textFormat=sent.textFormat,
            attachments=sent.attachments,
            replyToId=thread,
        )
        self._world.write(
            message_ref(activity.id),
            activity,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=conversation.id,
            after=snapshot(self._world, conversation, activity, None),
        )
        return activity

    # ------------------------------------------------------------------ update, delete

    async def update(self, request: Request) -> Response:
        app = self._bot(request)
        base, _ = split_conversation(request.path_params["conversation"])
        conversation = self._conversation(base, app)
        sent = self._activity_body(await request.body())
        found = self._world.message(request.path_params["activity"])
        if found is None or found[0] != conversation.id:
            raise ConnectorRefusal(404, "ActivityNotFoundInConversation", "Conversation not found.")
        current = found[1]
        if current.sender.id != bot_mri(app.app_id):
            raise ConnectorRefusal(403, "NotEnoughPermissions", "A bot can update only the activities it sent.")
        if sent.text and sent.attachments:
            raise ConnectorRefusal(400, "BadSyntax", "Activity resulted into multiple skype activities")
        updated = current.model_copy(
            update={
                "text": sent.text if sent.text is not None else (None if sent.attachments else current.text),
                "attachments": sent.attachments if sent.attachments is not None else current.attachments,
            }
        )
        self._world.write(
            message_ref(updated.id),
            updated,
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=conversation.id,
            after=snapshot(self._world, conversation, updated, None),
        )
        return Response(wire.dump(wire.ResourceResponse(id=updated.id)), media_type=JSON)

    async def delete(self, request: Request) -> Response:
        app = self._bot(request)
        base, _ = split_conversation(request.path_params["conversation"])
        conversation = self._conversation(base, app)
        found = self._world.message(request.path_params["activity"])
        if found is None or found[0] != conversation.id:
            raise ConnectorRefusal(404, "ActivityNotFoundInConversation", "Conversation not found.")
        if found[1].sender.id != bot_mri(app.app_id):
            raise ConnectorRefusal(403, "NotEnoughPermissions", "A bot can delete only the activities it sent.")
        self._world.remove(message_ref(found[1].id), actor=Actor.AGENT, parent=conversation.id)
        return Response(status_code=200)

    # ------------------------------------------------------------------ create conversation

    async def create(self, request: Request) -> Response:
        app = self._bot(request)
        try:
            asked = wire.read(wire.SentConversation, await request.body())
        except wire.Unreadable as e:
            raise ConnectorRefusal(400, "BadArgument", f"The conversation could not be read: {e.message}.") from e
        tenant = asked.channelData.tenant.id if asked.channelData and asked.channelData.tenant else asked.tenantId
        if not tenant:
            raise ConnectorRefusal(400, "BadArgument", "Tenant id is required to create a conversation in Teams.")
        if asked.bot is not None and asked.bot.id not in (app.app_id, bot_mri(app.app_id)):
            raise ConnectorRefusal(400, "BadArgument", "The bot in the request is not the bot that signed in.")
        if asked.isGroup or len(asked.members) != 1:
            raise ConnectorRefusal(
                400, "BadArgument", "Only a 1:1 conversation with exactly one member can be created by a bot."
            )
        member = asked.members[0].id
        user = self._world.user_by_mri(member) or self._world.user(member)
        if user is None or user.tenant_id != tenant or app.tenant_id != tenant:
            raise ConnectorRefusal(404, "MemberNotFound", "The member was not found in the tenant.")
        conversation = self._world.personal_with(user.user.id, tenant)
        if user.user.accountEnabled is False:
            raise ConnectorRefusal(403, "BotNotInConversationRoster", "The bot is not part of the conversation roster.")
        if conversation is None or not conversation.bot_installed:
            raise ConnectorRefusal(
                403, "ForbiddenOperationException", "The bot is not installed in the user's personal scope."
            )
        self._world.saw(conversation_ref(conversation.id), Operation.READ)
        activity_id: str | None = None
        if asked.activity is not None:
            activity_id = self.post(app, conversation, asked.activity, reply_to=None).id
        answer = wire.ConversationResourceResponse(id=conversation.id, serviceUrl=SERVICE_URL, activityId=activity_id)
        return Response(wire.dump(answer), status_code=201, media_type=JSON)

    # ------------------------------------------------------------------ members

    def _members(self, conversation: ConversationRecord) -> list[wire.TeamsChannelAccount]:
        found = (self._world.user(oid) for oid in conversation.members)
        return [member_of(u) for u in found if u is not None]

    async def members(self, request: Request) -> Response:
        app = self._bot(request)
        conversation = self._conversation(split_conversation(request.path_params["conversation"])[0], app)
        self._world.saw(conversation_ref(conversation.id), Operation.READ)
        body = "[" + ",".join(wire.dump(m) for m in self._members(conversation)) + "]"
        return Response(body, media_type=JSON)

    async def paged_members(self, request: Request) -> Response:
        app = self._bot(request)
        conversation = self._conversation(split_conversation(request.path_params["conversation"])[0], app)
        size_text = query(request, "pageSize")
        size = min(max(int(size_text), PAGE_MIN), PAGE_MAX) if size_text and size_text.isdigit() else PAGE_DEFAULT
        token = query(request, "continuationToken")
        everyone = self._members(conversation)
        start = 0
        if token:
            position = next((i for i, m in enumerate(everyone) if m.aadObjectId == token), None)
            if position is None:
                raise ConnectorRefusal(400, "BadArgument", "The continuation token is not valid.")
            start = position
        page = everyone[start : start + size]
        following = everyone[start + size].aadObjectId if start + size < len(everyone) else None
        self._world.saw(conversation_ref(conversation.id), Operation.READ)
        return Response(wire.dump(wire.PagedMembers(members=page, continuationToken=following)), media_type=JSON)

    async def member(self, request: Request) -> Response:
        app = self._bot(request)
        conversation = self._conversation(split_conversation(request.path_params["conversation"])[0], app)
        wanted = request.path_params["member"]
        found = next((m for m in self._members(conversation) if wanted in (m.id, m.aadObjectId)), None)
        if found is None:
            raise ConnectorRefusal(404, "MemberNotFound", "The member was not found in the conversation.")
        self._world.saw(conversation_ref(conversation.id), Operation.READ)
        return Response(wire.dump(found), media_type=JSON)

    # ------------------------------------------------------------------ teams

    def _team_record(self, request: Request, app: AppRecord) -> tuple[str, ConversationRecord]:
        thread = request.path_params["team"]
        team = self._world.team_by_thread(thread) or self._world.team(thread)
        if team is None:
            raise ConnectorRefusal(404, "NotFound", "The team was not found.")
        general = self._conversation(team.general_channel_id, app)
        return team.id, general

    async def team(self, request: Request) -> Response:
        app = self._bot(request)
        team_id, general = self._team_record(request, app)
        team = self._world.team(team_id)
        assert team is not None
        self._world.saw(conversation_ref(general.id), Operation.READ)
        answer = wire.TeamDetails(id=team.general_channel_id, name=team.display_name, aadGroupId=team.id)
        return Response(wire.dump(answer), media_type=JSON)

    async def team_conversations(self, request: Request) -> Response:
        app = self._bot(request)
        team_id, general = self._team_record(request, app)
        channels = self._world.channels_of(team_id)
        self._world.saw(conversation_ref(general.id), Operation.READ)
        answer = wire.ConversationList(
            conversations=[
                wire.ChannelSummary(id=c.id, name=None if c.id == general.id else c.display_name) for c in channels
            ]
        )
        # Written with its nulls: General's `name` is null on the wire, which `wire.dump` would drop.
        return Response(answer.model_dump_json(by_alias=True), media_type=JSON)


def connector_router(store: Store, clock: Clock) -> Router:
    api = Connector(store, clock)
    conv = "/{region}/v3/conversations"
    return Router(
        routes=[
            Route(conv, api.create, methods=["POST"]),
            Route(conv + "/{conversation}/activities", api.send, methods=["POST"]),
            Route(conv + "/{conversation}/activities/{activity}", api.send, methods=["POST"]),
            Route(conv + "/{conversation}/activities/{activity}", api.update, methods=["PUT"]),
            Route(conv + "/{conversation}/activities/{activity}", api.delete, methods=["DELETE"]),
            Route(conv + "/{conversation}/members", api.members, methods=["GET"]),
            Route(conv + "/{conversation}/pagedmembers", api.paged_members, methods=["GET"]),
            Route(conv + "/{conversation}/members/{member}", api.member, methods=["GET"]),
            Route("/{region}/v3/teams/{team}", api.team, methods=["GET"]),
            Route("/{region}/v3/teams/{team}/conversations", api.team_conversations, methods=["GET"]),
        ]
    )
