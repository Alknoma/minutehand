"""The YouTrack provider: the REST API and Hub, the seeded instance, and the moves a person makes on an issue."""

from __future__ import annotations

from collections.abc import Sequence

from minutehand.adapters.providers.youtrack import wire
from minutehand.adapters.providers.youtrack.app import build_app
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.adapters.providers.youtrack.seed import YouTrackSeed, seed, write_faults
from minutehand.adapters.providers.youtrack.state import YouTrackWorld, millis, placed
from minutehand.domain.people import PermissionGrant
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
from minutehand.domain.world import Actor, Change, EntityRef
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class YouTrackProvider:
    manifest: Manifest = MANIFEST
    seed_model = YouTrackSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def place(self, additions: Sequence[Change], world: Store) -> list[Change]:
        """`PlacesAdditions`: an added issue takes its project's next number in the world (`state.placed`)."""
        return placed(additions, world)

    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None:
        """The assignee resolves, cancels or reopens the issue: its State moves to the project's first value for `to`."""
        youtrack = YouTrackWorld(world)
        issue, project = _located(youtrack, ticket)
        assignee = youtrack.assignee_of(project, issue)
        if assignee is None:
            raise ValueError(f"{issue.idReadable} has no assignee to move it")
        moved = youtrack.moved(issue, project, youtrack.state_for(project, to), by=assignee.id, at=millis(clock.now()))
        youtrack.update_issue(moved, actor=Actor.PERSON)

    def edit(
        self, ticket: EntityRef, *, state: TicketState | None, assignee_email: str | None, world: Store, clock: Clock
    ) -> None:
        """The scenario rewrites the issue's state and/or assignee; None leaves a field as it is."""
        youtrack = YouTrackWorld(world)
        issue, project = _located(youtrack, ticket)
        at = millis(clock.now())
        changed = issue
        if state is not None:
            changed = youtrack.moved(changed, project, youtrack.state_for(project, state), by=changed.updater, at=at)
        if assignee_email is not None:
            user = youtrack.user_by_email(assignee_email)
            if user is None:
                raise LookupError(f"no YouTrack user has the email {assignee_email}")
            changed = _assigned(youtrack, project, changed, user, by=changed.updater, at=at)
        youtrack.update_issue(changed, actor=Actor.SCENARIO)

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`YouTrackSeed.faults`, on a world already open."""
        found = fault_fragment(YouTrackSeed, faults, frozenset({"faults"})).faults
        write_faults(YouTrackWorld(world), found, clock.now())

    def delete_ticket(self, ticket: EntityRef, world: Store, clock: Clock) -> None:
        """The issue is deleted, with every link it is an end of, by its assignee (or whoever last changed it)."""
        youtrack = YouTrackWorld(world)
        issue, project = _located(youtrack, ticket)
        assignee = youtrack.assignee_of(project, issue)
        by = assignee.id if assignee is not None else issue.updater
        youtrack.delete_issue(issue, by=by, at=millis(clock.now()), actor=Actor.PERSON)

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """An administrator bans or unbans the person's account: banned, it reads `banned: true` in YouTrack and
        Hub, its tokens no longer sign in, and it cannot be assigned."""
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
        after every grant already made, so it is the last word."""
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

    def act(self, happening: TicketHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        """A person changes the state or assignee of a seeded issue, comments on it, or deletes it, as themselves. An
        issue no longer there (the agent or someone else deleted it) is left alone and nothing is written."""
        youtrack = YouTrackWorld(world)
        seeded = scenario.happening_ticket(happening)
        issue = youtrack.seeded_issue(next(n for n, t in enumerate(scenario.tickets) if t is seeded))
        if issue is None:
            return
        project = youtrack.project(issue.project)
        if project is None:
            raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
        author = youtrack.user_by_login(happening.person)
        if author is None:
            raise LookupError(f"{happening.person} has no YouTrack account")
        at = millis(clock.now())
        action = happening.action
        if isinstance(action, Moves):
            moved = youtrack.moved(issue, project, youtrack.state_for(project, action.to), by=author.id, at=at)
            youtrack.update_issue(moved, actor=Actor.PERSON)
        elif isinstance(action, Reassigns):
            to = None if action.to is None else youtrack.user_by_login(action.to)
            if action.to is not None and to is None:
                raise LookupError(f"{action.to} has no YouTrack account")
            youtrack.update_issue(_assigned(youtrack, project, issue, to, by=author.id, at=at), actor=Actor.PERSON)
        elif isinstance(action, Comments):
            youtrack.write_comment(
                wire.StoredComment(
                    id=youtrack.next_id(4), issue=issue.id, text=action.text, author=author.id, created=at
                ),
                actor=Actor.PERSON,
            )
        elif isinstance(action, Deletes):
            youtrack.delete_issue(issue, by=author.id, at=at, actor=Actor.PERSON)


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


def _account(youtrack: YouTrackWorld, person: Person) -> wire.StoredUser:
    user = youtrack.user_by_login(person.key) or youtrack.user_by_email(person.email)
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
    """A `Provider` that also `HoldsTickets`, `EditsTickets` and `ActsOnTickets`; the tests hold it to all four."""
    return YouTrackProvider()
