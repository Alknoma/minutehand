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

from pydantic import JsonValue, TypeAdapter, ValidationError

from minutehand.adapters.providers.jira import wire
from minutehand.adapters.providers.jira.app import build_app
from minutehand.adapters.providers.jira.manifest import MANIFEST
from minutehand.adapters.providers.jira.moves import Desk
from minutehand.adapters.providers.jira.seed import JiraSeed, seed
from minutehand.adapters.providers.jira.state import issue_ref, placed
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
from minutehand.domain.transitions import Offer, OfferField, Transition, Waiting
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class JiraProvider:
    manifest: Manifest = MANIFEST
    seed_model = JiraSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def error(self, status: int, code: str, message: str) -> Rendered:
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
        self._move(desk, issue, to, by=issue.assignee, clock=clock, who=None)

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
        user = jira.user_by_email(person.email)
        if user is None:
            raise ValueError(f"{person.key} has no Jira account")
        active = change is PersonChange.REACTIVATED
        if change not in (PersonChange.DEACTIVATED, PersonChange.REACTIVATED):
            raise ValueError(f"jira has no way to show a person {change.value}")
        if user.active is active:
            raise ValueError(f"{person.key}'s Jira account is already {'active' if active else 'inactive'}")
        jira.write_user(user.model_copy(update={"active": active}), actor=Actor.SCENARIO)
        del clock

    # ------------------------------------------------------------------ transitions (`ProvidesTransitions`)

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        """Every issue assigned to the person's account whose status is not in the Done category: work that waits on
        them, shown as they would read it in Jira."""
        desk = Desk(world)
        if not desk.world.seeded():
            return []
        user = desk.world.user_by_email(person.email)
        if user is None:
            return []
        site = desk.site()
        waiting: list[Waiting] = []
        for issue in desk.world.every_issue():
            status = site.status(issue.status)
            if issue.assignee != user.accountId or status.category is wire.Category.DONE:
                continue
            waiting.append(Waiting(item=issue_ref(issue.id), state=status.name, shown=_shown(desk, issue)))
        return waiting

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        """The workflow transitions open from the issue's status, as `GET /issue/{key}/transitions` lists them, each
        taking a comment. One whose screen requires a field is not offered to a person, whose engine writes only
        free text (README, "People")."""
        del by, who
        desk = Desk(world)
        issue = _located(desk, item)
        if issue is None:
            raise LookupError(f"no Jira issue {item.external_id} in this run")
        site = desk.site()
        return [
            Offer(
                name=t.name,
                to_state=site.status(t.to).name,
                fields=[OfferField(name=COMMENT, description="A comment added to the issue as it moves")],
            )
            for t in desk.transitions(issue, _project(desk, issue))
            if not t.required
        ]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The person takes the transition through the same path as `POST /issue/{key}/transitions`, with their
        comment as its `update.comment`, as themselves."""
        desk = Desk(world)
        issue = _located(desk, item)
        if issue is None:
            raise LookupError(f"no Jira issue {item.external_id} in this run")
        project = _project(desk, issue)
        transition = next((t for t in desk.transitions(issue, project) if t.name == offer and not t.required), None)
        if transition is None:
            raise ValueError(f"{issue.key} offers no transition {offer!r} from {desk.site().status(issue.status).name}")
        given = _content(content)
        unknown = sorted(set(given) - {COMMENT})
        if unknown:
            raise ValueError(f"a Jira transition takes a comment, not {', '.join(unknown)}")
        update: wire.Json = {}
        if COMMENT in given and given[COMMENT].strip():
            update = {COMMENT: [{"add": {"body": wire.adf_from_text(given[COMMENT])}}]}
        account = _account(desk, who.email) if who is not None else None
        if account is None:
            raise ValueError("a Jira transition is taken by an account: name the person who takes it")
        return desk.transition(
            issue,
            project,
            transition,
            fields={},
            update=update,
            by=account,
            at=clock.now(),
            actor=by,
            who=who.key if who is not None else None,
        )

    def heard_of(self, item: EntityRef, world: Store, clock: Clock) -> bool:
        """Never: the fake serves no webhooks, so the agent finds a person's move on its next read."""
        del item, world, clock
        return False

    # ------------------------------------------------------------------ a person's acts

    def act(self, happening: TicketHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        """The happening's person does its action to the issue seeded from its ticket, as themselves."""
        desk = Desk(world)
        seeded = scenario.happening_ticket(happening)
        position = next(n for n, t in enumerate(scenario.tickets) if t is seeded)
        issue = next((i for i in desk.world.every_issue() if i.seededFrom == position), None)
        if issue is None:
            return
        by = _account(desk, _email(scenario, happening.person))
        action = happening.action
        if isinstance(action, Moves):
            self._move(desk, issue, action.to, by=by, clock=clock, who=happening.person)
        elif isinstance(action, Reassigns):
            to = None if action.to is None else _account(desk, _email(scenario, action.to))
            changed = desk.apply_fields(issue, _project(desk, issue), {"assignee": _assignee(to)}, creating=False)
            desk.write(issue, changed, by=by, at=clock.now(), actor=Actor.PERSON)
        elif isinstance(action, Comments):
            desk.comment(issue, wire.adf_from_text(action.text), by=by, at=clock.now(), actor=Actor.PERSON)
        elif isinstance(action, Deletes):
            desk.delete(issue, actor=Actor.PERSON)

    def _move(
        self, desk: Desk, issue: wire.StoredIssue, to: TicketState, *, by: str, clock: Clock, who: str | None
    ) -> None:
        """Walk the fewest transitions that end in a status meaning `to`, each its own changelog entry and its own
        transition, skipping any whose screen requires a field a person would have to fill. `who` is the person's
        key, when the scenario names them."""
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
            desk.transition(
                current, project, transition, fields={}, update={}, by=by, at=clock.now(), actor=Actor.PERSON, who=who
            )
            found = desk.world.issue(current.id)
            assert found is not None
            current = found


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


COMMENT = "comment"
"""The one field a person's transition takes: Jira's `update.comment`."""

_CONTENT: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])


def _content(content: str) -> dict[str, str]:
    try:
        return _CONTENT.validate_json(content)
    except ValidationError as e:
        raise ValueError(f"a transition's content is a JSON object of text fields: {content!r}") from e


def _shown(desk: Desk, issue: wire.StoredIssue) -> str:
    """The issue as its assignee reads it in Jira: key and summary, status, priority, due date, description and
    comments, oldest first."""
    site = desk.site()
    lines = [f"{issue.key}: {issue.summary}", f"Status: {site.status(issue.status).name}"]
    lines.append(f"Priority: {site.priority(issue.priority).name}")
    if issue.duedate is not None:
        lines.append(f"Due: {issue.duedate.isoformat()}")
    described = wire.adf_text(issue.description)
    if described:
        lines += ["", described]
    for comment in desk.world.comments(issue.id):
        author = desk.world.user(comment.author)
        lines.append(f"[{comment.created:%Y-%m-%d %H:%M} UTC] {author.displayName if author else 'Someone'}: "
                     f"{wire.adf_text(comment.body)}")  # fmt: skip
    return "\n".join(lines)


def _located(desk: Desk, ticket: EntityRef) -> wire.StoredIssue | None:
    if ticket.provider != MANIFEST.key or ticket.kind is not EntityKind.TICKET:
        raise ValueError(f"{ticket.provider} {ticket.kind.value} {ticket.external_id} is not a Jira issue")
    return desk.world.find_issue(ticket.external_id)


def _project(desk: Desk, issue: wire.StoredIssue) -> wire.StoredProject:
    project = desk.world.project(issue.project)
    if project is None:
        raise LookupError(f"{issue.key} names project {issue.project}, which does not exist")
    return project


def _email(scenario: Scenario, person: str) -> str:
    return next(p.email for p in scenario.people if p.key == person)


def _assignee(account: str | None) -> JsonValue:
    return {"accountId": account} if account is not None else None


def _account(desk: Desk, email: str) -> str:
    user = desk.world.user_by_email(email)
    if user is None:
        raise LookupError(f"no Jira account has the email {email}")
    return user.accountId


def build() -> JiraProvider:
    """A `Provider` that also `HoldsTickets`, `EditsTickets`, `ActsOnTickets` and `ProvidesTransitions`."""
    return JiraProvider()
