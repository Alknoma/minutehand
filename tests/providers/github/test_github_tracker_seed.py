"""The tracker a scenario starts with: labels, issues and pull requests with their comments and reviews, and branches
with commits of their own. It is read back as seeded, its ids come from names, and the seed refuses what GitHub
could never hold."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.providers.github.seed import (
    GitHubSeed,
    SeedBranch,
    SeedChange,
    SeedComment,
    SeedIssue,
    SeedLabel,
    SeedLineCommit,
    SeedPull,
    SeedRepository,
)
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from tests.providers.github.github_world import (
    SCENARIO,
    START,
    Hub,
    Json,
    body,
    github_seed,
    ledger,
    listing,
    tracker_ledger,
    tracker_seed,
)

ISSUES = "/repos/lanternworks/ledger/issues"


@pytest.fixture
def seeded() -> GitHubSeed:
    return tracker_seed()


async def test_a_seeded_issue_reads_as_seeded(hub: Hub) -> None:
    async with hub.client() as http:
        closed = body(await http.get(f"{ISSUES}/2"))
        commented = body(await http.get(f"{ISSUES}/1"))
        comments = listing(await http.get(f"{ISSUES}/1/comments"))
    assert (closed["state"], closed["state_reason"], closed["title"], closed["body"]) == (
        "closed",
        "completed",
        "Document the retry helper",
        None,
    )
    assert closed["created_at"] == "2026-08-19T10:50:03Z" and closed["closed_at"] == "2026-08-23T10:50:03Z"
    assert (
        cast(Json, closed["closed_by"])["login"] == "tomas-b" and cast(Json, closed["user"])["login"] == "iris-calder"
    )
    assert commented["updated_at"] == "2026-08-22T10:50:03Z", "the last comment's moment"
    assert [(c["body"], c["created_at"]) for c in comments] == [
        ("Looking at it.", "2026-08-21T10:50:03Z"),
        ("Thanks!", "2026-08-22T10:50:03Z"),
    ]


async def test_what_the_seed_names_has_the_same_id_in_every_world(hub: Hub, tmp_path: Path) -> None:
    async with hub.client() as http:
        issue = body(await http.get(f"{ISSUES}/1"))
        comments = listing(await http.get(f"{ISSUES}/1/comments"))
        labels = listing(await http.get("/repos/lanternworks/ledger/labels"))
    other = SqliteStore(tmp_path / "other.db", "other", RunClock(START))
    build().seed_with(tracker_seed(), SCENARIO, other)
    from minutehand.adapters.providers.github.state import GitHubWorld

    github = GitHubWorld(other)
    repository = github.repository("lanternworks", "ledger")
    assert repository is not None
    held = github.issue(repository, 1)
    assert held is not None and held.id == issue["id"]
    assert [c.id for c in github.comments(repository) if c.issue == 1] == [c["id"] for c in comments]
    assert [label.id for label in github.labels(repository)] == [label["id"] for label in labels]


def issue(number: int = 9, **changes: object) -> SeedIssue:
    base = SeedIssue(number=number, title="T", author="tomas-b", before=timedelta(hours=1))
    return base.model_copy(update=changes)


@pytest.mark.parametrize(
    ("repository", "message"),
    [
        (ledger(issues=[issue(5), issue(5)]), "share a number"),
        (
            ledger(
                issues=[issue(5)],
                pulls=[SeedPull(number=5, title="P", author="tomas-b", head="x", before=timedelta(hours=1))],
            ),
            "share a number",
        ),
        (ledger(labels=[SeedLabel(name="a", color="ffffff"), SeedLabel(name="a", color="000000")]), "share a name"),
        (ledger(issues=[issue(5, labels=["undefined"])]), "carries the label undefined"),
        (
            ledger(pulls=[SeedPull(number=5, title="P", author="tomas-b", head="nowhere", before=timedelta(hours=1))]),
            "is not in diverged_branches",
        ),
        (ledger(issues=[issue(5, state=wire.IssueState.CLOSED)]), "closed_before exactly when"),
        (ledger(issues=[issue(5, closed_before=timedelta(minutes=5))]), "closed_before exactly when"),
        (ledger(issues=[issue(5, lock_reason="spam")]), "not locked"),
        (
            ledger(
                issues=[
                    issue(5, comments=[SeedComment(author="tomas-b", body="early", before=timedelta(hours=2))]),
                ]
            ),
            "comments are oldest first",
        ),
        (ledger(issues=[issue(5, assignees=["nobody-at-all"])]), "no such user"),
        (
            ledger(
                diverged_branches=[
                    SeedBranch(
                        name="release",
                        commits=[
                            SeedLineCommit(
                                message="m",
                                author="tomas-b",
                                before=timedelta(minutes=1),
                                changes=[SeedChange(path="a", text="a")],
                            )
                        ],
                    )
                ]
            ),
            "is named twice",
        ),
        (
            ledger(
                diverged_branches=[
                    SeedBranch(
                        name="late",
                        commits=[
                            SeedLineCommit(
                                message="m",
                                author="tomas-b",
                                before=timedelta(days=9),
                                changes=[SeedChange(path="a", text="a")],
                            )
                        ],
                    )
                ]
            ),
            "made after the default branch's head",
        ),
    ],
    ids=[
        "number-twice",
        "number-in-both",
        "label-twice",
        "label-undefined",
        "pull-from-nowhere",
        "closed-without-when",
        "when-without-closed",
        "reason-without-lock",
        "comment-before-issue",
        "unknown-assignee",
        "branch-named-twice",
        "branch-before-head",
    ],
)
def test_a_seed_that_github_could_never_hold_is_refused(repository: SeedRepository, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        GitHubSeed.model_validate(github_seed().model_copy(update={"repositories": [repository]}).model_dump())


def test_a_branch_commit_that_deletes_or_replaces_what_is_not_there_is_refused_naming_it(tmp_path: Path) -> None:
    def seeded_with(change: SeedChange) -> GitHubSeed:
        branch = SeedBranch(
            name="topic",
            commits=[SeedLineCommit(message="m", author="tomas-b", before=timedelta(minutes=1), changes=[change])],
        )
        return github_seed(repositories=[ledger(diverged_branches=[branch])])

    for change, message in [
        (SeedChange(path="nowhere.md", delete=True), "deletes nowhere.md, which the branch has not"),
        (SeedChange(path="README.md", text="# Ledger\n\nThe billing and checkout services.\n"), "already has"),
        (SeedChange(path="README.md/inner.md", text="x"), "both a file and a directory"),
    ]:
        store = SqliteStore(tmp_path / f"{abs(hash(message))}.db", "w", RunClock(START))
        with pytest.raises(ValueError, match=message):
            build().seed_with(seeded_with(change), SCENARIO, store)


async def test_a_branch_made_by_the_seed_is_listed_at_its_own_commit(hub: Hub) -> None:
    async with hub.client() as http:
        branches = listing(await http.get("/repos/lanternworks/ledger/branches"))
    tips = {b["name"]: cast(Json, b["commit"])["sha"] for b in branches}
    assert set(tips) == {"main", "release", "timeout"}
    assert tips["timeout"] != tips["main"] and tips["release"] == tips["main"]
    assert tracker_ledger().diverged_branches[0].name == "timeout"


def gapped() -> GitHubSeed:
    """A seed that numbers its own issue 7 in a repository that holds no 1 to 6."""
    base = github_seed()
    notes = base.repositories[1].model_copy(update={"issues": [issue(7)]})
    return base.model_copy(update={"repositories": [base.repositories[0], notes, *base.repositories[2:]]})


@pytest.mark.parametrize("seeded", [gapped()])
async def test_a_new_issue_takes_the_number_after_the_highest_the_repository_holds(
    hub: Hub, seeded: GitHubSeed
) -> None:
    """GitHub numbers issues and pull requests from one sequence; a seed that numbers its own leaves gaps, and a new
    issue goes after the last, not after the count."""
    async with hub.client() as http:
        made = body(await http.post("/repos/iris-calder/notes/issues", json={"title": "After seven"}), 201)
    assert made["number"] == 8
