"""What a person does in Teams, put into the tenant and pushed to the bot as the Bot Framework pushes it: a POST of
the activity to the bot's messaging endpoint with `Authorization: Bearer <JWT>`, the JWT signed with the key the
Bot Framework's OpenID metadata publishes, issued by `https://api.botframework.com`, for the bot's app id, naming
the `serviceUrl` the activity carries.

| What the person does | Activity pushed |
|---|---|
| writes to the bot (`say`), answers it (`deliver`), posts (`happen`: `PersonPosts`) | `message`; in a channel or group chat only when the bot is @-mentioned, as Teams delivers it |
| edits, deletes a post | `messageUpdate` (`editMessage`), `messageDelete` (`softDeleteMessage`) |
| reacts | `messageReaction` with `reactionsAdded` |
| joins a channel | `conversationUpdate` with `membersAdded` |
| installs the bot (`install`) | `installationUpdate` (`add`), then `conversationUpdate` with the bot in `membersAdded` |
| presses a card's button (`press`) | `invoke` `adaptiveCard/action` for `Action.Execute`; `message` with `value` for `Action.Submit` |

An answer to an `invoke` whose `type` is an Adaptive Card replaces the card on the message, as Teams redraws it;
that change is the agent's. A person's reply in a channel goes in the thread of what it answers and @-mentions
the bot, so it reaches it.
"""

from __future__ import annotations

import httpx
from pydantic import JsonValue, ValidationError

from minutehand.adapters.providers.microsoft import cards, tokens, wire
from minutehand.adapters.providers.microsoft.connector import snapshot
from minutehand.adapters.providers.microsoft.manifest import MANIFEST
from minutehand.adapters.providers.microsoft.state import (
    POSTS,
    SERVICE_URL,
    ActionRecord,
    AppRecord,
    ConversationRecord,
    MicrosoftWorld,
    PostRecord,
    UserRecord,
    action_ref,
    bot_mri,
    graph_time,
    message_ref,
    post_ref,
    reaction_ref,
)
from minutehand.adapters.providers.microsoft.subscriptions import conversation_watch, notify
from minutehand.adapters.providers.microsoft.wire import TokenUse
from minutehand.application.refusals import AgentFailed
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.scenario import (
    Happening,
    PersonCommands,
    PersonDeletes,
    PersonEdits,
    PersonJoins,
    PersonOpensAgent,
    PersonPosts,
    PersonReacts,
)
from minutehand.domain.world import (
    Actor,
    EntityKind,
    InteractionKind,
    InteractionSnapshot,
    Operation,
    RecordSnapshot,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

CHANNEL_APP_ID = "4d0f5c3e-6b1a-4e8f-9d2c-7a1b3c5e9f01"
"""The app the channel service signs pushed activities as (`appid`); the bot checks the audience, not this."""


class DeliveryRefused(AgentFailed):
    """The bot answered a pushed activity with something other than 2xx, or could not be reached."""

    def __init__(self, url: str, status: int | None, body: str) -> None:
        answered = f"answered {status}" if status is not None else "could not be reached"
        super().__init__(f"{url} {answered} to a Teams activity: {body[:200]}")
        self.status = status


def activity_token(app: AppRecord) -> str:
    token, _ = tokens.issued(
        use=TokenUse.ACTIVITY,
        issuer=tokens.BOT_FRAMEWORK_ISSUER,
        audience=app.app_id,
        tenant=None,
        app_id=CHANNEL_APP_ID,
        user=None,
        service_url=SERVICE_URL,
        lifetime=3600,
    )
    return token


async def push(
    target: InboundTarget, app: AppRecord, activity: wire.Activity, *, url: str | None = None
) -> httpx.Response:
    if target.provider != MANIFEST.key:
        raise ValueError(f"a {target.provider} target is not Microsoft's to deliver to")
    where = url or target.url
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            answered = await client.post(
                where,
                content=wire.dump(activity),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {activity_token(app)}",
                    "User-Agent": "Microsoft-SkypeBotApi (Microsoft-BotFramework/3.0)",
                },
            )
    except httpx.HTTPError as e:
        raise DeliveryRefused(where, None, repr(e)) from e
    if not answered.is_success:
        raise DeliveryRefused(where, answered.status_code, answered.text)
    return answered


class People:
    """Everything a person does in Teams, over one world."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self.world = MicrosoftWorld(store)
        self.clock = clock

    # ------------------------------------------------------------------ lookups

    def app(self) -> AppRecord:
        apps = self.world.apps()
        if len(apps) != 1:
            raise LookupError(f"the tenant must hold exactly one bot to push to; it holds {len(apps)}")
        return apps[0]

    def person(self, key: str) -> UserRecord:
        found = self.world.person(key)
        if found is None:
            raise LookupError(f"{key} is not a user of the tenant")
        return found

    def personal(self, user: UserRecord) -> ConversationRecord:
        found = self.world.personal_with(user.user.id, user.tenant_id)
        if found is None:
            raise LookupError(f"{user.person_key} has no 1:1 chat with the bot")
        return found

    def named(self, channel: str | None, user: UserRecord) -> ConversationRecord:
        if channel is None:
            return self.personal(user)
        found = next(
            (
                c
                for c in self.world.conversations()
                if (c.display_name or ("general" if c.type is wire.ConversationType.CHANNEL else None)) == channel
            ),
            None,
        )
        if found is None:
            raise LookupError(f"no channel {channel} in the tenant")
        return found

    def located(self, activity_id: str) -> tuple[ConversationRecord, wire.Activity]:
        found = self.world.message(activity_id)
        if found is None:
            raise LookupError(f"no Teams message {activity_id}")
        conversation = self.world.conversation(found[0])
        assert conversation is not None
        return conversation, found[1]

    def _post_id(self, key: str) -> str:
        found = self.world.post(key)
        if found is None:
            raise LookupError(f"no post {key} in the tenant")
        return found

    # ------------------------------------------------------------------ building activities

    def _activity(
        self,
        kind: wire.ActivityType,
        user: UserRecord,
        conversation: ConversationRecord,
        *,
        thread: str | None,
        own_id: str | None = None,
        **fields: object,
    ) -> wire.Activity:
        app = self.app()
        activity_id = own_id or self.world.next_activity_id(self.clock)
        channel = conversation.type is wire.ConversationType.CHANNEL
        team = self.world.team(conversation.team_id) if conversation.team_id is not None else None
        connector_id = f"{conversation.id};messageid={thread or activity_id}" if channel else conversation.id
        return wire.Activity(
            type=kind,
            id=activity_id,
            timestamp=graph_time(self.clock.now()),
            localTimestamp=graph_time(self.clock.now()),
            serviceUrl=SERVICE_URL,
            sender=wire.ChannelAccount(id=user.mri, name=user.user.displayName, aadObjectId=user.user.id),
            conversation=wire.ConversationAccount(
                id=connector_id,
                conversationType=conversation.type,
                tenantId=conversation.tenant_id,
                isGroup=conversation.type is not wire.ConversationType.PERSONAL or None,
            ),
            recipient=wire.ChannelAccount(id=bot_mri(app.app_id), name=app.display_name),
            channelData=wire.ChannelData(
                tenant=wire.TenantInfo(id=conversation.tenant_id),
                team=wire.TeamInfo(id=team.general_channel_id, name=team.display_name, aadGroupId=team.id)
                if channel and team is not None
                else None,
                channel=wire.ChannelInfo(id=conversation.id, name=conversation.display_name) if channel else None,
                eventType=fields.pop("event_type") if "event_type" in fields else None,  # type: ignore[arg-type]
            ),
            locale="en-US",
            **fields,  # type: ignore[arg-type]
        )

    def _mention(self, app: AppRecord) -> wire.Mention:
        return wire.Mention(
            mentioned=wire.ChannelAccount(id=bot_mri(app.app_id), name=app.display_name),
            text=f"<at>{app.display_name}</at>",
        )

    async def _message(
        self,
        user: UserRecord,
        conversation: ConversationRecord,
        text: str,
        *,
        thread: str | None,
        mention: bool,
        target: InboundTarget,
        key: str | None = None,
    ) -> wire.Activity:
        app = self.app()
        entities = [self._mention(app)] if mention else None
        written = f"<at>{app.display_name}</at> {text}" if mention else text
        activity = self._activity(
            wire.ActivityType.MESSAGE,
            user,
            conversation,
            thread=thread,
            text=written,
            textFormat="plain",
            entities=entities,
            replyToId=thread,
        )
        stored = activity.model_copy(
            update={"conversation": activity.conversation.model_copy(update={"id": conversation.id})}
        )
        self.world.write(
            message_ref(activity.id),
            stored,
            operation=Operation.CREATE,
            actor=Actor.PERSON,
            parent=conversation.id,
            after=snapshot(self.world, conversation, stored, user.user.id),
        )
        if key is not None:
            self.world.write(
                post_ref(key),
                PostRecord(key=key, activity_id=activity.id),
                operation=Operation.CREATE,
                actor=Actor.PERSON,
                parent=POSTS,
            )
        await notify(
            self.world,
            self.clock,
            conversation_watch(conversation.id),
            change="created",
            odata_type="#Microsoft.Graph.chatMessage",
            resource=f"chats('{conversation.graph_id}')/messages('{activity.id}')",
            item=activity.id,
        )
        reaches = conversation.type is wire.ConversationType.PERSONAL or mention
        if reaches and conversation.bot_installed:
            await push(target, app, activity)
        return stored

    # ------------------------------------------------------------------ the ports' acts

    async def say(self, message: PersonMessage, target: InboundTarget) -> None:
        user = self.person(message.person)
        await self._message(user, self.personal(user), message.text, thread=None, mention=False, target=target)

    async def deliver(self, reply: PersonReply, target: InboundTarget) -> None:
        if reply.in_reply_to.provider != MANIFEST.key or reply.in_reply_to.kind is not EntityKind.MESSAGE:
            raise ValueError(f"a reply answers a Microsoft message, not {reply.in_reply_to}")
        if reply.press is not None:
            await self.press(reply, target)
            return
        user = self.person(reply.person)
        conversation, asked = self.located(reply.in_reply_to.external_id)
        if user.user.id not in conversation.members:
            raise LookupError(f"{reply.person} is not in the conversation they are answering")
        channel = conversation.type is wire.ConversationType.CHANNEL
        thread = (asked.replyToId or asked.id) if channel else None
        await self._message(user, conversation, reply.text, thread=thread, mention=channel, target=target)

    async def happen(self, happening: Happening, target: InboundTarget) -> None:
        user = self.person(happening.person)
        if isinstance(happening, PersonPosts):
            conversation = self.named(happening.channel, user)
            thread = self._post_id(happening.in_thread_of) if happening.in_thread_of is not None else None
            await self._message(
                user,
                conversation,
                happening.text,
                thread=thread,
                mention=happening.mentions_agent,
                target=target,
                key=happening.key,
            )
        elif isinstance(happening, PersonCommands):
            conversation = self.named(happening.channel, user)
            text = f"{happening.command} {happening.text}".strip()
            await self._message(
                user,
                conversation,
                text,
                thread=None,
                mention=conversation.type is not wire.ConversationType.PERSONAL,
                target=target,
            )
        elif isinstance(happening, PersonEdits):
            await self.edit(user, self._post_id(happening.post), happening.text, target)
        elif isinstance(happening, PersonDeletes):
            await self.delete(user, self._post_id(happening.post), target)
        elif isinstance(happening, PersonReacts):
            if happening.post is not None:
                activity_id = self._post_id(happening.post)
            else:
                conversation = self.named(happening.channel, user)
                agent = [m for m in self.world.messages(conversation.id) if m.sender.id == bot_mri(self.app().app_id)]
                if not agent:
                    raise LookupError(
                        f"the agent has written nothing in {conversation.id} for {user.person_key} to react to"
                    )
                activity_id = agent[-1].id
            await self.react(user, activity_id, happening.reaction, target)
        elif isinstance(happening, PersonJoins):
            await self.join(user, self.named(happening.channel, user), target)
        elif isinstance(happening, PersonOpensAgent):
            raise ValueError(
                "Teams pushes nothing to a bot when a person opens its app; this happening has no Teams form"
            )

    async def edit(self, user: UserRecord, activity_id: str, text: str, target: InboundTarget) -> None:
        conversation, current = self.located(activity_id)
        if current.sender.id != user.mri:
            raise ValueError(f"{user.person_key} can edit only their own message")
        edited = current.model_copy(update={"text": text, "localTimestamp": graph_time(self.clock.now())})
        self.world.write(
            message_ref(activity_id),
            edited,
            operation=Operation.UPDATE,
            actor=Actor.PERSON,
            parent=conversation.id,
            after=snapshot(self.world, conversation, edited, user.user.id),
        )
        update = self._activity(
            wire.ActivityType.MESSAGE_UPDATE,
            user,
            conversation,
            thread=current.replyToId,
            own_id=activity_id,
            text=text,
            event_type="editMessage",
        )
        await self._reaching(conversation, current, update, target)

    async def delete(self, user: UserRecord, activity_id: str, target: InboundTarget) -> None:
        conversation, current = self.located(activity_id)
        if current.sender.id != user.mri:
            raise ValueError(f"{user.person_key} can delete only their own message")
        self.world.remove(message_ref(activity_id), actor=Actor.PERSON, parent=conversation.id)
        gone = self._activity(
            wire.ActivityType.MESSAGE_DELETE,
            user,
            conversation,
            thread=current.replyToId,
            own_id=activity_id,
            event_type="softDeleteMessage",
        )
        await self._reaching(conversation, current, gone, target)

    async def _reaching(
        self, conversation: ConversationRecord, current: wire.Activity, activity: wire.Activity, target: InboundTarget
    ) -> None:
        """Push an edit or a delete when the original reached the bot: in its chat, or because it named the bot."""
        named = any(m.mentioned.id == bot_mri(self.app().app_id) for m in current.entities or [])
        if conversation.bot_installed and (conversation.type is wire.ConversationType.PERSONAL or named):
            await push(target, self.app(), activity)

    async def react(self, user: UserRecord, activity_id: str, reaction: str, target: InboundTarget) -> None:
        conversation, current = self.located(activity_id)
        self.world.write(
            reaction_ref(activity_id, user.user.id),
            wire.Reaction(type=reaction),
            operation=Operation.CREATE,
            actor=Actor.PERSON,
            parent=f"reactions:{activity_id}",
            after=RecordSnapshot(resource="reaction", text=reaction),
        )
        reacted = self._activity(
            wire.ActivityType.MESSAGE_REACTION,
            user,
            conversation,
            thread=current.replyToId,
            reactionsAdded=[wire.Reaction(type=reaction)],
            replyToId=activity_id,
        )
        if conversation.bot_installed and current.sender.id == bot_mri(self.app().app_id):
            await push(target, self.app(), reacted)

    async def join(self, user: UserRecord, conversation: ConversationRecord, target: InboundTarget) -> None:
        if user.user.id in conversation.members:
            raise ValueError(f"{user.person_key} is already in {conversation.id}")
        joined = conversation.model_copy(update={"members": [*conversation.members, user.user.id]})
        self.world.write_conversation(joined, operation=Operation.UPDATE, actor=Actor.PERSON)
        added = self._activity(
            wire.ActivityType.CONVERSATION_UPDATE,
            user,
            joined,
            thread=None,
            membersAdded=[wire.ChannelAccount(id=user.mri, aadObjectId=user.user.id)],
            event_type="teamMemberAdded" if joined.type is wire.ConversationType.CHANNEL else None,
        )
        if joined.bot_installed:
            await push(target, self.app(), added)

    async def install(self, user: UserRecord, conversation: ConversationRecord, target: InboundTarget) -> None:
        """`user` adds the bot to `conversation` (their 1:1 chat, a group chat, or a team): the bot learns the
        conversation's reference from the two activities Teams sends."""
        if conversation.bot_installed:
            raise ValueError(f"the bot is already installed in {conversation.id}")
        installed = conversation.model_copy(update={"bot_installed": True})
        self.world.write_conversation(installed, operation=Operation.UPDATE, actor=Actor.PERSON)
        app = self.app()
        await push(
            target,
            app,
            self._activity(wire.ActivityType.INSTALLATION_UPDATE, user, installed, thread=None, action="add"),
        )
        await push(
            target,
            app,
            self._activity(
                wire.ActivityType.CONVERSATION_UPDATE,
                user,
                installed,
                thread=None,
                membersAdded=[wire.ChannelAccount(id=bot_mri(app.app_id))],
                event_type="teamMemberAdded" if installed.type is wire.ConversationType.CHANNEL else None,
            ),
        )

    # ------------------------------------------------------------------ pressing a card

    async def press(self, reply: PersonReply, target: InboundTarget) -> None:
        if reply.press is None:
            raise ValueError("a press needs `reply.press`: which button, and what the card's inputs hold")
        pressed = reply.press
        user = self.person(reply.person)
        conversation, message = self.located(reply.in_reply_to.external_id)
        found = next(
            (
                (card, action)
                for card in cards.cards_of(message)
                for action in card.actions
                if cards.action_id(action) == pressed.action_id
            ),
            None,
        )
        if found is None:
            raise LookupError(f"the message {message.id} has no button {pressed.action_id!r} ({pressed.label!r})")
        card, action = found
        if action.kind not in (cards.ActionKind.EXECUTE, cards.ActionKind.SUBMIT):
            raise ValueError(f"{pressed.label!r} is a {action.kind.value}, which sends nothing to the bot")
        inputs: dict[str, JsonValue] = {i.id: i.value for i in card.inputs if i.value is not None}
        text_inputs = [i for i in card.inputs if i.type == "Input.Text"]
        for filled in pressed.form:
            if filled.input_id is not None:
                if not any(i.id == filled.input_id for i in card.inputs):
                    raise LookupError(f"the card has no input {filled.input_id!r}")
                inputs[filled.input_id] = filled.value
            elif len(text_inputs) == 1:
                inputs[text_inputs[0].id] = filled.value
            else:
                raise LookupError(f"the card has {len(text_inputs)} text inputs; name the one filled")
        if pressed.picks is not None:
            choice = next((i for i in card.inputs if i.type == "Input.ChoiceSet"), None)
            if choice is None:
                raise LookupError("the card has no choice for a person to be picked in")
            inputs[choice.id] = self.person(pressed.picks).user.id
        data: dict[str, JsonValue] = dict(action.data) if isinstance(action.data, dict) else {}
        data.update(inputs)
        app = self.app()
        if action.kind is cards.ActionKind.EXECUTE:
            sent = self._activity(
                wire.ActivityType.INVOKE,
                user,
                conversation,
                thread=message.replyToId,
                name="adaptiveCard/action",
                value={
                    "action": {"type": action.kind.value, "id": action.id, "verb": action.verb, "data": data},
                    "trigger": "manual",
                },
                replyToId=message.id,
            )
        else:
            sent = self._activity(
                wire.ActivityType.MESSAGE,
                user,
                conversation,
                thread=message.replyToId,
                value=data,
                replyToId=message.id,
            )
        self.world.write(
            action_ref(sent.id),
            ActionRecord(activity=sent, status=0, answer=""),
            operation=Operation.CREATE,
            actor=Actor.PERSON,
            parent=conversation.id,
            after=InteractionSnapshot(
                interaction=InteractionKind.SUBMIT if pressed.form else InteractionKind.PRESS,
                person=reply.person,
                on=reply.in_reply_to,
                action_id=pressed.action_id,
                label=action.title or pressed.label,
                value=pressed.value,
                form=[f.value for f in pressed.form],
            ),
        )
        answered = await push(target, app, sent, url=target.interactivity_url)
        self.world.write(
            action_ref(sent.id),
            ActionRecord(activity=sent, status=answered.status_code, answer=answered.text),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=conversation.id,
        )
        if action.kind is not cards.ActionKind.EXECUTE or not answered.content:
            return
        try:
            answer = wire.parse(wire.InvokeAnswer, answered.content)
        except ValidationError as e:
            raise DeliveryRefused(target.url, answered.status_code, f"an invoke answer that is not one: {e}") from e
        if answer.statusCode != 200:
            raise DeliveryRefused(target.url, answer.statusCode, answered.text)
        if answer.type == wire.ADAPTIVE_CARD:
            attachments = list(message.attachments or [])
            attachments[card.attachment] = attachments[card.attachment].model_copy(update={"content": answer.value})
            redrawn = message.model_copy(update={"attachments": attachments})
            self.world.write(
                message_ref(message.id),
                redrawn,
                operation=Operation.UPDATE,
                actor=Actor.AGENT,
                parent=conversation.id,
                after=snapshot(self.world, conversation, redrawn, None),
            )
