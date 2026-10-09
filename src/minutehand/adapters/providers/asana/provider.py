"""The Asana provider: the REST API, the seeded workspace, and tickets people finish, move and talk about."""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from minutehand.adapters.providers.asana import state, webhooks, wire
from minutehand.adapters.providers.asana.app import build_app
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.seed import AsanaSeed, limited, seed, seeded_gid
from minutehand.adapters.providers.asana.state import AsanaWorld
from minutehand.domain.errors import Rendered
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
from minutehand.domain.transitions import Offer, OfferField, Transition, Waiting, item_parent
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation, TransitionSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


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
        when they move it there, each with a comment. An approval task is decided instead: `approved`, `rejected`
        or `changes_requested`, whichever it is not now, each with a comment."""
        del by, who
        asana = AsanaWorld(world)
        task = _task(asana, item)
        words = (
            [d.value for d in DECISIONS if d is not task.approval_status]
            if task.resource_subtype is wire.TaskSubtype.APPROVAL
            else _reachable(asana, task)
        )
        return [
            Offer(
                name=w,
                to_state=w,
                fields=[OfferField(name=COMMENT, description="A comment added to the task as it moves")],
            )
            for w in words
        ]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The person moves the task as a fate or a happening moves it (`AsanaWorld.moved`), or decides an approval
        task (`approved`, `rejected`, `changes_requested`: `completed` and `approval_status` set together, as the
        reference says they are kept in step), with their comment as a story, as themselves; the webhooks that hear of
        it are sent what they are owed."""
        asana = AsanaWorld(world)
        task = _task(asana, item)
        if who is None:
            raise ValueError("an asana task is moved by a person: name them")
        try:
            given = _CONTENT.validate_json(content)
        except ValidationError as e:
            raise ValueError(f"a transition's content is a JSON object of text fields: {content!r}") from e
        unknown = sorted(set(given) - {COMMENT})
        if unknown:
            raise ValueError(f"an asana move takes a comment, not {', '.join(unknown)}")
        person = state.user_gid(who.key)
        if task.resource_subtype is wire.TaskSubtype.APPROVAL:
            decision = next((d for d in DECISIONS if d.value == offer and d is not task.approval_status), None)
            if decision is None:
                raise ValueError(f"asana approval {task.gid} offers no decision {offer!r}")
            at = wire.stamp(clock.now())
            moved = task.model_copy(
                update={
                    "completed": True,
                    "approval_status": decision,
                    "completed_at": task.completed_at if task.completed else at,
                    "modified_at": at,
                }
            )
        else:
            target = next(
                (t for t in TicketState if t is not asana.state_of(task) and _words_if_moved(asana, task, t) == offer),
                None,
            )
            if target is None:
                raise ValueError(f"asana task {task.gid} offers no move to {offer!r}")
            moved = asana.moved(task, target, now=clock.now())
        asana.put_task(moved, operation=Operation.UPDATE, actor=by, who=who.key, content=content, by=person)
        if COMMENT in given and given[COMMENT].strip():
            asana.put_story(
                wire.AsanaStory(
                    gid=asana.next_gid(),
                    text=given[COMMENT],
                    task=task.gid,
                    created_by=person,
                    created_at=wire.stamp(clock.now()),
                ),
                actor=by,
                by=person,
            )
        moves = world.children(MANIFEST.key, EntityKind.TRANSITION, item_parent(item), limit=1000)
        last = max(moves, key=lambda s: s.seq)
        event = next(e for e in world.events(since=last.seq - 1) if e.seq == last.seq)
        assert isinstance(event.after, TransitionSnapshot)
        await webhooks.deliver(asana, clock)
        return Transition.of(event)

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """Whether a webhook on the task, or on a project or task above it, hears of the move: that push is a wake.
        Otherwise the agent finds a person's move on its next read."""
        del who, clock
        asana = AsanaWorld(world)
        return webhooks.watching(asana, asana.scope(_task(asana, item)))

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
                    who=happening.person,
                    by=person,
                )
            case Reassigns():
                to = state.user_gid(happening.action.to) if happening.action.to is not None else None
                asana.put_task(
                    task.model_copy(update={"assignee": to, "modified_at": now}),
                    operation=Operation.UPDATE,
                    actor=Actor.PERSON,
                    by=person,
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
                    by=person,
                )
            case Deletes():
                asana.delete_task(task, actor=Actor.PERSON)


DECISIONS = (wire.ApprovalStatus.APPROVED, wire.ApprovalStatus.REJECTED, wire.ApprovalStatus.CHANGES_REQUESTED)
"""What an approver decides: the `approval_status` values that translate to `completed` true."""

COMMENT = "comment"
"""What a person's move takes besides where it goes: a comment, kept as a story."""

_CONTENT: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])


def _words_if_moved(asana: AsanaWorld, task: wire.AsanaTask, to: TicketState) -> str | None:
    """What the task would say moved to `to` as a person moves it; None when the status source cannot say `to`."""
    moved_at = wire.parse_stamp(task.modified_at)
    assert moved_at is not None
    try:
        return asana.words(asana.moved(task, to, now=moved_at))
    except state.StateUnexpressible:
        return None


def _reachable(asana: AsanaWorld, task: wire.AsanaTask) -> list[str]:
    """What the task would say moved to each other of open, done and cancelled the status source can express."""
    found: list[str] = []
    for to in TicketState:
        if to is asana.state_of(task):
            continue
        words = _words_if_moved(asana, task, to)
        if words is not None and words not in found and words != asana.words(task):
            found.append(words)
    return found


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
    """A `Provider` that also `HoldsTickets`, `EditsTickets` and `ActsOnTickets`; the tests hold it to all four."""
    return AsanaProvider()
