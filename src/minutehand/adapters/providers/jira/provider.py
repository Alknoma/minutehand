"""The Jira provider: the REST API, the seeded site, and what people do to issues without the agent.

`act` (`ActsOnTickets`) lands a `TicketHappening` on the issue seeded from its ticket, by the happening's person at
the run clock's time, recorded as actor PERSON with that person as the changelog's or comment's author: `Moves`
walks the workflow to a status that means the state, `Reassigns`, `Comments` and `Deletes` do what they say.
`transition` (`HoldsTickets`) is a move by the issue's assignee; `edit` (`EditsTickets`) rewrites state and
assignee as the scenario, past the workflow. An act on an issue that is no longer there (the agent deleted it)
writes nothing: the person finds nothing to act on.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import JsonValue

from minutehand.adapters.answering import guarded
from minutehand.adapters.providers.jira import wire
from minutehand.adapters.providers.jira.app import build_app
from minutehand.adapters.providers.jira.manifest import MANIFEST
from minutehand.adapters.providers.jira.moves import Desk
from minutehand.adapters.providers.jira.seed import JiraSeed, seed
from minutehand.adapters.providers.jira.state import placed
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
from minutehand.domain.world import Actor, Change, EntityRef
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class JiraProvider:
    manifest: Manifest = MANIFEST
    seed_model = JiraSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return guarded(build_app(world, clock), self, provider=self.manifest.key)

    def error(self, status: int, code: str, message: str) -> Rendered:
        """Minutehand's own 501 or 500 in Jira's `{"errorMessages": [message], "errors": {}}`, the body every Jira
        client reads a failure from; Jira's body has no code, so `code` stays on the recorded call."""
        del code
        return wire.error_answer(status, message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def place(self, additions: Sequence[Change], world: Store) -> list[Change]:
        """`PlacesAdditions`: an added issue takes its project's next number in the world (`state.placed`)."""
        return placed(additions, world)

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`JiraSeed.rate_limits`, on a world already open: each answers its next calls 429."""
        limits = fault_fragment(JiraSeed, faults, frozenset({"rate_limits"})).rate_limits
        Desk(world).world.declare_limits(limits)
        del clock

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
        self, ticket: EntityRef, *, state: TicketState | None, assignee: Person | None, world: Store, clock: Clock
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
        if assignee is not None:
            user = desk.world.user_of(assignee.key)
            if user is None:
                raise LookupError(f"{assignee.key} has no Jira account")
            if user.accountId not in {u.accountId for u in desk.assignable(project)}:
                raise ValueError(f"{assignee.key} cannot be assigned issues in {project.key}")
            changed = changed.model_copy(update={"assignee": user.accountId})
        written = desk.write(issue, changed, by=None, at=clock.now(), actor=Actor.SCENARIO)
        if written is issue:
            desk.world.update_issue(issue, actor=Actor.SCENARIO)

    def delete_ticket(self, ticket: EntityRef, world: Store, clock: Clock) -> None:
        """The issue is deleted, with its subtasks and the links naming them, as its assignee deletes it."""
        desk = Desk(world)
        issue = _located(desk, ticket)
        if issue is None:
            raise LookupError(f"no Jira issue {ticket.external_id} in this run")
        desk.delete(issue, actor=Actor.PERSON)
        del clock

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """A site administrator deactivates or reactivates the person's Atlassian account: deactivated, it signs in
        to nothing, is not assignable, and reads `active: false` wherever it is shown."""
        jira = Desk(world).world
        user = jira.user_of(person.key)
        if user is None:
            raise ValueError(f"{person.key} has no Jira account")
        active = change is PersonChange.REACTIVATED
        if change not in (PersonChange.DEACTIVATED, PersonChange.REACTIVATED):
            raise ValueError(f"jira has no way to show a person {change.value}")
        if user.active is active:
            raise ValueError(f"{person.key}'s Jira account is already {'active' if active else 'inactive'}")
        jira.write_user(user.model_copy(update={"active": active}), actor=Actor.SCENARIO)
        del clock

    # ------------------------------------------------------------------ a person's acts

    def act(self, happening: TicketHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        """The happening's person does its action to the issue seeded from its ticket, as themselves."""
        desk = Desk(world)
        seeded = scenario.happening_ticket(happening)
        position = next(n for n, t in enumerate(scenario.tickets) if t is seeded)
        issue = next((i for i in desk.world.every_issue() if i.seededFrom == position), None)
        if issue is None:
            return
        by = _account(desk, happening.person)
        action = happening.action
        if isinstance(action, Moves):
            self._move(desk, issue, action.to, by=by, clock=clock)
        elif isinstance(action, Reassigns):
            to = None if action.to is None else _account(desk, action.to)
            changed = desk.apply_fields(issue, _project(desk, issue), {"assignee": _assignee(to)}, creating=False)
            desk.write(issue, changed, by=by, at=clock.now(), actor=Actor.PERSON)
        elif isinstance(action, Comments):
            desk.comment(issue, wire.adf_from_text(action.text), by=by, at=clock.now(), actor=Actor.PERSON)
        elif isinstance(action, Deletes):
            desk.delete(issue, actor=Actor.PERSON)

    def _move(self, desk: Desk, issue: wire.StoredIssue, to: TicketState, *, by: str, clock: Clock) -> None:
        """Walk the fewest transitions that end in a status meaning `to`, each its own changelog entry, skipping
        any whose screen requires a field a person would have to fill."""
        project = _project(desk, issue)
        site = desk.site()
        if site.status(issue.status).outcome is to:
            return
        route = _route(desk, issue, project, to)
        if route is None:
            raise LookupError(
                f"no transitions lead from {site.status(issue.status).name} in {project.key} to a status that is "
                f"{to.value} without a screen to fill"
            )
        current = issue
        for transition in route:
            moved = desk.moved(current, site.status(transition.to))
            current = desk.write(current, moved, by=by, at=clock.now(), actor=Actor.PERSON)


def _route(
    desk: Desk, issue: wire.StoredIssue, project: wire.StoredProject, to: TicketState
) -> list[wire.StoredTransition] | None:
    site = desk.site()
    seen = {issue.status}
    frontier: list[tuple[str, list[wire.StoredTransition]]] = [(issue.status, [])]
    while frontier:
        status, path = frontier.pop(0)
        here = issue.model_copy(update={"status": status})
        for transition in desk.transitions(here, project):
            if transition.required or transition.to in seen:
                continue
            route = [*path, transition]
            if site.status(transition.to).outcome is to:
                return route
            seen.add(transition.to)
            frontier.append((transition.to, route))
    return None


def _located(desk: Desk, ticket: EntityRef) -> wire.StoredIssue | None:
    if ticket.provider != MANIFEST.key:
        raise ValueError(f"{ticket.provider} ticket {ticket.external_id} is not a Jira issue")
    return desk.world.find_issue(ticket.external_id)


def _project(desk: Desk, issue: wire.StoredIssue) -> wire.StoredProject:
    project = desk.world.project(issue.project)
    if project is None:
        raise LookupError(f"{issue.key} names project {issue.project}, which does not exist")
    return project


def _assignee(account: str | None) -> JsonValue:
    return {"accountId": account} if account is not None else None


def _account(desk: Desk, person: str) -> str:
    """The accountId seeded for the person with this key."""
    user = desk.world.user_of(person)
    if user is None:
        raise LookupError(f"{person} has no Jira account")
    return user.accountId


def build() -> JiraProvider:
    """A `Provider` that also `HoldsTickets`, `EditsTickets` and `ActsOnTickets`."""
    return JiraProvider()
