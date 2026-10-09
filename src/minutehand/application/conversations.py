"""People's answers to the agent's messages, through a provider whose service pushes them to the agent and, where
it has one, lands some where the agent reads them: `ports.transitions.ProvidesTransitions` for one agent, bound to
its inbound target and signing secret (`ports.transitions.TalksToAgent`).

Each message the agent sent a person that they can answer where it went is an ask (`domain.transitions`): they
write back, pushed as the service pushes a message (`PushesEvents.deliver`), or use one of its controls, pushed as
the service pushes an interaction (`PushesInteractions.press`). An answer the service lands instead (an email
back, a meeting request's answer: `LandsReplies`) needs no inbound target.
"""

from __future__ import annotations

from minutehand.application.refusals import RunRefused
from minutehand.domain.people import InboundTarget, Press
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
from minutehand.ports.provider import LandsReplies, PushesEvents, PushesInteractions
from minutehand.ports.store import Store


class PushedConversations:
    def __init__(
        self, key: ProviderKey, service: PushesEvents, target: InboundTarget | None, secret: str | None
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
        lands = self._service if isinstance(self._service, LandsReplies) else None
        if lands is not None and lands.lands(reply, world):
            await lands.land(reply, world, clock)
        elif self._target is None or self._secret is None:
            raise RunRefused(f"{who.key} answers on {self._key}, and the agent declares no inbound target there")
        elif reply.press is not None and offer != AUTOMATIC_REPLY:
            if not isinstance(self._service, PushesInteractions):
                raise RunRefused(f"{who.key} uses a control on a {self._key} message, and {self._key} carries none")
            await self._service.press(reply, self._target, world, clock, secret=self._secret)
        else:
            await self._service.deliver(reply, self._target, world, clock, secret=self._secret)
        moved = answer_transition(self._key, item, offer, by, who.key, content, clock.now())
        recorded = world.apply(transition_change(moved, at_seq=world.head() + 1))
        return moved.model_copy(update={"seq": recorded.seq})

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """An answer pushed always is, to an agent with an inbound target here; one the service lands (an email back,
        a meeting request's answer) when the service tells the agent of it (`LandsReplies.heard`)."""
        asked = self._asked(item, world)
        lands = self._service if isinstance(self._service, LandsReplies) else None
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
