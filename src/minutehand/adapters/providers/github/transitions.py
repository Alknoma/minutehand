"""What people do on GitHub, through the one port people act through (`ports.transitions.ProvidesTransitions`).

An issue assigned to a person and still open waits on them: they close it (with a comment, and the reason GitHub
takes for closing one) or, once it is closed, reopen it. An open pull request waits on a person who is asked for their
review and has not given it, and on one it is assigned to: they approve it, ask for changes or comment (the three
events a review takes), merge it if they may and it merges cleanly, or close it. Each goes through the code the REST
route takes, so the same validation, history and update times apply, and each is one move of the item's state in the
log. When the agent declares an inbound target for GitHub, each is also pushed to it as the webhooks GitHub sends
(`hooks.py`), signed when the world declares a secret for the target.
"""

from __future__ import annotations

from minutehand.adapters.providers.github import pulls, state, wire
from minutehand.adapters.providers.github.app import GitHubApi
from minutehand.adapters.providers.github.hooks import Pusher
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.domain.errors import NotServed
from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Person
from minutehand.domain.transitions import Offer, OfferField, Transition, Waiting, content_of, item_parent
from minutehand.domain.world import Actor, EntityKind, EntityRef, TransitionSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

REVIEW_REQUESTED = "review requested"
ASSIGNED = "assigned"
BODY = "body"
COMMENT = "comment"
REASON = "state_reason"
CLOSE = "close"
REOPEN = "reopen"
MERGE = "merge"
COMMIT_TITLE = "commit_title"
COMMIT_MESSAGE = "commit_message"
CLOSING_REASONS = (wire.StateReason.COMPLETED, wire.StateReason.NOT_PLANNED)
"""What a person closing an issue can say it was closed as: `duplicate` needs the issue it duplicates."""


def account_of(world: GitHubWorld, person: Person) -> wire.StoredAccount | None:
    """The user a scenario person is on GitHub: the account that carries their email."""
    return next(
        (
            a
            for a in world.accounts()
            if a.type is wire.AccountType.USER and (a.email or "").lower() == person.email.lower()
        ),
        None,
    )


def locate(world: GitHubWorld, item: EntityRef) -> tuple[wire.StoredRepository, wire.StoredIssue]:
    """The repository and the issue or pull request an item names; one the world does not hold raises `LookupError`."""
    if item.provider != MANIFEST.key or item.kind is not EntityKind.RECORD or not item.external_id.startswith("issue/"):
        raise ValueError(f"{item} is not a github issue or pull request")
    _, owner, name, number = item.external_id.split("/")
    repository = world.repository(owner, name)
    found = None if repository is None else world.issue(repository, int(number))
    if repository is None or found is None:
        raise LookupError(f"no github issue {item.external_id} in the world")
    return repository, found


def reaches(world: GitHubWorld, account: wire.StoredAccount, repository: wire.StoredRepository) -> int:
    """How far the account's role on the repository reaches, as an index of `PERMISSION_ORDER`; -1 when it may not
    see it."""
    role = world.role(account, repository) or (None if repository.private else wire.Permission.PULL)
    return -1 if role is None else wire.PERMISSION_ORDER.index(role)


def last_move(world: Store, item: EntityRef) -> Transition:
    """The latest move recorded of the item."""
    moves = world.children(MANIFEST.key, EntityKind.TRANSITION, item_parent(item), limit=1000)
    last = max(moves, key=lambda s: s.seq)
    event = next(e for e in world.events(since=last.seq - 1) if e.seq == last.seq)
    assert isinstance(event.after, TransitionSnapshot)
    return Transition.of(event)


class GitHubTransitions:
    """The provider's moves for the people engine, for one agent: when it declares an inbound target for GitHub, each
    move is pushed there."""

    def __init__(self, target: InboundTarget | None = None, secret: str | None = None) -> None:
        # The secret signs the push only where the world declares one for the target: a target with none is sent a
        # delivery with no signature.
        self._pusher = None if target is None else Pusher(target, secret if target.secret is not None else None)

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        """Every open issue assigned to the person's account, and every open pull request that asks their review and
        has none from them, or is assigned to them, as they read it on GitHub."""
        github = GitHubWorld(world)
        account = account_of(github, person)
        if account is None:
            return []
        waiting: list[Waiting] = []
        for repository in github.repositories():
            if reaches(github, account, repository) < 0:
                continue
            reviewed = {(r.pull, r.author.lower()) for r in github.reviews(repository)}
            for issue in github.issues(repository):
                if issue.state is not wire.IssueState.OPEN:
                    continue
                item = state.issue_ref(repository.owner, repository.name, issue.number)
                if issue.pull is None:
                    if account.login in issue.assignees:
                        waiting.append(
                            Waiting(item=item, state=issue.state.value, shown=self._shown(github, repository, issue))
                        )
                    continue
                reviewed_it = (issue.number, account.login.lower()) in reviewed
                asked = account.login in issue.pull.requested_reviewers and not reviewed_it
                # An assignee waits until they have reviewed it and, where they may merge or close it, for as long as it
                # is open.
                acts = account.login in issue.assignees and (
                    not reviewed_it
                    or any(o.name in (MERGE, CLOSE) for o in self._pull_offers(github, repository, issue, account))
                )
                if asked or acts:
                    held = REVIEW_REQUESTED if asked else ASSIGNED
                    waiting.append(Waiting(item=item, state=held, shown=self._shown(github, repository, issue)))
        return waiting

    def _shown(self, github: GitHubWorld, repository: wire.StoredRepository, issue: wire.StoredIssue) -> str:
        """The issue as its assignee reads it, or the pull request as its reviewer does: title, state, labels, body,
        for a pull request the files it changes with their patches, and the conversation, oldest first."""
        lines = [f"{repository.full_name}#{issue.number}: {issue.title}", f"State: {issue.state.value}"]
        if issue.labels:
            lines.append(f"Labels: {', '.join(issue.labels)}")
        if issue.body:
            lines += ["", issue.body]
        if issue.pull is not None:
            lines += ["", f"{issue.author} asks to merge {issue.pull.head} into {issue.pull.base}."]
            for entry in pulls.entries_of(github, repository, issue):
                lines.append(f"{entry.status.value} {entry.path} (+{entry.counts.additions} -{entry.counts.deletions})")
                try:
                    lines.append(entry.patch())
                except NotServed:
                    continue
        for comment in github.comments(repository):
            if comment.issue == issue.number:
                lines.append(f"{comment.author}: {comment.body}")
        for review in github.reviews(repository):
            if review.pull == issue.number:
                lines.append(f"{review.author} reviewed ({review.state.value}): {review.body}")
        return "\n".join(lines)

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        """Close an open issue, reopen a closed one, for whoever can see it (authorization is out of scope: no role is
        asked). Review an open pull request, merge it where it merges cleanly and is no draft, close it."""
        del by
        github = GitHubWorld(world)
        repository, issue = locate(github, item)
        account = None if who is None else account_of(github, who)
        if account is None:
            return []
        if issue.pull is not None:
            return self._pull_offers(github, repository, issue, account)
        if reaches(github, account, repository) < 0:
            return []
        comment = OfferField(name=COMMENT, description="A comment added to the issue as it moves")
        if issue.state is wire.IssueState.OPEN:
            reason = OfferField(
                name=REASON, description=f"Why it is closed: {' or '.join(r.value for r in CLOSING_REASONS)}"
            )
            return [
                Offer(
                    name=CLOSE,
                    to_state=wire.IssueState.CLOSED.value,
                    fields=[comment, reason],
                    description="Close the issue",
                )
            ]
        return [
            Offer(name=REOPEN, to_state=wire.IssueState.OPEN.value, fields=[comment], description="Reopen the issue")
        ]

    def _pull_offers(
        self,
        github: GitHubWorld,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        account: wire.StoredAccount,
    ) -> list[Offer]:
        """The three events a review takes, for an open pull request the account can see (authorship bars none); a merge,
        for one that merges cleanly and is no draft; a close."""
        reach = reaches(github, account, repository)
        if issue.state is not wire.IssueState.OPEN or reach < 0 or issue.pull is None:
            return []
        wording = {
            wire.ReviewEvent.APPROVE: ("Approve the changes", False),
            wire.ReviewEvent.REQUEST_CHANGES: ("Ask for changes before it merges", True),
            wire.ReviewEvent.COMMENT: ("Comment without approving or asking for changes", True),
        }
        found: list[Offer] = []
        for event, (description, words_required) in wording.items():
            found.append(
                Offer(
                    name=event.value,
                    to_state=pulls.REVIEW_WORDS[pulls.REVIEW_STATES[event]],
                    fields=[OfferField(name=BODY, required=words_required, description="The text of your review")],
                    description=description,
                )
            )
        if not issue.pull.draft and pulls.merges_cleanly(github, repository, issue) is True:
            found.append(
                Offer(
                    name=MERGE,
                    to_state="merged",
                    fields=[
                        OfferField(
                            name=COMMIT_MESSAGE, description="Extra detail to append to the merge commit's message"
                        ),
                        OfferField(
                            name=COMMIT_TITLE, description="The merge commit's title, in place of the automatic one"
                        ),
                    ],
                    description="Merge the pull request",
                )
            )
        found.append(
            Offer(
                name=CLOSE,
                to_state=wire.IssueState.CLOSED.value,
                fields=[OfferField(name=COMMENT, description="A comment added to the pull request as it closes")],
                description="Close the pull request without merging it",
            )
        )
        return found

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The person moves the item as its REST route moves it: `PATCH /repos/{owner}/{repo}/issues/{number}` after
        their comment, if they wrote one, as `POST .../comments` posts it; for a pull request the review, merge or close
        routes."""
        github = GitHubWorld(world)
        repository, issue = locate(github, item)
        if who is None or by is not Actor.PERSON:
            raise ValueError("a github issue is moved by an account: name the person")
        found = next((o for o in self.legal(item, by, who, world) if o.name == offer), None)
        if found is None:
            raise ValueError(f"github issue {repository.full_name}#{issue.number} offers {who.key} no {offer!r}")
        given = content_of(content, found, who.key)
        account = account_of(github, who)
        assert account is not None
        api = GitHubApi(world, clock, self._pusher)
        if issue.pull is not None and offer in {e.value for e in wire.ReviewEvent}:
            await api.pulls.submit_review(
                repository,
                issue,
                account,
                event=wire.ReviewEvent(offer),
                body=given[BODY] if BODY in given else None,
                commit_id=None,
                actor=Actor.PERSON,
                who=who.key,
            )
        elif issue.pull is not None and offer == MERGE:
            await api.pulls.merge(
                repository,
                issue,
                account,
                title=given[COMMIT_TITLE] if COMMIT_TITLE in given else None,
                extra=given[COMMIT_MESSAGE] if COMMIT_MESSAGE in given else None,
                actor=Actor.PERSON,
                who=who.key,
                content=content,
            )
        else:
            await self._move(api, github, repository, issue, offer, given, account, who, content)
        return last_move(world, item)

    async def _move(
        self,
        api: GitHubApi,
        github: GitHubWorld,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        offer: str,
        given: dict[str, str],
        account: wire.StoredAccount,
        who: Person,
        content: str,
    ) -> None:
        """Close or reopen: the comment first, as the page posts it, then the state."""
        reason = given[REASON] if REASON in given else None
        if reason is not None and reason not in {r.value for r in CLOSING_REASONS}:
            raise ValueError(
                f"github closes an issue as {' or '.join(r.value for r in CLOSING_REASONS)}, not {reason!r}"
            )
        if COMMENT in given and given[COMMENT].strip():
            await api.tracker.post_comment(repository, issue, account, given[COMMENT], actor=Actor.PERSON)
        before = github.issue(repository, issue.number)
        assert before is not None
        to = wire.IssueState.CLOSED if offer == CLOSE else wire.IssueState.OPEN
        why = None if reason is None else wire.StateReason(reason)
        moved = api.tracker.moved(before, to, why, account, wire.timestamp(api.clock.now()))
        if before.pull is not None:
            moved = api.pulls.frozen(repository, moved)
        await api.tracker.edit_issue(
            repository, before, moved, by=account, actor=Actor.PERSON, who=who.key, content=content
        )

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """When the agent declares a target for GitHub: GitHub pushes a webhook for each move, which is a wake."""
        del item, who, world, clock
        return self._pusher is not None
