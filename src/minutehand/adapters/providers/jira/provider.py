"""The Jira provider: the REST API, the seeded site, and what people do to issues.

Everything a person does to an issue is a transition through `apply` (`ports.transitions`), by the person at the run
clock's time, recorded as actor PERSON with that person as the changelog's or comment's author: a workflow
transition, a walk of them to a status that means an outcome, a comment, a reassignment, a deletion. The scenario
sets an outcome past the workflow (a fork's ticket edit). An act on an issue that is no longer there writes nothing.
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
    Person,
    Scenario,
    SeededTicket,
    TicketState,
)
from minutehand.domain.transitions import (
    ASSIGNEE,
    COMMENT,
    DELETE,
    REASSIGN,
    Offer,
    OfferField,
    Transition,
    Waiting,
    content_of,
    ticket_acts,
)
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store
from minutehand.ports.transitions import record


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
        taking a comment and saying what its status means. One whose screen requires a field is not offered to a
        person, whose engine writes only free text (README, "People"). Then, only for what the scenario has someone
        do: each other outcome as a walk of transitions to it (named open, done, cancelled), a comment, a
        reassignment, the issue's deletion; for the scenario itself, each outcome set past the workflow."""
        del who
        desk = Desk(world)
        issue = _located(desk, item)
        if issue is None:
            raise LookupError(f"no Jira issue {item.external_id} in this run")
        site = desk.site()
        project = _project(desk, issue)
        here = site.status(issue.status)
        if by is Actor.SCENARIO:
            outcomes = [
                Offer(name=outcome.value, to_state=status.name, means=outcome, unprompted=False)
                for outcome in TicketState
                if outcome is not here.outcome
                and (
                    status := next(
                        (site.status(s) for s in project.statuses if site.status(s).outcome is outcome), None
                    )
                )
                is not None
            ]
            return [*outcomes, *(o for o in ticket_acts(here.name) if o.name == REASSIGN)]
        direct = [
            Offer(
                name=t.name,
                to_state=site.status(t.to).name,
                means=site.status(t.to).outcome,
                fields=[OfferField(name=COMMENT, description="A comment added to the issue as it moves")],
            )
            for t in desk.transitions(issue, project)
            if not t.required
        ]
        walked: list[Offer] = []
        for outcome in TicketState:
            if outcome is here.outcome or any(o.means is outcome for o in direct):
                continue
            route = _route(desk, issue, project, outcome)
            status = site.status(route[-1].to) if route is not None else None
            if status is not None:
                walked.append(Offer(name=outcome.value, to_state=status.name, means=outcome, unprompted=False))
        return [*direct, *walked, *ticket_acts(here.name)]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The person takes the transition through the same path as `POST /issue/{key}/transitions`, with their
        comment as its `update.comment`, as themselves; walks the workflow to an outcome, transition by transition;
        comments, reassigns or deletes the issue as the API does. The scenario sets an outcome past the workflow.
        Every one is recorded as a transition."""
        desk = Desk(world)
        issue = _located(desk, item)
        if issue is None:
            raise LookupError(f"no Jira issue {item.external_id} in this run")
        project = _project(desk, issue)
        site = desk.site()
        found = next((o for o in self.legal(item, by, who, world) if o.name == offer), None)
        if found is None:
            raise ValueError(f"{issue.key} offers no transition {offer!r} from {site.status(issue.status).name}")
        given = content_of(content, found, who.key if who is not None else by.value)
        account = _account(desk, who.email) if who is not None else None
        if account is None and by is not Actor.SCENARIO:
            raise ValueError("a Jira transition is taken by an account: name the person who takes it")
        before = site.status(issue.status).name
        if offer in (COMMENT, REASSIGN, DELETE):
            if offer == COMMENT:
                if account is None:
                    raise ValueError(f"a comment on {issue.key} is written by an account: name the person")
                desk.comment(issue, wire.adf_from_text(given[COMMENT]), by=account, at=clock.now(), actor=by)
            elif offer == REASSIGN:
                address = given[ASSIGNEE].strip() if ASSIGNEE in given else ""
                try:
                    to = _account(desk, address) if address else None
                except LookupError as e:
                    raise ValueError(str(e.args[0]) if e.args else str(e)) from e
                changed = desk.apply_fields(issue, project, {"assignee": _assignee(to)}, creating=False)
                desk.write(issue, changed, by=account, at=clock.now(), actor=by)
            else:
                desk.delete(issue, actor=by)
            return _recorded(world, item, offer, before, found.to_state, by, who, content, clock)
        if by is Actor.SCENARIO or (found.means is not None and found.name == found.means.value):
            assert found.means is not None
            if by is Actor.SCENARIO:
                status = next(site.status(s) for s in project.statuses if site.status(s).outcome is found.means)
                desk.write(issue, desk.moved(issue, status), by=None, at=clock.now(), actor=Actor.SCENARIO)
                return _recorded(world, item, offer, before, status.name, by, who, content, clock)
            assert account is not None
            return self._walk(desk, issue, found.means, by=account, actor=by, clock=clock, who=who)
        transition = next(t for t in desk.transitions(issue, project) if t.name == offer)
        update: wire.Json = {}
        if COMMENT in given and given[COMMENT].strip():
            update = {COMMENT: [{"add": {"body": wire.adf_from_text(given[COMMENT])}}]}
        assert account is not None
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

    def seeded(self, scenario: Scenario, ticket: SeededTicket, world: Store) -> EntityRef | None:
        """The issue seeded from `ticket`, while it is there."""
        position = next(n for n, t in enumerate(scenario.tickets) if t is ticket)
        issue = next((i for i in Desk(world).world.every_issue() if i.seededFrom == position), None)
        return issue_ref(issue.id) if issue is not None else None

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """Never: the fake serves no webhooks, so the agent finds a person's move on its next read."""
        del item, who, world, clock
        return False

    def _walk(
        self,
        desk: Desk,
        issue: wire.StoredIssue,
        to: TicketState,
        *,
        by: str,
        actor: Actor,
        clock: Clock,
        who: Person | None,
    ) -> Transition:
        """Walk the fewest transitions that end in a status meaning `to`, each its own changelog entry and its own
        transition, skipping any whose screen requires a field a person would have to fill. Answers the last."""
        project = _project(desk, issue)
        route = _route(desk, issue, project, to)
        if route is None:
            raise ValueError(
                f"no transitions lead from {desk.site().status(issue.status).name} in {project.key} to a status that "
                f"is {to.value} without a screen to fill"
            )
        current = issue
        moved: Transition | None = None
        for transition in route:
            moved = desk.transition(
                current,
                project,
                transition,
                fields={},
                update={},
                by=by,
                at=clock.now(),
                actor=actor,
                who=who.key if who is not None else None,
            )
            found = desk.world.issue(current.id)
            assert found is not None
            current = found
        assert moved is not None
        return moved


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
    """A comment, a reassignment, a deletion or an outcome the scenario set, recorded once as the move it is."""
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
    """A `Provider` that also `ProvidesTransitions` and `HoldsSeeded`."""
    return JiraProvider()
