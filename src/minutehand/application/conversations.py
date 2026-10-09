"""People's answers to the agent's messages, through a provider whose service pushes them to the agent and, where
it has one, lands some where the agent reads them: `ports.transitions.ProvidesTransitions` for one agent, bound to
its inbound target and signing secret (`ports.transitions.TalksToAgent`).

Each message the agent sent a person that they can answer where it went is an ask (`domain.transitions`): they
write back, pushed as the service pushes a message (`PushesAnswers.deliver`), or use one of its controls, pushed as
the service pushes an interaction (`PushesPresses.press`). An answer the service lands instead (an email back, a
meeting request's answer: `LandsAnswers`) needs no inbound target. These three are what a messaging provider gives
this port; nothing else calls them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from minutehand.application.refusals import AgentFailed, RunRefused, Unheard
from minutehand.domain.people import InboundTarget, PersonReply, Press
from minutehand.domain.scenario import Person, ProviderKey
from minutehand.domain.transitions import (
    AUTOMATIC_REPLY,
    TEXT,
    Offer,
    Transition,
    Waiting,
    answer_transition,
    answered,
    conversations,
    message_offers,
    transition_change,
)
from minutehand.domain.world import Actor, EntityKind, EntityRef, MessageSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


@runtime_checkable
class PushesAnswers(Protocol):
    """A provider whose service pushes a person's message to the agent: Slack events, Teams activities."""

    async def deliver(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        """Record the reply in the world and push it to the agent the way the real service would, signed with
        `secret`."""
        ...


@runtime_checkable
class PushesPresses(Protocol):
    """A provider whose messages carry controls a person can use (Slack's buttons, Teams' card actions), and whose
    service tells the agent when one is used."""

    async def press(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        """The person uses `reply.press` on `reply.in_reply_to`: recorded as actor PERSON, pushed to `target`'s
        interactivity URL signed with `secret`, the agent's answer applied as the real service applies it, and a form
        the agent opens in answer filled with `reply.press.form` and submitted. A control the message does not carry
        is refused loudly."""
        ...


@runtime_checkable
class LandsAnswers(Protocol):
    """A provider where a person's answer lands where the agent reads it, and nothing is pushed: a reply email in the
    agent's mailbox, an attendee's response on its calendar event. It wakes nobody, unless the service itself tells
    the agent of it (`heard`)."""

    def lands(self, reply: PersonReply, world: Store) -> bool:
        """Whether `reply` lands here rather than being pushed."""
        ...

    def heard(self, reply: PersonReply, world: Store, clock: Clock) -> bool:
        """Whether landing `reply` tells the agent, by a push of the service's own that it asked for."""
        ...

    async def land(self, reply: PersonReply, world: Store, clock: Clock) -> None:
        """Write the person's answer as the real service would, recorded as actor PERSON, and tell whoever `heard`
        names. A message no longer there is left alone."""
        ...


class PushedConversations:
    def __init__(
        self, key: ProviderKey, service: PushesAnswers, target: InboundTarget | None, secret: str | None
    ) -> None:
        self._key = key
        self._service = service
        self._target = target
        self._secret = secret

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        return conversations(person.email, self._key, world.events())

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        del by
        asked = self._asked(item, world)
        if asked is None or who is None or who.email not in asked.recipient_emails:
            return []
        return message_offers(asked)

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        asked = self._asked(item, world)
        if asked is None or who is None:
            raise ValueError(f"the message {item.external_id} is gone: there is nothing to answer")
        reply = answered(item, asked, offer, who.key, content, clock.now())
        lands = self._service if isinstance(self._service, LandsAnswers) else None
        unheard: AgentFailed | None = None
        if lands is not None and lands.lands(reply, world):
            await lands.land(reply, world, clock)
        elif self._target is None or self._secret is None:
            raise RunRefused(f"{who.key} answers on {self._key}, and the agent declares no inbound target there")
        elif reply.press is not None and offer != AUTOMATIC_REPLY:
            if not isinstance(self._service, PushesPresses):
                raise RunRefused(f"{who.key} uses a control on a {self._key} message, and {self._key} carries none")
            await self._service.press(reply, self._target, world, clock, secret=self._secret)
        else:
            try:
                await self._service.deliver(reply, self._target, world, clock, secret=self._secret)
            except AgentFailed as e:
                unheard = e  # the service took their message, and only its push to the agent failed
        moved = answer_transition(self._key, item, offer, by, who.key, content, clock.now())
        recorded = world.apply(transition_change(moved, at_seq=world.head() + 1))
        if unheard is not None:
            raise Unheard(str(unheard), seq=recorded.seq) from unheard
        return moved.model_copy(update={"seq": recorded.seq})

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """An answer pushed always is, to an agent with an inbound target here; one the service lands (an email back,
        a meeting request's answer) when the service tells the agent of it (`LandsAnswers.heard`)."""
        asked = self._asked(item, world)
        lands = self._service if isinstance(self._service, LandsAnswers) else None
        if asked is None or who is None or lands is None:
            return self._target is not None
        probe = answered(item, asked, AUTOMATIC_REPLY, who.key, f'{{"{TEXT}": ""}}', clock.now())
        if asked.actions:
            first = asked.actions[0]
            probe = probe.model_copy(update={"press": Press(action_id=first.action_id, label=first.label)})
        if lands.lands(probe, world):
            return lands.heard(probe, world, clock)
        return self._target is not None

    def _asked(self, item: EntityRef, world: Store) -> MessageSnapshot | None:
        if item.provider != self._key or item.kind is not EntityKind.MESSAGE:
            raise ValueError(f"{item.provider} {item.kind.value} {item.external_id} is no {self._key} message")
        found = next((e.after for e in reversed(world.events()) if e.entity == item and e.after is not None), None)
        return found if isinstance(found, MessageSnapshot) else None
