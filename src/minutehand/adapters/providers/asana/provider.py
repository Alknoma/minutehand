"""The Asana provider: the REST API, the seeded workspace, and tickets people finish."""

from __future__ import annotations

from minutehand.adapters.providers.asana.app import build_app
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.seed import seed
from minutehand.adapters.providers.asana.state import AsanaWorld
from minutehand.adapters.providers.asana import wire
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario, TicketState
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class AsanaProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None:
        """The assignee ticks the task done, or moves it to Cancelled and closes it."""
        asana = AsanaWorld(world)
        task = _task(asana, ticket)
        asana.put_task(asana.moved(task, to, now=clock.now()), operation=Operation.UPDATE, actor=Actor.PERSON)

    def edit(
        self, ticket: EntityRef, *, state: TicketState | None, assignee_email: str | None, world: Store, clock: Clock
    ) -> None:
        asana = AsanaWorld(world)
        task = _task(asana, ticket)
        if state is not None:
            task = asana.moved(task, state, now=clock.now())
        if assignee_email is not None:
            user = asana.user_by_email(assignee_email)
            if user is None:
                raise LookupError(f"no asana user has the email {assignee_email}")
            task = task.model_copy(update={"assignee": user.gid, "modified_at": wire.stamp(clock.now())})
        asana.put_task(task, operation=Operation.UPDATE, actor=Actor.SCENARIO)


def _task(asana: AsanaWorld, ticket: EntityRef) -> wire.AsanaTask:
    if ticket.provider != MANIFEST.key or ticket.kind is not EntityKind.TICKET:
        raise ValueError(f"{ticket} is not an asana task")
    task = asana.task(ticket.external_id)
    if task is None:
        raise LookupError(f"no asana task {ticket.external_id} in the world: it was deleted or never created")
    return task


def build() -> AsanaProvider:
    """A `Provider` that also `HoldsTickets` and `EditsTickets`; the tests hold it to all three."""
    return AsanaProvider()
