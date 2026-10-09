"""People act on GitHub issues through the one port (`docs/design-transitions.md`): an open issue assigned to a person
is pending on them; at their moment they close it, with their comment and the reason GitHub takes, as themselves, and
the move is recorded once as theirs. Nobody is offered a move GitHub would refuse them."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.adapters.providers.github.transitions import GitHubTransitions
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, Scenario, Take
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation, PendingStatus, TransitionSnapshot
from minutehand.ports.store import Store
from tests.providers.github.github_world import PEOPLE, SCENARIO, START, pulls_seed, tracker_seed
from tests.support.people import people_engine, people_model

IRIS, TOMAS = PEOPLE


def _played(take: str, *, nth: int | None = None, verbatim: str | None = "Handled.") -> Scenario:
    """The scenario with the engine playing GitHub, and Tomas's first move pinned to `take` two hours after it waits."""
    pinned = Take(provider="github", take=take, nth=nth, after=timedelta(hours=2), verbatim=verbatim)
    people = [p.model_copy(update={"takes": [pinned]}) if p.key == "tomas" else p for p in SCENARIO.people]
    return SCENARIO.model_copy(update={"transitions_on": ["github"], "people": people})


def _world(tmp_path: Path, scenario: Scenario) -> tuple[GitHubTransitions, SqliteStore, RunClock]:
    """The GitHub the scenario starts with and, for an agent that declares no inbound target for it, the port people act
    through."""
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = build()
    provider.seed_with(tracker_seed(), scenario, store)
    return provider.talking(None, None), store, clock


def _moves(store: Store) -> list[TransitionSnapshot]:
    return [e.after for e in store.events() if isinstance(e.after, TransitionSnapshot)]


def _ref(number: int) -> EntityRef:
    return EntityRef(provider="github", kind=EntityKind.RECORD, external_id=f"issue/lanternworks/ledger/{number:010d}")


async def test_an_open_issue_assigned_to_a_person_is_pending_and_a_pinned_close_lands_as_theirs(tmp_path: Path) -> None:
    scenario = _played("close")
    provider, store, clock = _world(tmp_path, scenario)
    engine = people_engine(scenario, {"github": provider}, people_model())

    # Issue 3 is Tomas's; issue 1 is Iris's, who cannot close it (below); issue 2 is closed; the pull request asks
    # Iris for a review, and is no assignment of anyone's here. Mutation: dropping the open-state or the pull request
    # filter books more.
    booked = (await engine.look(store, clock)).booked
    assert [(b.person, b.item.external_id) for b in booked] == [
        ("iris", "issue/lanternworks/ledger/0000000001"),
        ("iris", "issue/lanternworks/ledger/0000000004"),
        ("tomas", "issue/lanternworks/ledger/0000000003"),
    ]
    [mine] = [b for b in booked if b.person == "tomas"]
    assert mine.at == START + timedelta(hours=2)
    assert [o.name for o in provider.legal(mine.item, Actor.PERSON, TOMAS, store)] == ["close"]

    clock.jump(START + timedelta(hours=2))
    acted = await engine.act(mine.pending, store, clock)

    assert acted.transition is not None
    [moved] = [m for m in _moves(store) if m.who == "tomas"]
    assert (moved.name, moved.from_state, moved.to_state) == ("close", "open", "closed")
    assert json.loads(moved.content) == {"comment": "Handled."}
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    assert repository is not None
    issue = github.issue(repository, 3)
    assert issue is not None and issue.state is wire.IssueState.CLOSED
    assert (issue.closed_by, issue.closed_at, issue.state_reason) == ("tomas-b", "2026-08-24T12:50:03Z", None)
    comments = [c for c in github.comments(repository) if c.issue == 3]
    assert [(c.author, c.body) for c in comments] == [("tomas-b", "Handled.")]
    assert engine.pending(mine.pending, store).status is PendingStatus.ACTED
    assert provider.items_for(TOMAS, store) == [], "closed: it no longer waits on them"
    people = [e for e in store.events() if e.entity.external_id.startswith("issue/") and e.actor is Actor.PERSON]
    assert len(people) == 2, "the comment moves the issue's update time, then the close"


def test_nobody_is_offered_a_move_github_would_refuse_them(tmp_path: Path) -> None:
    """Documented: "Issue owners and users with push access or Triage role can edit an issue".
    https://docs.github.com/en/rest/issues/issues#update-an-issue. Iris reads the ledger as an organization member and
    did not open issue 1; Tomas pushes."""
    provider, store, _ = _world(tmp_path, SCENARIO)
    assert provider.legal(_ref(1), Actor.PERSON, IRIS, store) == []
    assert [o.name for o in provider.legal(_ref(1), Actor.PERSON, TOMAS, store)] == ["close"]
    # Mutation: dropping the role check offers Iris the close.
    assert [o.name for o in provider.legal(_ref(3), Actor.PERSON, IRIS, store)] == ["close"], "her own issue"
    assert [o.name for o in provider.legal(_ref(2), Actor.PERSON, TOMAS, store)] == ["reopen"]
    assert [o.name for o in provider.legal(_ref(4), Actor.PERSON, TOMAS, store)] == [
        "COMMENT",
        "merge",
        "close",
    ], "his own pull request: he may comment on it, and merge or close it, but not approve it"
    assert [o.name for o in provider.legal(_ref(4), Actor.PERSON, IRIS, store)] == [
        "APPROVE",
        "REQUEST_CHANGES",
        "COMMENT",
    ], "a reader reviews a pull request; she cannot merge it (the merge route takes write access) or close it"


async def test_a_close_names_why_it_was_closed_and_only_as_github_takes(tmp_path: Path) -> None:
    provider, store, clock = _world(tmp_path, SCENARIO)
    content = json.dumps({"comment": "Not doing this.", "state_reason": "not_planned"})
    moved = await provider.apply(_ref(3), "close", Actor.PERSON, TOMAS, content, store, clock)
    assert (moved.name, moved.to_state, moved.who, moved.by) == ("close", "closed", "tomas", Actor.PERSON)
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    assert repository is not None
    issue = github.issue(repository, 3)
    assert issue is not None and issue.state_reason is wire.StateReason.NOT_PLANNED
    with pytest.raises(ValueError, match="offers tomas no 'close'"):
        await provider.apply(_ref(3), "close", Actor.PERSON, TOMAS, "{}", store, clock)


@pytest.mark.parametrize(
    ("content", "complaint"),
    [
        (json.dumps({"state_reason": "duplicate"}), "completed or not_planned"),
        (json.dumps({"labels": "bug"}), "takes no such field"),
    ],
    ids=["duplicate", "unknown-field"],
)
async def test_a_close_with_a_reason_or_field_github_does_not_take_here_is_refused(
    tmp_path: Path, content: str, complaint: str
) -> None:
    provider, store, clock = _world(tmp_path, SCENARIO)
    with pytest.raises(ValueError, match=complaint):
        await provider.apply(_ref(3), "close", Actor.PERSON, TOMAS, content, store, clock)
    assert _moves(store) == []


async def test_a_reopen_puts_the_issue_back_and_clears_who_closed_it(tmp_path: Path) -> None:
    provider, store, clock = _world(tmp_path, SCENARIO)
    moved = await provider.apply(
        _ref(2), "reopen", Actor.PERSON, TOMAS, json.dumps({"comment": "Again."}), store, clock
    )
    assert (moved.from_state, moved.to_state) == ("closed", "open")
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    assert repository is not None
    issue = github.issue(repository, 2)
    assert issue is not None
    assert (issue.state, issue.closed_at, issue.closed_by, issue.state_reason) == (
        wire.IssueState.OPEN,
        None,
        None,
        None,
    )


def _reviewing(take: str, verbatim: str | None) -> Scenario:
    """The scenario with the engine playing GitHub, and Iris's moves pinned to `take` two hours after an item waits on
    her; the issue that waits on her cannot be approved, and only the pull request that asks her review is moved."""
    pinned = Take(provider="github", take=take, after=timedelta(hours=2), verbatim=verbatim)
    people = [p.model_copy(update={"takes": [pinned]}) if p.key == "iris" else p for p in SCENARIO.people]
    return SCENARIO.model_copy(update={"transitions_on": ["github"], "people": people})


async def test_a_pull_request_that_asks_a_review_is_pending_on_the_reviewer_and_a_pinned_approval_lands_as_theirs(
    tmp_path: Path,
) -> None:
    scenario = _reviewing("approve", "Looks right to me.")
    provider, store, clock = _world(tmp_path, scenario)
    engine = people_engine(scenario, {"github": provider}, people_model())

    booked = {b.item.external_id: b for b in (await engine.look(store, clock)).booked if b.person == "iris"}
    mine = booked["issue/lanternworks/ledger/0000000004"]
    assert [o.name for o in provider.legal(mine.item, Actor.PERSON, IRIS, store)] == [
        "APPROVE",
        "REQUEST_CHANGES",
        "COMMENT",
    ]
    clock.jump(START + timedelta(hours=2))
    acted = await engine.act(mine.pending, store, clock)

    assert acted.transition is not None
    [moved] = [m for m in _moves(store) if m.who == "iris"]
    assert (moved.name, moved.from_state, moved.to_state) == ("APPROVE", "open", "approved")
    assert json.loads(moved.content) == {"body": "Looks right to me."}
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    assert repository is not None
    [review] = [r for r in github.reviews(repository) if r.author == "iris-calder"]
    assert (review.pull, review.state, review.body) == (4, wire.ReviewState.APPROVED, "Looks right to me.")
    assert review.submitted_at == "2026-08-24T12:50:03Z"
    assert engine.pending(mine.pending, store).status is PendingStatus.ACTED
    assert [w.item.external_id for w in provider.items_for(IRIS, store)] == ["issue/lanternworks/ledger/0000000001"], (
        "reviewed: it no longer waits on her"
    )
    people = [e for e in store.events() if e.entity.external_id.startswith("review/") and e.actor is Actor.PERSON]
    assert len(people) == 1


async def test_a_review_that_asks_for_changes_needs_the_words_and_a_comment_review_too(tmp_path: Path) -> None:
    provider, store, clock = _world(tmp_path, SCENARIO)
    for event in ("REQUEST_CHANGES", "COMMENT"):
        with pytest.raises(ValueError, match=r"without body, which it requires"):
            await provider.apply(_ref(4), event, Actor.PERSON, IRIS, "{}", store, clock)
    approved = await provider.apply(_ref(4), "APPROVE", Actor.PERSON, IRIS, "{}", store, clock)
    assert (approved.name, approved.to_state, approved.who) == ("APPROVE", "approved", "iris")
    asked = await provider.apply(
        _ref(4), "REQUEST_CHANGES", Actor.PERSON, IRIS, json.dumps({"body": "Needs a test."}), store, clock
    )
    assert asked.to_state == "changes requested"


async def test_a_pull_request_closed_or_reviewed_offers_its_reviewer_nothing_more_to_ask(tmp_path: Path) -> None:
    provider, store, clock = _world(tmp_path, SCENARIO)
    await provider.apply(_ref(4), "COMMENT", Actor.PERSON, IRIS, json.dumps({"body": "Here."}), store, clock)
    assert provider.items_for(IRIS, store)[0].item.external_id.endswith("0000000001"), "her review ends the wait"
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    pull = github.issue(repository, 4) if repository is not None else None
    assert repository is not None and pull is not None
    github.put_issue(
        repository,
        pull.model_copy(update={"state": wire.IssueState.CLOSED}),
        operation=Operation.UPDATE,
        actor=Actor.SCENARIO,
    )
    assert provider.legal(_ref(4), Actor.PERSON, IRIS, store) == [], "a closed pull request is not reviewed"


async def test_a_person_whose_account_cannot_read_the_repository_is_offered_no_review_and_nothing_waits_on_them(
    tmp_path: Path,
) -> None:
    visitor = Person(key="visitor", name="Visiting Contractor", email="visitor@example.com")
    scenario = SCENARIO.model_copy(update={"people": [*SCENARIO.people, visitor]})
    seed = tracker_seed()
    users = [
        u.model_copy(update={"person": "visitor", "name": None}) if u.login == "outsider" else u for u in seed.users
    ]
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = build()
    provider.seed_with(seed.model_copy(update={"users": users}), scenario, store)
    port = provider.talking(None, None)
    assert port.legal(_ref(4), Actor.PERSON, visitor, store) == [], "a private repository is a 404 to them"
    assert port.items_for(visitor, store) == []
    stranger = Person(key="stranger", name="Nobody", email="stranger@example.com")
    assert port.legal(_ref(4), Actor.PERSON, stranger, store) == [], "no account on GitHub"
    assert port.items_for(stranger, store) == []


def _assigned_to_tomas() -> GitHubSeed:
    """The ledger's pull request assigned to Tomas, who pushes, in place of Iris, who only reads."""
    seed = pulls_seed()
    ledger = seed.repositories[0]
    pull = ledger.pulls[0].model_copy(update={"assignees": ["tomas-b"], "requested_reviewers": []})
    mine = ledger.model_copy(update={"pulls": [pull]})
    return seed.model_copy(update={"repositories": [mine, *seed.repositories[1:]]})


def _held(tmp_path: Path, seed: GitHubSeed, scenario: Scenario) -> tuple[GitHubTransitions, SqliteStore, RunClock]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = build()
    provider.seed_with(seed, scenario, store)
    return provider.talking(None, None), store, clock


async def test_a_pull_request_assigned_to_a_person_who_may_merge_it_waits_on_them_and_a_pinned_merge_lands_as_theirs(
    tmp_path: Path,
) -> None:
    """Documented: the merge route takes write access and merges a pull request that merges cleanly.
    https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request"""
    scenario = _played("merge", verbatim="With care.")
    port, store, clock = _held(tmp_path, _assigned_to_tomas(), scenario)
    engine = people_engine(scenario, {"github": port}, people_model())

    booked = {b.item.external_id: b for b in (await engine.look(store, clock)).booked if b.person == "tomas"}
    assert sorted(booked) == ["issue/lanternworks/ledger/0000000003", "issue/lanternworks/ledger/0000000004"]
    [waiting] = [w for w in port.items_for(TOMAS, store) if w.item.external_id.endswith("4")]
    assert (
        waiting.state == "assigned"
        and "services/billing/config.py" in waiting.shown
        and "+PAYMENT_TIMEOUT = 60" in waiting.shown
    )
    clock.jump(START + timedelta(hours=2))
    acted = await engine.act(booked["issue/lanternworks/ledger/0000000004"].pending, store, clock)

    assert acted.transition is not None
    [moved] = [m for m in _moves(store) if m.who == "tomas" and m.name == "merge"]
    assert (moved.from_state, moved.to_state) == ("open", "merged")
    assert json.loads(moved.content) == {"commit_message": "With care."}
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    assert repository is not None
    pull = github.issue(repository, 4)
    assert pull is not None and pull.pull is not None and pull.pull.merged and pull.state is wire.IssueState.CLOSED
    assert (pull.pull.merged_by, pull.closed_by) == ("tomas-b", "tomas-b")
    assert repository.commits[0].message == (
        "Merge pull request #4 from lanternworks/timeout\n\nRaise the payment timeout\n\nWith care."
    )
    assert [w.item.external_id for w in port.items_for(TOMAS, store)] == ["issue/lanternworks/ledger/0000000003"]


async def test_a_pull_request_closed_by_a_person_is_closed_not_merged_with_their_comment(tmp_path: Path) -> None:
    port, store, clock = _held(tmp_path, pulls_seed(), SCENARIO)
    moved = await port.apply(_ref(4), "close", Actor.PERSON, TOMAS, json.dumps({"comment": "Not now."}), store, clock)
    assert (moved.name, moved.from_state, moved.to_state, moved.who) == ("close", "open", "closed", "tomas")
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    assert repository is not None
    pull = github.issue(repository, 4)
    assert pull is not None and pull.pull is not None
    assert (pull.state, pull.pull.merged, pull.closed_by) == (wire.IssueState.CLOSED, False, "tomas-b")
    assert pull.pull.head_sha is not None and pull.pull.base_sha is not None, "a closed pull request keeps its commits"
    assert [c.body for c in github.comments(repository) if c.issue == 4] == ["On it.", "Not now."]
    assert repository.commits[0].message == "Add the checkout button", "nothing was merged"


async def test_a_draft_or_conflicting_pull_request_is_not_offered_a_merge(tmp_path: Path) -> None:
    seed = pulls_seed()
    ledger = seed.repositories[0]
    draft = ledger.pulls[0].model_copy(update={"draft": True})
    drafted = seed.model_copy(
        update={"repositories": [ledger.model_copy(update={"pulls": [draft]}), *seed.repositories[1:]]}
    )
    port, store, clock = _held(tmp_path / "draft", drafted, SCENARIO)
    assert "merge" not in [o.name for o in port.legal(_ref(4), Actor.PERSON, TOMAS, store)], "a draft"
    port, store, clock = _held(tmp_path / "clean", pulls_seed(), SCENARIO)
    assert "merge" in [o.name for o in port.legal(_ref(4), Actor.PERSON, TOMAS, store)]
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    assert repository is not None
    rival = wire.StoredIssue(
        number=5,
        id=5,
        title="More retries",
        body=None,
        author="tomas-b",
        created_at="2026-08-24T10:50:03Z",
        updated_at="2026-08-24T10:50:03Z",
        pull=wire.StoredPull(id=5, head="retry-config", base="main"),
    )
    github.put_issue(repository, rival, operation=Operation.CREATE, actor=Actor.SCENARIO)
    assert "merge" in [o.name for o in port.legal(_ref(5), Actor.PERSON, TOMAS, store)]
    await port.apply(_ref(4), "merge", Actor.PERSON, TOMAS, "{}", store, clock)
    assert "merge" not in [o.name for o in port.legal(_ref(5), Actor.PERSON, TOMAS, store)], "both changed the config"


async def test_a_merge_the_pull_request_cannot_make_is_refused(tmp_path: Path) -> None:
    port, store, clock = _held(tmp_path, pulls_seed(), SCENARIO)
    with pytest.raises(ValueError, match="offers iris no 'merge'"):
        await port.apply(_ref(4), "merge", Actor.PERSON, IRIS, "{}", store, clock)
    await port.apply(_ref(4), "merge", Actor.PERSON, TOMAS, "{}", store, clock)
    with pytest.raises(ValueError, match="offers tomas no 'merge'"):
        await port.apply(_ref(4), "merge", Actor.PERSON, TOMAS, "{}", store, clock)
