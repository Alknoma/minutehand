"""The YouTrack provider: the REST API and Hub, the seeded instance, and the moves a person makes on an issue.

An issue assigned to a person is work they carry (`ports.transitions.CarriesWork`): what holds it back is each
unresolved issue it depends on, and what is left of it is its estimate less its spent time, both as YouTrack holds
them."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from minutehand.adapters.providers.youtrack import fields, wire
from minutehand.adapters.providers.youtrack.app import build_app
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.adapters.providers.youtrack.seed import DEPEND, YouTrackSeed, seed, write_faults
from minutehand.adapters.providers.youtrack.state import YouTrackWorld, issue_ref, millis, placed
from minutehand.domain.errors import Rendered
from minutehand.domain.people import PermissionGrant
from minutehand.domain.provider import Manifest, PersonChange, fault_fragment
from minutehand.domain.scenario import Person, Scenario, SeededTicket
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
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, TransitionSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store
from minutehand.ports.transitions import record


class YouTrackProvider:
    manifest: Manifest = MANIFEST
    seed_model = YouTrackSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def error(self, status: int, code: str, message: str) -> Rendered:
        return wire.error_answer(status, code, message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def place(self, additions: Sequence[Change], world: Store) -> list[Change]:
        """`PlacesAdditions`: an added issue takes its project's next number in the world (`state.placed`)."""
        return placed(additions, world)

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`YouTrackSeed.faults`, on a world already open."""
        found = fault_fragment(YouTrackSeed, faults, frozenset({"faults"})).faults
        write_faults(YouTrackWorld(world), found, clock.now(), declared=True)

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """An administrator bans or unbans the person's account: banned, it reads `banned: true` in YouTrack and
        Hub and it cannot be assigned. Its tokens still act as it: Minutehand checks no credential."""
        youtrack = YouTrackWorld(world)
        user = _account(youtrack, person)
        if change not in (PersonChange.DEACTIVATED, PersonChange.REACTIVATED):
            raise ValueError(f"youtrack has no way to show a person {change.value}")
        banned = change is PersonChange.DEACTIVATED
        if user.banned is banned:
            raise ValueError(f"{person.key}'s YouTrack account is already {'banned' if banned else 'not banned'}")
        youtrack.write_user(user.model_copy(update={"banned": banned}), actor=Actor.SCENARIO)
        del clock

    def permit(self, grant: PermissionGrant, person: Person, world: Store, clock: Clock) -> None:
        """A permission given to or taken from the person, in one project (by short name or name) or all of them,
        after every grant already made, so it is the last word. Hub's permissions cache reports it; no call is refused
        for it."""
        youtrack = YouTrackWorld(world)
        user = _account(youtrack, person)
        try:
            permission = wire.Permission(grant.permission)
        except ValueError:
            known = ", ".join(p.value for p in wire.Permission)
            raise ValueError(f"youtrack has no permission {grant.permission!r}; it has {known}") from None
        project = None
        if grant.project is not None:
            wanted = grant.project.lower()
            project = next((p for p in youtrack.projects() if wanted in (p.shortName.lower(), p.name.lower())), None)
            if project is None:
                raise ValueError(f"a grant names project {grant.project!r}, which the instance has not got")
        youtrack.write_grant(
            len(youtrack.grants()),
            wire.StoredGrant(
                user=user.id, permission=permission, project=None if project is None else project.id, held=grant.held
            ),
            actor=Actor.SCENARIO,
        )
        del clock

    # -- transitions (`ProvidesTransitions`) ---------------------------------------------------------------------

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        """Every issue assigned to the person's account whose State is not a resolved one, as they read it: work they
        carry until it is resolved, due on the day its Due Date names, and ranked by its Priority's place in its set,
        which YouTrack orders most urgent first (Show-stopper is 0)."""
        youtrack = YouTrackWorld(world)
        user = youtrack.user_by_login(person.key) or (youtrack.user_by_email(person.email) if person.email else None)
        if user is None:
            return []
        waiting: list[Waiting] = []
        for issue in youtrack.every_issue():
            project = youtrack.project(issue.project)
            if project is None:
                continue
            assignee = youtrack.assignee_of(project, issue)
            state = youtrack.state_of(project, issue)
            if assignee is None or assignee.id != user.id or state is None or state.isResolved:
                continue
            waiting.append(
                Waiting(
                    item=issue_ref(issue.id),
                    state=state.name,
                    shown=_shown(youtrack, issue, state),
                    work=True,
                    due=_due(youtrack, project, issue),
                    rank=_rank(youtrack, project, issue),
                )
            )
        return waiting

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        """Every value of the project's State field but the one the issue is in, each with a comment and what it
        means: YouTrack holds no workflow beside the field's values, and a person sets State as the agent does. Then,
        only for what the scenario has someone do: a comment, a reassignment, the issue's deletion."""
        del by, who
        youtrack = YouTrackWorld(world)
        issue, project = _located(youtrack, item)
        field = youtrack.state_field(project)
        current = youtrack.state_of(project, issue)
        moves = [
            Offer(
                name=value.name,
                to_state=value.name,
                means=value.outcome,
                fields=[OfferField(name=COMMENT, description="A comment added to the issue as its State changes")],
            )
            for value in sorted(field.values if field is not None else [], key=lambda v: v.ordinal)
            if current is None or value.id != current.id
        ]
        return [*moves, *ticket_acts(current.name if current is not None else "")]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The person sets the issue's State, as `POST /issues/{id}/customFields/{field}` sets it, with their comment;
        or comments, reassigns or deletes the issue as the API does; as themselves. The scenario sets State as the
        issue's last updater."""
        youtrack = YouTrackWorld(world)
        issue, project = _located(youtrack, item)
        found = next((o for o in self.legal(item, by, who, world) if o.name == offer), None)
        if found is None:
            raise ValueError(f"{issue.idReadable} offers no State {offer!r}")
        given = content_of(content, found, who.key if who is not None else by.value)
        if who is None and by is not Actor.SCENARIO:
            raise ValueError("a YouTrack issue is changed by an account: name the person")
        author = _account(youtrack, who).id if who is not None else issue.updater
        at = millis(clock.now())
        current = youtrack.state_of(project, issue)
        before = current.name if current is not None else ""
        if offer == COMMENT:
            if who is None:
                raise ValueError(f"a comment on {issue.idReadable} is written by an account: name the person")
            youtrack.write_comment(
                wire.StoredComment(
                    id=youtrack.next_id(4), issue=issue.id, text=given[COMMENT], author=author, created=at
                ),
                actor=by,
            )
            return _recorded(world, item, offer, before, before, by, who, content, clock)
        if offer == REASSIGN:
            address = given[ASSIGNEE].strip() if ASSIGNEE in given else ""
            to = youtrack.user_by_email(address) if address else None
            if address and to is None:
                raise ValueError(f"no YouTrack user has the email {address}")
            youtrack.update_issue(_assigned(youtrack, project, issue, to, by=author, at=at), actor=by)
            return _recorded(world, item, offer, before, before, by, who, content, clock)
        if offer == DELETE:
            youtrack.delete_issue(issue, by=author, at=at, actor=by)
            return _recorded(world, item, offer, before, DELETED, by, who, content, clock)
        field = youtrack.state_field(project)
        assert field is not None
        value = next(v for v in field.values if v.name == offer)
        youtrack.update_issue(youtrack.moved(issue, project, value, by=author, at=at), actor=by, content=content)
        if COMMENT in given and given[COMMENT].strip():
            youtrack.write_comment(
                wire.StoredComment(
                    id=youtrack.next_id(4), issue=issue.id, text=given[COMMENT], author=author, created=at
                ),
                actor=by,
            )
        return _last_move(world, item)

    def seeded(self, scenario: Scenario, ticket: SeededTicket, world: Store) -> EntityRef | None:
        """The issue seeded from `ticket`, while it is there."""
        issue = YouTrackWorld(world).seeded_issue(next(n for n, t in enumerate(scenario.tickets) if t is ticket))
        return issue_ref(issue.id) if issue is not None else None

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """Never: the fake serves no webhooks, so the agent finds a person's change on its next read."""
        del item, who, world, clock
        return False

    # -- work (`CarriesWork`) ------------------------------------------------------------------------------------

    def holding(self, item: EntityRef, world: Store) -> list[EntityRef]:
        """Each issue this one depends on, by a link of the Depend type (this one "depends on" it, it "is required
        for" this one, so it must be resolved first), whose State is not a resolved one, in the order the links were
        made."""
        youtrack = YouTrackWorld(world)
        issue, _ = _located(youtrack, item)
        depend = {t.id for t in youtrack.link_types() if t.name == DEPEND}
        held: list[EntityRef] = []
        for link in youtrack.links():
            if link.linkType not in depend or link.target != issue.id:
                continue
            required = youtrack.issue(link.source)
            project = youtrack.project(required.project) if required is not None else None
            if required is not None and project is not None and not youtrack.is_resolved(project, required):
                held.append(issue_ref(required.id))
        return held

    def estimate(self, item: EntityRef, world: Store) -> timedelta | None:
        """The issue's estimate less its spent time, by the period fields its project's time-tracking settings name
        (what is left of the estimate's pie), never below nothing; None where the project tracks no time or the issue
        holds no estimate."""
        youtrack = YouTrackWorld(world)
        issue, project = _located(youtrack, item)
        tracking = project.timeTracking
        if not tracking.enabled or tracking.estimate is None or tracking.estimate not in issue.values:
            return None
        spent = _minutes(issue, tracking.timeSpent) if tracking.timeSpent is not None else 0
        return timedelta(minutes=max(0, _minutes(issue, tracking.estimate) - spent))


def _assigned(
    youtrack: YouTrackWorld,
    project: wire.StoredProject,
    issue: wire.StoredIssue,
    user: wire.StoredUser | None,
    *,
    by: str,
    at: int,
) -> wire.StoredIssue:
    field = youtrack.assignee_field(project)
    if field is None:
        raise LookupError(f"project {project.shortName} has no Assignee field")
    if user is not None and user.id not in project.team:
        raise ValueError(f"{user.login} is not on the team of {project.shortName}")
    values = {k: v for k, v in issue.values.items() if k != field.id}
    if user is not None:
        values[field.id] = user.id
    changed = issue.model_copy(update={"values": values})
    return youtrack.settled(changed, project, was=issue, by=by, at=at)


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


def _shown(youtrack: YouTrackWorld, issue: wire.StoredIssue, state: wire.StoredBundleValue) -> str:
    """The issue as its assignee reads it: id and summary, State, description and comments, oldest first."""
    lines = [f"{issue.idReadable}: {issue.summary}", f"State: {state.name}"]
    if issue.description:
        lines += ["", issue.description]
    for comment in youtrack.comments(issue.id):
        author = youtrack.user(comment.author)
        lines.append(f"{author.login if author else 'Someone'}: {comment.text}")
    return "\n".join(lines)


def _due(youtrack: YouTrackWorld, project: wire.StoredProject, issue: wire.StoredIssue) -> date | None:
    """The day the issue's Due Date names, as the date picker keeps it (noon UTC of that day, `seed.DAY_AT`); None
    when the project carries no Due Date or the issue holds none."""
    field = youtrack.project_field(project, fields.DUE_DATE.name)
    if field is None or field.id not in issue.values:
        return None
    moment = issue.values[field.id]
    if not isinstance(moment, int):
        raise ValueError(f"{issue.idReadable}'s {fields.DUE_DATE.name} holds {moment!r}, which is no moment")
    return datetime.fromtimestamp(moment / 1000, UTC).date()


def _rank(youtrack: YouTrackWorld, project: wire.StoredProject, issue: wire.StoredIssue) -> int | None:
    """The place of the issue's Priority among the values of its set, in their order (each value's `ordinal`), which
    for Priority runs most urgent first (Show-stopper is 0); None when the project carries no Priority or the issue
    holds none."""
    field = youtrack.project_field(project, fields.PRIORITY.name)
    if field is None or field.id not in issue.values:
        return None
    ordered = sorted(field.values, key=lambda v: v.ordinal)
    return next((n for n, v in enumerate(ordered) if v.id == issue.values[field.id]), None)


def _minutes(issue: wire.StoredIssue, field: str) -> int:
    """What the issue's period field `field` holds, in minutes; 0 when it holds nothing."""
    if field not in issue.values:
        return 0
    minutes = issue.values[field]
    if not isinstance(minutes, int):
        raise ValueError(f"{issue.idReadable} holds {minutes!r} in a period field, which holds minutes")
    return minutes


def _last_move(world: Store, item: EntityRef) -> Transition:
    moves = world.children(MANIFEST.key, EntityKind.TRANSITION, item_parent(item), limit=1000)
    last = max(moves, key=lambda s: s.seq)
    event = next(e for e in world.events(since=last.seq - 1) if e.seq == last.seq)
    assert isinstance(event.after, TransitionSnapshot)
    return Transition.of(event)


def _account(youtrack: YouTrackWorld, person: Person) -> wire.StoredUser:
    user = youtrack.user_by_login(person.key) or (youtrack.user_by_email(person.email) if person.email else None)
    if user is None:
        raise ValueError(f"{person.key} has no YouTrack account")
    return user


def _located(youtrack: YouTrackWorld, ticket: EntityRef) -> tuple[wire.StoredIssue, wire.StoredProject]:
    if ticket.provider != MANIFEST.key:
        raise ValueError(f"{ticket.provider} ticket {ticket.external_id} is not a YouTrack issue")
    issue = youtrack.issue(ticket.external_id)
    if issue is None:
        raise LookupError(f"no YouTrack issue {ticket.external_id} in this run")
    project = youtrack.project(issue.project)
    if project is None:
        raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
    return issue, project


def build() -> YouTrackProvider:
    """A `Provider` that also `ProvidesTransitions`, `CarriesWork` and `HoldsSeeded`."""
    return YouTrackProvider()
