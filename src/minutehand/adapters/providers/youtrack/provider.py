"""The YouTrack provider: the REST API, the seeded instance, and the moves a person makes on an issue."""

from __future__ import annotations

from minutehand.adapters.providers.youtrack import wire
from minutehand.adapters.providers.youtrack.app import build_app
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.adapters.providers.youtrack.seed import seed
from minutehand.adapters.providers.youtrack.state import YouTrackWorld, millis
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario, TicketState
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
        if issue.assignee is None:
            raise ValueError(f"{issue.idReadable} has no assignee to move it")
        moved = youtrack.moved(
            issue, project, youtrack.state_for(project, to), by=issue.assignee, at=millis(clock.now())
        )
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
            if user.id not in project.team:
                raise ValueError(f"{assignee_email} is not on the team of {project.shortName}")
            changed = changed.model_copy(update={"assignee": user.id, "updated": at})
        youtrack.update_issue(changed, actor=Actor.SCENARIO)


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
    """A `Provider` that also `HoldsTickets` and `EditsTickets`; the tests hold it to all three."""
    return YouTrackProvider()
