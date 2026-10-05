"""The YouTrack provider: the REST API and Hub, the seeded instance, and the moves a person makes on an issue."""

from __future__ import annotations

from minutehand.adapters.providers.youtrack import wire
from minutehand.adapters.providers.youtrack.app import build_app
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.adapters.providers.youtrack.seed import seed
from minutehand.adapters.providers.youtrack.state import YouTrackWorld, millis
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import (
    Commented,
    Person,
    Reassigned,
    Removed,
    Scenario,
    StateChanged,
    TicketHappening,
    TicketState,
)
from minutehand.domain.world import Actor, EntityRef
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class YouTrackProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

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

    def happen(self, happening: TicketHappening, by: Person, world: Store, clock: Clock) -> None:
        """A person changes the state or assignee of a seeded issue, comments on it, or deletes it, as themselves."""
        youtrack = YouTrackWorld(world)
        issue = youtrack.issue_by_key(happening.ticket)
        if issue is None:
            raise LookupError(f"no YouTrack issue is the seeded ticket {happening.ticket}, or it was deleted")
        project = youtrack.project(issue.project)
        if project is None:
            raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
        author = youtrack.user_by_login(by.key)
        if author is None:
            raise LookupError(f"{by.key} has no YouTrack account")
        at = millis(clock.now())
        change = happening.change
        if isinstance(change, StateChanged):
            moved = youtrack.moved(issue, project, youtrack.state_for(project, change.to), by=author.id, at=at)
            youtrack.update_issue(moved, actor=Actor.PERSON)
        elif isinstance(change, Reassigned):
            to = None if change.to is None else youtrack.user_by_login(change.to)
            if change.to is not None and to is None:
                raise LookupError(f"{change.to} has no YouTrack account")
            youtrack.update_issue(_assigned(youtrack, project, issue, to, by=author.id, at=at), actor=Actor.PERSON)
        elif isinstance(change, Commented):
            youtrack.write_comment(
                wire.StoredComment(
                    id=youtrack.next_id(4), issue=issue.id, text=change.text, author=author.id, created=at
                ),
                actor=Actor.PERSON,
            )
        elif isinstance(change, Removed):
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
    """A `Provider` that also `HoldsTickets`, `EditsTickets` and `TicketsHappen`; the tests hold it to all four."""
    return YouTrackProvider()
