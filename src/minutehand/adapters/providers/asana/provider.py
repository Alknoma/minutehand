"""The Asana provider: the REST API, the seeded workspace, and tickets people finish, move and talk about."""

from __future__ import annotations

from minutehand.adapters.providers.asana import state, wire
from minutehand.adapters.providers.asana.app import build_app
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.seed import AsanaSeed, limited, seed, seeded_gid
from minutehand.adapters.providers.asana.state import AsanaWorld
from minutehand.domain.provider import Manifest, PersonChange, fault_fragment
from minutehand.domain.scenario import (
    Comments,
    Deletes,
    Moves,
    Person,
    Reassigns,
    Scenario,
    TicketHappening,
    TicketState,
)
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class AsanaProvider:
    manifest: Manifest = MANIFEST
    seed_model = AsanaSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`AsanaSeed.rate_limits`, on a world already open, each stretch counted from now; and `.limits`, the
        workspace's plan, kind and threshold for a read without `limit`, from now on."""
        fragment = fault_fragment(AsanaSeed, faults, frozenset({"rate_limits", "limits"}))
        limits = fragment.rate_limits
        asana = AsanaWorld(world)
        workspace = asana.workspace(state.WORKSPACE_GID)
        if workspace is None:
            raise ValueError("this world holds no Asana workspace to declare faults on")
        now = clock.now()
        windows = [
            wire.RateWindow(start=wire.stamp(now + r.after), end=wire.stamp(now + r.after + r.lasts)) for r in limits
        ]
        asana.put_record(
            limited(workspace.model_copy(update={"rate_limits": [*workspace.rate_limits, *windows]}), fragment.limits),
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

    def delete_ticket(self, ticket: EntityRef, world: Store, clock: Clock) -> None:
        """The task is deleted, with its subtasks, as its assignee (or the owner) deletes it in Asana."""
        asana = AsanaWorld(world)
        asana.delete_task(_task(asana, ticket), actor=Actor.PERSON)
        del clock

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """An administrator removes the person from the workspace: no longer listed or a team's member, refused as
        an assignee or a follower, their tokens refused; what they did before still names them."""
        if change is not PersonChange.REMOVED:
            raise ValueError(f"asana has no way to show a person {change.value}")
        asana = AsanaWorld(world)
        user = asana.user(state.user_gid(person.key))
        if user is None or user.removed:
            raise ValueError(f"{person.key} is not a member of the asana workspace")
        asana.put_record(
            user.model_copy(update={"removed": True}),
            parent=state.USERS,
            actor=Actor.SCENARIO,
            operation=Operation.UPDATE,
        )
        for team in asana.teams():
            if user.gid in team.members:
                asana.put_record(
                    team.model_copy(update={"members": [m for m in team.members if m != user.gid]}),
                    parent=state.TEAMS,
                    actor=Actor.SCENARIO,
                    operation=Operation.UPDATE,
                )
        del clock

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
