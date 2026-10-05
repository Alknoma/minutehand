"""The Jira provider: the REST API, the seeded site, and what people do to issues without the agent.

A person's acts are methods here, each by a named person at the run clock's time and recorded as actor PERSON
with that person as the changelog's author: `moves` (through the workflow to a status that means a state),
`reassigns`, `comments` and `deletes`. `transition` (`HoldsTickets`) is `moves` by the issue's assignee;
`edit` (`EditsTickets`) rewrites state and assignee as the scenario, past the workflow. An act on an issue that
is no longer there (the agent deleted it) writes nothing: the person finds nothing to act on.
"""

from __future__ import annotations

from pydantic import JsonValue

from minutehand.adapters.providers.jira import wire
from minutehand.adapters.providers.jira.app import build_app
from minutehand.adapters.providers.jira.manifest import MANIFEST
from minutehand.adapters.providers.jira.moves import Desk
from minutehand.adapters.providers.jira.seed import seed
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario, TicketState
from minutehand.domain.world import Actor, EntityRef
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class JiraProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    # ------------------------------------------------------------------ ports

    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None:
        """The assignee moves the issue through the workflow to a status that means `to`, as actor PERSON."""
        desk = Desk(world)
        issue = _located(desk, ticket)
        if issue is None:
            raise LookupError(f"no Jira issue {ticket.external_id} in this run")
        if issue.assignee is None:
            raise ValueError(f"{issue.key} has no assignee to move it")
        self._move(desk, issue, to, by=issue.assignee, clock=clock)

    def edit(
        self, ticket: EntityRef, *, state: TicketState | None, assignee_email: str | None, world: Store, clock: Clock
    ) -> None:
        """The scenario rewrites the issue's state and/or assignee, past the workflow; None leaves a field."""
        desk = Desk(world)
        issue = _located(desk, ticket)
        if issue is None:
            raise LookupError(f"no Jira issue {ticket.external_id} in this run")
        project = _project(desk, issue)
        site = desk.site()
        changed = issue
        if state is not None:
            status = next((site.status(s) for s in project.statuses if site.status(s).outcome is state), None)
            if status is None:
                raise LookupError(f"project {project.key} has no status that is {state.value}")
            changed = desk.moved(changed, status)
        if assignee_email is not None:
            user = desk.world.user_by_email(assignee_email)
            if user is None:
                raise LookupError(f"no Jira account has the email {assignee_email}")
            if user.accountId not in {u.accountId for u in desk.assignable(project)}:
                raise ValueError(f"{assignee_email} cannot be assigned issues in {project.key}")
            changed = changed.model_copy(update={"assignee": user.accountId})
        written = desk.write(issue, changed, by=None, at=clock.now(), actor=Actor.SCENARIO)
        if written is issue:
            desk.world.update_issue(issue, actor=Actor.SCENARIO)

    # ------------------------------------------------------------------ a person's acts

    def moves(self, ticket: EntityRef, to: TicketState, *, by_email: str, world: Store, clock: Clock) -> None:
        """The person takes the issue through a transition to a status that means `to`."""
        desk = Desk(world)
        issue = _located(desk, ticket)
        if issue is not None:
            self._move(desk, issue, to, by=_account(desk, by_email), clock=clock)

    def reassigns(self, ticket: EntityRef, to_email: str | None, *, by_email: str, world: Store, clock: Clock) -> None:
        """The person hands the issue to someone else, or leaves it unassigned."""
        desk = Desk(world)
        issue = _located(desk, ticket)
        if issue is None:
            return
        project = _project(desk, issue)
        value: JsonValue = {"accountId": _account(desk, to_email)} if to_email is not None else None
        changed = desk.apply_fields(issue, project, {"assignee": value}, creating=False)
        desk.write(issue, changed, by=_account(desk, by_email), at=clock.now(), actor=Actor.PERSON)

    def comments(self, ticket: EntityRef, text: str, *, by_email: str, world: Store, clock: Clock) -> None:
        """The person writes a comment, kept as an Atlassian document."""
        desk = Desk(world)
        issue = _located(desk, ticket)
        if issue is not None:
            desk.comment(
                issue, wire.adf_from_text(text), by=_account(desk, by_email), at=clock.now(), actor=Actor.PERSON
            )

    def deletes(self, ticket: EntityRef, *, by_email: str, world: Store, clock: Clock) -> None:
        """The person deletes the issue, its subtasks and its links."""
        desk = Desk(world)
        issue = _located(desk, ticket)
        if issue is not None:
            _account(desk, by_email)
            desk.delete(issue, actor=Actor.PERSON)
        del clock

    def _move(self, desk: Desk, issue: wire.StoredIssue, to: TicketState, *, by: str, clock: Clock) -> None:
        project = _project(desk, issue)
        site = desk.site()
        if site.status(issue.status).outcome is to:
            return
        transition = next(
            (t for t in desk.transitions(issue, project) if site.status(t.to).outcome is to and not t.required),
            None,
        )
        if transition is None:
            raise LookupError(
                f"no transition from {site.status(issue.status).name} in {project.key} leads to a status that is "
                f"{to.value} without a screen to fill"
            )
        moved = desk.moved(issue, site.status(transition.to))
        desk.write(issue, moved, by=by, at=clock.now(), actor=Actor.PERSON)


def _located(desk: Desk, ticket: EntityRef) -> wire.StoredIssue | None:
    if ticket.provider != MANIFEST.key:
        raise ValueError(f"{ticket.provider} ticket {ticket.external_id} is not a Jira issue")
    return desk.world.find_issue(ticket.external_id)


def _project(desk: Desk, issue: wire.StoredIssue) -> wire.StoredProject:
    project = desk.world.project(issue.project)
    if project is None:
        raise LookupError(f"{issue.key} names project {issue.project}, which does not exist")
    return project


def _account(desk: Desk, email: str) -> str:
    user = desk.world.user_by_email(email)
    if user is None:
        raise LookupError(f"no Jira account has the email {email}")
    return user.accountId


def build() -> JiraProvider:
    """A `Provider` that also `HoldsTickets` and `EditsTickets`."""
    return JiraProvider()
