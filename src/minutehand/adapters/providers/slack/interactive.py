"""A person using what the agent put in front of them: a button or a person picker on a message, the modal the agent
opens in answer, a slash command. Pushed to the agent's interactivity URL as Slack pushes it: form-encoded, the
JSON in `payload`, signed like an event, and never retried.

A press hands the agent a `trigger_id` (open one view with it, once, within three seconds) and a `response_url`
(five posts within thirty minutes: a new message, the pressed one replaced, or deleted; `app.SlackApi.response_url`).
When the person has a form to fill, the modal the agent opened with that trigger is filled and submitted, and what
the agent answers is applied: nothing closes it, `clear` closes it, `update` and `push` show the next view, and
`errors` leaves it open with the errors on it.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from pydantic import ValidationError

from minutehand.adapters.providers.slack import inbound, state, wire
from minutehand.adapters.providers.slack.app import message_actions, write_view
from minutehand.adapters.providers.slack.inbound import DeliveryRefused
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.application.refusals import AgentFailed
from minutehand.domain.people import InboundTarget, PersonReply, Press
from minutehand.domain.scenario import FormInput, PersonCommands
from minutehand.domain.world import (
    Actor,
    EntityKind,
    InteractionKind,
    InteractionSnapshot,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

FORM_WAIT = 3.0
"""Real seconds a press waits for the agent to open the form the person means to fill: a trigger's lifetime."""
_POLL = 0.05


class FormNeverOpened(AgentFailed):
    """The person pressed something meaning to fill in the form it opens, and the agent opened none in time."""


def _url(target: InboundTarget) -> str:
    return target.interactivity_url or target.url


def _person(slack: SlackWorld, author: str) -> wire.PayloadUser:
    user = slack.user(author)
    if user is None:
        raise LookupError(f"{author} is not a member of the workspace")
    return wire.PayloadUser(id=user.id, username=user.name, name=user.name, team_id=slack.team.id)


def _team(slack: SlackWorld) -> wire.PayloadTeam:
    return wire.PayloadTeam(id=slack.team.id, domain=slack.team.domain)


def _channel(channel: wire.SlackChannel) -> wire.PayloadChannel:
    return wire.PayloadChannel(id=channel.id, name=channel.name or ("directmessage" if channel.is_im else "mpdm"))


def mint_trigger(slack: SlackWorld, clock: Clock, author: str) -> wire.SlackTrigger:
    """A `trigger_id`, stored so the agent's use of it is checked."""
    now = int(clock.now().timestamp())
    trigger = wire.SlackTrigger(id=state.minted("trigger", slack.next_seq(), now), user=author, issued=now)
    slack.write(
        state.trigger_ref(trigger.id), trigger, operation=Operation.CREATE, actor=Actor.PERSON, parent=state.TRIGGERS
    )
    return trigger


def mint(
    slack: SlackWorld, clock: Clock, author: str, channel: str, message: str | None
) -> tuple[wire.SlackTrigger, wire.SlackHook]:
    """A `trigger_id` and a `response_url` for one press or command, stored so the agent's use of each is checked."""
    trigger = mint_trigger(slack, clock, author)
    now = int(clock.now().timestamp())
    seq = slack.next_seq()
    hook = wire.SlackHook(
        id=f"{seq}",
        secret=state.minted("hook", seq, now).rsplit(".", 1)[-1],
        user=author,
        channel=channel,
        message=message,
        issued=now,
    )
    slack.write(state.hook_ref(hook.id), hook, operation=Operation.CREATE, actor=Actor.PERSON, parent=state.HOOKS)
    return trigger, hook


async def _send(url: str, body: bytes, secret: str, what: str) -> httpx.Response:
    try:
        answered = await inbound.post_signed(url, body, "application/x-www-form-urlencoded", secret)
    except httpx.HTTPError as e:
        raise DeliveryRefused(url, None, repr(e), what=what) from e
    if not answered.is_success:
        raise DeliveryRefused(url, answered.status_code, answered.text, what=what)
    return answered


# --------------------------------------------------------------------------- a press


async def press(reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str) -> None:
    inbound.refuse_foreign(target)
    pressed = reply.press
    if pressed is None:
        raise ValueError(f"{reply.person}'s reply presses nothing; it is delivered as a message")
    if reply.in_reply_to.provider != MANIFEST.key or reply.in_reply_to.kind is not EntityKind.MESSAGE:
        raise ValueError(f"{reply.person} presses on {reply.in_reply_to}, which is not a Slack message")
    found = SlackWorld(world).located(reply.in_reply_to.external_id)
    if found is None:
        raise LookupError(f"no Slack message {reply.in_reply_to.external_id} for {reply.person} to press on")
    channel_id, message = found
    slack = inbound.where(world, channel_id)
    channel = slack.channel(channel_id)
    author = state.user_id(reply.person, slack.team.id)
    shown = message.ephemeral_to == author if message.ephemeral_to is not None else slack.is_member(channel_id, author)
    if channel is None or not shown:
        raise LookupError(f"{reply.person} was never shown the message {message.ts} they press on")
    control = next(
        (
            c
            for c in wire.controls(message.blocks)
            if c.action_id == pressed.action_id and (pressed.value is None or c.value == pressed.value)
        ),
        None,
    )
    if control is None:
        offered = [a.action_id for a in message_actions(message)]
        raise LookupError(f"the message {message.ts} has no control {pressed.action_id!r} to press; it has {offered}")
    picked = state.user_id(pressed.picks, slack.team.id) if pressed.picks is not None else None
    if control.type == "users_select" and picked is None:
        raise ValueError(f"{reply.person} uses the person picker {control.label!r} and picks nobody")

    trigger, hook = mint(slack, clock, author, channel_id, message.ts)
    slack.write(
        state.interaction_ref(trigger.id),
        trigger,
        operation=Operation.CREATE,
        actor=Actor.PERSON,
        parent=state.INTERACTIONS,
        after=InteractionSnapshot(
            interaction=InteractionKind.PRESS,
            person=reply.person,
            on=reply.in_reply_to,
            action_id=control.action_id,
            label=control.label,
            value=picked if control.type == "users_select" else control.value,
        ),
    )
    payload = wire.BlockActions(
        user=_person(slack, author),
        api_app_id=slack.team.app_id,
        container=wire.MessageContainer(
            message_ts=message.ts,
            channel_id=channel_id,
            is_ephemeral=message.ephemeral_to is not None,
            thread_ts=message.thread_ts,
        ),
        trigger_id=trigger.id,
        team=_team(slack),
        channel=_channel(channel),
        message=message.model_copy(update={"ephemeral_to": None}) if message.ephemeral_to is None else None,
        response_url=state.response_url(hook.id, hook.secret, command=False, team=slack.team.id),
        actions=[
            wire.PressedAction(
                type=control.type,
                action_id=control.action_id,
                block_id=control.block_id,
                action_ts=slack.next_ts(clock),
                text=None if control.label_free else wire.TextObject(text=control.label, emoji=True),
                value=control.value,
                style=control.style,
                selected_user=picked,
                initial_user=control.initial_user,
            )
        ],
    )
    await _send(_url(target), wire.payload_form(payload), secret, "a block_actions payload")
    if pressed.form:
        await _fill(slack, world, pressed, reply.person, author, trigger.id, reply, target, clock, secret)


async def _fill(
    slack: SlackWorld,
    world: Store,
    pressed: Press,
    person: str,
    author: str,
    trigger: str,
    reply: PersonReply,
    target: InboundTarget,
    clock: Clock,
    secret: str,
) -> None:
    """Wait for the modal the agent opens with the press's trigger, fill it with the person's form, submit it."""
    deadline = time.monotonic() + FORM_WAIT  # clock-lint: exempt a trigger lives three real seconds, as in Slack
    opened: wire.OpenView | None = None
    while opened is None:
        used = slack.body(state.trigger_ref(trigger), wire.SlackTrigger)
        if used is not None and used.view is not None:
            opened = slack.body(state.view_ref(used.view), wire.OpenView)
            break
        if time.monotonic() > deadline:  # clock-lint: exempt as above
            raise FormNeverOpened(
                f"{person} pressed {pressed.label!r} to fill in a form, and the agent opened none with the press's "
                f"trigger_id within {FORM_WAIT:g} s"
            )
        await asyncio.sleep(_POLL)
    if opened is None or not opened.open:
        raise FormNeverOpened(f"the form the agent opened for {person}'s press of {pressed.label!r} is closed")
    await submit(slack, world, opened, pressed.form, person, author, reply, target, clock, secret)


def _filled(view: wire.SlackView, form: list[FormInput], person: str) -> tuple[wire.ViewState, list[str]]:
    fields = wire.inputs(view.blocks)
    texts = [f for f in fields if f.type == "plain_text_input"]
    given: dict[str, str] = {}
    for entry in form:
        if entry.input_id is None:
            if len(texts) != 1:
                raise ValueError(
                    f"{person} types into the form's only text field, and it has {len(texts)}: "
                    f"{[f.action_id for f in texts]}"
                )
            given[texts[0].action_id] = entry.value
            continue
        field = next((f for f in fields if entry.input_id in (f.action_id, f.block_id)), None)
        if field is None or field.type != "plain_text_input":
            raise ValueError(f"the form has no text field {entry.input_id!r}; it has {[f.action_id for f in fields]}")
        given[field.action_id] = entry.value
    missing = [f.label or f.action_id for f in fields if not f.optional and f.action_id not in given]
    if missing:
        raise ValueError(f"{person} submits the form leaving required fields empty: {missing}")
    values = {
        f.block_id: {
            f.action_id: wire.ViewInputValue(type=f.type, value=given[f.action_id] if f.action_id in given else None)
        }
        for f in fields
    }
    return wire.ViewState(values=values), [given[f.action_id] for f in fields if f.action_id in given]


async def submit(
    slack: SlackWorld,
    world: Store,
    opened: wire.OpenView,
    form: list[FormInput],
    person: str,
    author: str,
    reply: PersonReply,
    target: InboundTarget,
    clock: Clock,
    secret: str,
) -> None:
    view_state, typed = _filled(opened.view, form, person)
    view = opened.view.model_copy(update={"state": view_state})
    trigger = mint_trigger(slack, clock, author)
    title = view.title.text if view.title is not None else view.callback_id
    slack.write(
        state.interaction_ref(trigger.id),
        trigger,
        operation=Operation.CREATE,
        actor=Actor.PERSON,
        parent=state.INTERACTIONS,
        after=InteractionSnapshot(
            interaction=InteractionKind.SUBMIT,
            person=person,
            on=reply.in_reply_to,
            action_id=view.callback_id,
            label=title,
            form=typed,
        ),
    )
    payload = wire.ViewSubmission(
        team=_team(slack), user=_person(slack, author), api_app_id=slack.team.app_id, trigger_id=trigger.id, view=view
    )
    url = _url(target)
    answered = await _send(url, wire.payload_form(payload), secret, "a view_submission payload")
    try:
        answer = wire.view_answer(answered.content)
    except (ValueError, ValidationError) as e:
        raise DeliveryRefused(url, answered.status_code, answered.text, what="a view_submission payload") from e
    shown = opened.model_copy(update={"view": view})
    if answer is None or answer.response_action == "clear":  # enum-lint: exempt Slack's own response_action
        _close(slack, shown)
    elif answer.response_action == "errors":  # enum-lint: exempt Slack's own response_action
        write_view(slack, shown.model_copy(update={"errors": answer.errors}), Operation.UPDATE, Actor.AGENT)
    else:
        if answer.view is None:
            raise DeliveryRefused(url, answered.status_code, answered.text, what="a view_submission (no view)")
        _next_view(slack, shown, answer)


def _close(slack: SlackWorld, shown: wire.OpenView) -> None:
    write_view(slack, shown.model_copy(update={"open": False}), Operation.UPDATE, Actor.AGENT)


def _next_view(slack: SlackWorld, shown: wire.OpenView, answer: wire.ViewAnswer) -> None:
    """`update` replaces the submitted view; `push` stacks a new one on it."""
    assert answer.view is not None
    wire.check_view(answer.view)
    pushing = answer.response_action == "push"  # enum-lint: exempt Slack's own response_action
    view_id = state.view_id(slack.next_seq()) if pushing else shown.view.id
    version = 0 if pushing else int(shown.view.hash.split(".", 1)[0]) + 1
    spec = answer.view
    view = wire.SlackView(
        id=view_id,
        team_id=slack.team.id,
        type=spec.type,
        title=spec.title,
        submit=spec.submit,
        close=spec.close,
        blocks=wire.with_ids(spec.blocks, view_id) or [],
        private_metadata=spec.private_metadata,
        callback_id=spec.callback_id,
        external_id=spec.external_id,
        state=wire.ViewState(),
        hash=state.view_hash(view_id, version),
        clear_on_close=spec.clear_on_close,
        notify_on_close=spec.notify_on_close,
        previous_view_id=shown.view.id if pushing else shown.view.previous_view_id,
        root_view_id=shown.view.root_view_id,
        app_id=slack.team.app_id,
        app_installed_team_id=slack.team.id,
        bot_id=slack.team.bot_id,
    )
    write_view(
        slack,
        wire.OpenView(view=view, user=shown.user, trigger_id=shown.trigger_id),
        Operation.CREATE if pushing else Operation.UPDATE,
        Actor.AGENT,
    )


# --------------------------------------------------------------------------- a slash command


async def command(happening: PersonCommands, target: InboundTarget, world: Store, clock: Clock, *, secret: str) -> None:
    """The person runs one of the agent's slash commands: form fields to the agent's URL, and what it answers at
    once shown to them alone, or to the channel when it says `in_channel`."""
    inbound.refuse_foreign(target)
    slack = inbound.acting(world, happening.person, happening.channel)
    author = state.user_id(happening.person, slack.team.id)
    channel = inbound.conversation(slack, happening.channel, author)
    trigger, hook = mint(slack, clock, author, channel.id, None)
    slack.write(
        state.command_ref(trigger.id),
        trigger,
        operation=Operation.CREATE,
        actor=Actor.PERSON,
        parent=state.COMMANDS,
        after=RecordSnapshot(resource="slash_commands", text=f"{happening.command} {happening.text}".strip()),
    )
    body = wire.slash_command_form(
        team=_team(slack),
        channel=_channel(channel),
        user=_person(slack, author),
        command=happening.command,
        text=happening.text,
        api_app_id=slack.team.app_id,
        response_url=state.response_url(hook.id, hook.secret, command=True, team=slack.team.id),
        trigger_id=trigger.id,
    )
    answered = await _send(target.url, body, secret, f"the slash command {happening.command}")
    if not answered.content.strip():
        return
    try:
        answer = wire.CommandAnswer.model_validate_json(answered.content)
    except ValidationError:
        answer = wire.CommandAnswer(text=answered.text)
    if not answer.text and not answer.blocks:
        return
    ts = slack.next_ts(clock)
    in_channel = answer.response_type == "in_channel"  # enum-lint: exempt Slack's own response_type
    user = slack.user(author)
    assert user is not None
    message = wire.SlackMessage(
        ts=ts,
        user=slack.bot,
        text=answer.text,
        team=slack.team.id,
        bot_id=slack.team.bot_id,
        app_id=slack.team.app_id,
        blocks=wire.with_ids(answer.blocks, ts),
        bot_profile=state.bot_profile(int(clock.now().timestamp()), slack.team),
        ephemeral_to=None if in_channel else author,
    )
    seen = (
        slack.human_emails(channel.id, besides=slack.bot)
        if in_channel
        else ([user.profile.email] if user.profile.email else [])
    )
    slack.write(
        state.message_ref(ts),
        message,
        operation=Operation.CREATE,
        actor=Actor.AGENT,
        parent=channel.id,
        after=MessageSnapshot(
            text=wire.visible_text(message.text, message.blocks),
            channel=channel.id,
            recipient_emails=seen,
            actions=message_actions(message),
        ),
    )
