"""The Asana provider: the REST API, the seeded workspace, and tickets people finish, move and talk about."""

from __future__ import annotations

from minutehand.adapters.providers.asana import state, wire
from minutehand.adapters.providers.asana.app import build_app
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.seed import AsanaSeed, limited, seed, seeded_gid
from minutehand.adapters.providers.asana.state import AsanaWorld
from minutehand.domain.errors import Rendered
from minutehand.domain.provider import Manifest, PersonChange, fault_fragment
from minutehand.domain.scenario import Person, Scenario, SeededTicket, TicketState
from minutehand.domain.transitions import (
    ASSIGNEE,
    COMMENT,
    DELETE,
    DELETED,
    REASSIGN,
    Offer,
    OfferField,
    Transition,
    Waiting,
    content_of,
    item_parent,
    ticket_acts,
)
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation, TransitionSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store
from minutehand.ports.transitions import record


class AsanaProvider:
    manifest: Manifest = MANIFEST
    seed_model = AsanaSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def error(self, status: int, code: str, message: str) -> Rendered:
        del code
        return wire.error_answer(status, message)

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

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """An administrator removes the person from the workspace: no longer listed or a team's member, refused as
        an assignee or a follower; what they did before still names them. A token naming them still acts as them:
        Minutehand does not enforce credentials."""
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

    # -- transitions (`ProvidesTransitions`) ---------------------------------------------------------------------

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        """Every task assigned to the person that is neither done nor cancelled, as they read it in Asana."""
        asana = AsanaWorld(world)
        user = asana.user_by_email(person.email)
        if user is None:
            return []
        return [
            Waiting(item=state.task_ref(t.gid), state=asana.words(t), shown=_shown(asana, t))
            for t in asana.tasks()
            if t.assignee == user.gid and asana.state_of(t) is TicketState.OPEN
        ]

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        """What a person can make the task say as its status source reads it: each place (the completed box, a
        section, the status field's option) that means another of open, done or cancelled, as a person leaves it
        when they move it there, each with a comment and what it means. Then, only for what the scenario has someone
        do: a comment, a reassignment, the task's deletion."""
        del by, who
        asana = AsanaWorld(world)
        task = _task(asana, item)
        moves: list[Offer] = []
        for to in TicketState:
            words = _words_if_moved(asana, task, to) if to is not asana.state_of(task) else None
            if words is None or words == asana.words(task) or any(o.name == words for o in moves):
                continue
            moves.append(
                Offer(
                    name=words,
                    to_state=words,
                    means=to,
                    fields=[OfferField(name=COMMENT, description="A comment added to the task as it moves")],
                )
            )
        return [*moves, *ticket_acts(asana.words(task))]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The person moves the task as Asana's own status source reads it (`AsanaWorld.moved`), with their comment as
        a story; or comments, reassigns or deletes it as the API does; as themselves. The scenario moves it as no one."""
        asana = AsanaWorld(world)
        task = _task(asana, item)
        found = next((o for o in self.legal(item, by, who, world) if o.name == offer), None)
        if found is None:
            raise ValueError(f"asana task {task.gid} offers no move to {offer!r}")
        given = content_of(content, found, who.key if who is not None else by.value)
        if who is None and by is not Actor.SCENARIO:
            raise ValueError("an asana task is changed by a person: name them")
        before = asana.words(task)
        now = wire.stamp(clock.now())
        if offer == COMMENT:
            if who is None:
                raise ValueError(f"a comment on asana task {task.gid} is written by a person: name them")
            self._story(asana, task, given[COMMENT], who, by, clock)
            return _recorded(world, item, offer, before, before, by, who, content, clock)
        if offer == REASSIGN:
            address = given[ASSIGNEE].strip() if ASSIGNEE in given else ""
            user = asana.user_by_email(address) if address else None
            if address and user is None:
                raise ValueError(f"no asana user has the email {address}")
            changed = task.model_copy(update={"assignee": user.gid if user is not None else None, "modified_at": now})
            asana.put_task(changed, operation=Operation.UPDATE, actor=by)
            return _recorded(world, item, offer, before, before, by, who, content, clock)
        if offer == DELETE:
            asana.delete_task(task, actor=by)
            return _recorded(world, item, offer, before, DELETED, by, who, content, clock)
        assert found.means is not None
        moved = asana.moved(task, found.means, now=clock.now())
        asana.put_task(
            moved, operation=Operation.UPDATE, actor=by, who=who.key if who is not None else None, content=content
        )
        if COMMENT in given and given[COMMENT].strip() and who is not None:
            self._story(asana, task, given[COMMENT], who, by, clock)
        moves = world.children(MANIFEST.key, EntityKind.TRANSITION, item_parent(item), limit=1000)
        last = max(moves, key=lambda s: s.seq)
        event = next(e for e in world.events(since=last.seq - 1) if e.seq == last.seq)
        assert isinstance(event.after, TransitionSnapshot)
        return Transition.of(event)

    def _story(self, asana: AsanaWorld, task: wire.AsanaTask, text: str, who: Person, by: Actor, clock: Clock) -> None:
        asana.put_story(
            wire.AsanaStory(
                gid=asana.next_gid(),
                text=text,
                task=task.gid,
                created_by=state.user_gid(who.key),
                created_at=wire.stamp(clock.now()),
            ),
            actor=by,
        )

    def seeded(self, scenario: Scenario, ticket: SeededTicket, world: Store) -> EntityRef | None:
        """The task seeded from `ticket`, while it is there."""
        task = AsanaWorld(world).task(seeded_gid(scenario, ticket))
        return state.task_ref(task.gid) if task is not None else None

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """Never: Asana pushes nothing to the agent here; it finds a person's move on its next read."""
        del item, who, world, clock
        return False


def _recorded(
    world: Store,
    item: EntityRef,
    offer: str,
    before: str,
    after: str,
    by: Actor,
    who: Person | None,
    content: str,
    clock: Clock,
) -> Transition:
    """A comment, a reassignment or a deletion, recorded once as the move it is."""
    return record(
        world,
        Transition(
            provider=MANIFEST.key,
            item=item,
            name=offer,
            from_state=before,
            to_state=after,
            by=by,
            who=who.key if who is not None else None,
            content=content,
            at=clock.now(),
        ),
    )


def _words_if_moved(asana: AsanaWorld, task: wire.AsanaTask, to: TicketState) -> str | None:
    """What the task would say moved to `to` as a person moves it; None when the status source cannot say `to`."""
    moved_at = wire.parse_stamp(task.modified_at)
    assert moved_at is not None
    try:
        return asana.words(asana.moved(task, to, now=moved_at))
    except state.StateUnexpressible:
        return None


def _shown(asana: AsanaWorld, task: wire.AsanaTask) -> str:
    """The task as its assignee reads it: its name, status, notes and stories, oldest first."""
    lines = [task.name, f"Status: {asana.words(task)}"]
    if task.notes:
        lines += ["", task.notes]
    for story in asana.stories(task.gid):
        author = asana.user(story.created_by)
        lines.append(f"{author.name if author else 'Someone'}: {story.text}")
    return "\n".join(lines)


def _task(asana: AsanaWorld, ticket: EntityRef) -> wire.AsanaTask:
    if ticket.provider != MANIFEST.key or ticket.kind is not EntityKind.TICKET:
        raise ValueError(f"{ticket} is not an asana task")
    task = asana.task(ticket.external_id)
    if task is None:
        raise LookupError(f"no asana task {ticket.external_id} in the world: it was deleted or never created")
    return task


def build() -> AsanaProvider:
    """A `Provider` that also `ProvidesTransitions` and `HoldsSeeded`; the tests hold it to all four."""
    return AsanaProvider()
