"""The Asana provider: the REST API, the seeded workspace, and tickets people finish, move and talk about."""

from __future__ import annotations

from minutehand.adapters.providers.asana import state, wire
from minutehand.adapters.providers.asana.app import build_app
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.seed import AsanaSeed, seed, seeded_gid
from minutehand.adapters.providers.asana.state import AsanaWorld
from minutehand.domain.provider import Manifest, fault_fragment
from minutehand.domain.scenario import Comments, Deletes, Moves, Reassigns, Scenario, TicketHappening, TicketState
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

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`AsanaSeed.rate_limits`, on a world already open: each stretch counted from now."""
        limits = fault_fragment(AsanaSeed, faults, frozenset({"rate_limits"})).rate_limits
        asana = AsanaWorld(world)
        workspace = asana.workspace(state.WORKSPACE_GID)
        if workspace is None:
            raise ValueError("this world holds no Asana workspace to declare faults on")
        now = clock.now()
        windows = [
            wire.RateWindow(start=wire.stamp(now + r.after), end=wire.stamp(now + r.after + r.lasts)) for r in limits
        ]
        asana.put_record(
            workspace.model_copy(update={"rate_limits": [*workspace.rate_limits, *windows]}),
            parent=state.WORKSPACES,
            actor=Actor.SCENARIO,
            operation=Operation.UPDATE,
        )

    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None:
        """The assignee moves the task to `to` the way the workspace's status source says it: ticks it done,
        or moves it to the section, or sets the status field, that means `to`."""
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

    def act(self, happening: TicketHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        """The person does what the happening says to the seeded task, as themself. A task no longer there
        (the agent deleted it) is left alone: there is nothing for them to act on, and nothing is written."""
        seeded = scenario.happening_ticket(happening)
        asana = AsanaWorld(world)
        task = asana.task(seeded_gid(scenario, seeded))
        if task is None:
            return
        person = state.user_gid(happening.person)
        now = wire.stamp(clock.now())
        match happening.action:
            case Moves():
                asana.put_task(
                    asana.moved(task, happening.action.to, now=clock.now()),
                    operation=Operation.UPDATE,
                    actor=Actor.PERSON,
                )
            case Reassigns():
                to = state.user_gid(happening.action.to) if happening.action.to is not None else None
                asana.put_task(
                    task.model_copy(update={"assignee": to, "modified_at": now}),
                    operation=Operation.UPDATE,
                    actor=Actor.PERSON,
                )
            case Comments():
                asana.put_story(
                    wire.AsanaStory(
                        gid=asana.next_gid(),
                        text=happening.action.text,
                        task=task.gid,
                        created_by=person,
                        created_at=now,
                    ),
                    actor=Actor.PERSON,
                )
            case Deletes():
                asana.delete_task(task, actor=Actor.PERSON)


def _task(asana: AsanaWorld, ticket: EntityRef) -> wire.AsanaTask:
    if ticket.provider != MANIFEST.key or ticket.kind is not EntityKind.TICKET:
        raise ValueError(f"{ticket} is not an asana task")
    task = asana.task(ticket.external_id)
    if task is None:
        raise LookupError(f"no asana task {ticket.external_id} in the world: it was deleted or never created")
    return task


def build() -> AsanaProvider:
    """A `Provider` that also `HoldsTickets`, `EditsTickets` and `ActsOnTickets`; the tests hold it to all four."""
    return AsanaProvider()
