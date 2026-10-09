"""What tests do to a ticket provider's tickets, the one way anything is done to them: a transition through its
`apply` (`ports.transitions`), as the run loop and a standing world do it."""

from __future__ import annotations

from minutehand.application.people import happen, move
from minutehand.domain.scenario import Scenario, TicketHappening, TicketState
from minutehand.domain.transitions import ASSIGNEE, REASSIGN, Transition
from minutehand.domain.world import Actor, EntityRef, TicketSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from minutehand.ports.transitions import ProvidesTransitions


async def assignee_moves(
    provider: ProvidesTransitions, ticket: EntityRef, to: TicketState, scenario: Scenario, world: Store, clock: Clock
) -> Transition:
    """The ticket's assignee moves it to a state that means `to`; refused when it has none."""
    stood = next(
        (e.after for e in reversed(world.events()) if e.entity == ticket and isinstance(e.after, TicketSnapshot)), None
    )
    assignee = next((p for p in scenario.people if stood is not None and p.email == stood.assignee_email), None)
    if assignee is None:
        raise ValueError(f"{ticket.external_id} has no assignee to move it")
    return await move(provider, ticket, to.value, Actor.PERSON, assignee, {}, world, clock)


async def edited(
    provider: ProvidesTransitions,
    ticket: EntityRef,
    *,
    state: TicketState | None,
    assignee_email: str | None,
    world: Store,
    clock: Clock,
) -> None:
    """The scenario sets the ticket's state past any workflow, and hands it to `assignee_email`."""
    if state is not None:
        await move(provider, ticket, state.value, Actor.SCENARIO, None, {}, world, clock)
    if assignee_email is not None:
        await move(provider, ticket, REASSIGN, Actor.SCENARIO, None, {ASSIGNEE: assignee_email}, world, clock)


async def acted(
    provider: ProvidesTransitions, happening: TicketHappening, scenario: Scenario, world: Store, clock: Clock
) -> Transition | None:
    """The happening's person does its action to the seeded ticket now."""
    return await happen(scenario, provider, happening, world, clock)
