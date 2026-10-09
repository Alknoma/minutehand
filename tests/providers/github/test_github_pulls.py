"""Pull requests as an agent works them through the REST API: listed, read, opened, changed, closed and merged, with the
files and commits they carry. The branches they come from have commits of their own, laid on the default branch's
head as the seed leaves it."""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import cast

import pytest

from minutehand.adapters.providers.github import history, wire
from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.domain.world import Actor, TransitionSnapshot
from tests.providers.github.github_world import (
    CONFIG,
    IRIS,
    OUTSIDER,
    START,
    TIMEOUT,
    TOMAS,
    Hub,
    Json,
    body,
    listing,
    pulls_seed,
    refusal,
)

LEDGER = "/repos/lanternworks/ledger"
PULLS = f"{LEDGER}/pulls"
PATCH_OF_CONFIG = "@@ -1,2 +1,2 @@\n-PAYMENT_TIMEOUT = 45\n+PAYMENT_TIMEOUT = 60\n RETRY_LIMIT = 5"


@pytest.fixture
def seeded() -> GitHubSeed:
    return pulls_seed()


def numbers(found: list[Json]) -> list[object]:
    return [p["number"] for p in found]


def user(found: Json, key: str) -> object:
    return cast(Json, found[key])["login"]


# ------------------------------------------------------------------------------------------------------ reading


async def test_pull_requests_list_the_open_ones_newest_first_in_the_simple_form(hub: Hub) -> None:
    """Documented: the list answers the description's `pull-request-simple`; the default `state` is open.
    https://docs.github.com/en/rest/pulls/pulls#list-pull-requests. Recorded: the simple form has none of the counts a
    pull request got has (tests/data/github_rest/)."""
    async with hub.client() as http:
        found = listing(await http.get(PULLS))
    assert numbers(found) == [4]
    [pull] = found
    assert {"merged", "mergeable", "mergeable_state", "additions", "commits", "changed_files"}.isdisjoint(pull)
    assert (pull["state"], pull["title"], pull["draft"], pull["merged_at"]) == (
        "open",
        "Raise the payment timeout",
        False,
        None,
    )
    assert cast(Json, pull["head"])["ref"] == "timeout" and cast(Json, pull["base"])["ref"] == "main"


async def test_pull_requests_filter_by_state_head_and_base_and_sort(hub: Hub) -> None:
    """Documented: `state`, `head` (`user:ref-name`), `base`, `sort` (created, updated, popularity) and `direction`
    ("`desc` when sort is `created` or sort is not specified, otherwise `asc`")."""
    async with hub.client(TOMAS) as http:
        await http.post(PULLS, json={"title": "Second", "head": "notes", "base": "main"})
        await http.patch(f"{PULLS}/4", json={"state": "closed"})
        open_ones = listing(await http.get(PULLS))
        closed = listing(await http.get(PULLS, params={"state": "closed"}))
        both = listing(await http.get(PULLS, params={"state": "all"}))
        oldest = listing(await http.get(PULLS, params={"state": "all", "direction": "asc"}))
        by_head = listing(await http.get(PULLS, params={"state": "all", "head": "lanternworks:timeout"}))
        other_owner = listing(await http.get(PULLS, params={"state": "all", "head": "somebody:timeout"}))
        by_base = listing(await http.get(PULLS, params={"state": "all", "base": "release"}))
        popular = listing(await http.get(PULLS, params={"state": "all", "sort": "popularity"}))
        updated = listing(await http.get(PULLS, params={"state": "all", "sort": "updated"}))
    assert numbers(open_ones) == [5] and numbers(closed) == [4]
    assert numbers(both) == [5, 4] and numbers(oldest) == [4, 5]
    assert numbers(by_head) == [4] and other_owner == [] and by_base == []
    assert numbers(popular) == [5, 4], "ascending by comments: the default direction of a sort other than created"
    assert numbers(updated) == [4, 5]


async def test_pull_requests_are_paged(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        await http.post(PULLS, json={"title": "Second", "head": "notes", "base": "main"})
        first = await http.get(PULLS, params={"per_page": 1})
        second = await http.get(PULLS, params={"per_page": 1, "page": 2})
    assert numbers(listing(first)) == [5] and numbers(listing(second)) == [4]
    assert 'rel="next"' in first.headers["Link"] and 'rel="prev"' in second.headers["Link"]


async def test_the_long_running_sort_is_refused_by_name(hub: Hub) -> None:
    async with hub.client() as http:
        refused = await http.get(PULLS, params={"sort": "long-running"})
    assert refused.status_code == 501 and "long-running" in refused.json()["message"]


async def test_a_pull_request_reads_with_what_github_computes_of_it(hub: Hub) -> None:
    """Documented: `mergeable` is true, false or null, and when true `merge_commit_sha` is the test merge commit's; the
    counts are those of the diff. Recorded: an open clean pull request is `mergeable_state: "clean"`
    (tests/data/github_rest/)."""
    async with hub.client() as http:
        pull = body(await http.get(f"{PULLS}/4"))
        branches = {b["name"]: cast(Json, b["commit"])["sha"] for b in listing(await http.get(f"{LEDGER}/branches"))}
    assert (pull["number"], pull["state"], pull["merged"], pull["mergeable"], pull["mergeable_state"]) == (
        4,
        "open",
        False,
        True,
        "clean",
    )
    assert (pull["title"], pull["body"]) == ("Raise the payment timeout", "Sixty seconds is what the processor allows.")
    assert (pull["commits"], pull["changed_files"], pull["additions"], pull["deletions"]) == (1, 2, 4, 1)
    assert (pull["comments"], pull["review_comments"], pull["maintainer_can_modify"]) == (1, 0, False)
    head, base = cast(Json, pull["head"]), cast(Json, pull["base"])
    assert (head["ref"], head["label"], head["sha"]) == ("timeout", "lanternworks:timeout", branches["timeout"])
    assert (base["ref"], base["label"], base["sha"]) == ("main", "lanternworks:main", branches["main"])
    assert cast(Json, head["repo"])["full_name"] == "lanternworks/ledger" and user(head, "user") == "lanternworks"
    assert isinstance(pull["merge_commit_sha"], str) and len(pull["merge_commit_sha"]) == 40
    assert user(pull, "user") == "tomas-b" and [a["login"] for a in cast(list[Json], pull["assignees"])] == [
        "iris-calder"
    ]
    assert [a["login"] for a in cast(list[Json], pull["requested_reviewers"])] == ["iris-calder"]
    assert pull["html_url"] == "https://github.com/lanternworks/ledger/pull/4"
    assert pull["diff_url"] == "https://github.com/lanternworks/ledger/pull/4.diff"
    assert pull["issue_url"] == "https://api.github.com/repos/lanternworks/ledger/issues/4"
    assert cast(Json, pull["_links"])["self"] == {"href": "https://api.github.com/repos/lanternworks/ledger/pulls/4"}
    assert pull["author_association"] == "COLLABORATOR" and pull["draft"] is False


async def test_a_pulls_id_is_not_its_issues(hub: Hub) -> None:
    """Documented: "the `id` of a pull request returned from "Issues" endpoints will be an _issue id_"."""
    async with hub.client() as http:
        pull = body(await http.get(f"{PULLS}/4"))
        issue = body(await http.get(f"{LEDGER}/issues/4"))
    assert pull["id"] != issue["id"] and pull["number"] == issue["number"]


async def test_an_issue_is_no_pull_request_and_a_missing_one_is_404(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(await http.get(f"{PULLS}/1"), 404, "Not Found")
        refusal(await http.get(f"{PULLS}/99"), 404, "Not Found")
    async with hub.client(OUTSIDER) as http:
        refusal(await http.get(f"{PULLS}/4"), 404, "Not Found")


async def test_the_test_merge_commit_follows_the_commits_it_would_merge(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        before = body(await http.get(f"{PULLS}/4"))["merge_commit_sha"]
        again = body(await http.get(f"{PULLS}/4"))["merge_commit_sha"]
        await http.post(PULLS, json={"title": "Notes", "head": "notes", "base": "main"})
        await http.put(f"{PULLS}/5/merge", json={})
        after = body(await http.get(f"{PULLS}/4"))["merge_commit_sha"]
    assert before == again and before != after


# ------------------------------------------------------------------------------------------------------ opening


async def test_a_pull_request_the_agent_opens_is_kept_and_returned_as_sent(hub: Hub) -> None:
    """Documented: create-a-pull-request answers 201 with the pull request. Data stays as sent."""
    title = "  Describe “the notes” → メモ  "
    text = "Line one  \n\n```\ncode\n```\n\ttabbed \U0001f600\n"
    async with hub.client(TOMAS) as http:
        made = body(
            await http.post(
                PULLS,
                json={
                    "title": title,
                    "body": text,
                    "head": "notes",
                    "base": "main",
                    "draft": True,
                    "maintainer_can_modify": True,
                },
            ),
            201,
        )
        again = body(await http.get(f"{PULLS}/5"))
        issue = body(await http.get(f"{LEDGER}/issues/5"))
    assert made["title"] == title and made["body"] == text and again["title"] == title and again["body"] == text
    assert (made["number"], made["state"], made["draft"], made["maintainer_can_modify"]) == (5, "open", True, True)
    assert made["created_at"] == made["updated_at"] == "2026-08-24T10:50:03Z"
    assert (made["commits"], made["changed_files"], made["additions"], made["deletions"]) == (1, 1, 3, 0)
    assert user(made, "user") == "tomas-b" and issue["title"] == title and "pull_request" in issue


async def test_an_organization_member_may_open_one_and_a_reader_of_a_user_repository_may_not(hub: Hub) -> None:
    """Documented: "To open or update a pull request in a public repository, you must have write access to the head or
    the source branch. For organization-owned repositories, you must be a member of the organization that owns the
    repository." Iris reads the ledger as a member of its organization; Tomas only reads her public notes."""
    async with hub.client(IRIS) as http:
        member = body(await http.post(PULLS, json={"title": "Notes", "head": "notes", "base": "main"}), 201)
    async with hub.client(TOMAS) as http:
        refusal(
            await http.post("/repos/iris-calder/notes/pulls", json={"title": "Fix", "head": "fix", "base": "main"}),
            403,
            "Forbidden",
        )
    async with hub.client(IRIS) as http:
        own = body(
            await http.post("/repos/iris-calder/notes/pulls", json={"title": "Fix", "head": "fix", "base": "main"}), 201
        )
    assert member["number"] == 5 and own["number"] == 1


@pytest.mark.parametrize(
    "sent",
    [
        {},
        {"title": "T"},
        {"title": "T", "head": "notes"},
        {"head": "notes", "base": "main"},
        {"title": "", "head": "notes", "base": "main"},
    ],
    ids=["empty", "no-head", "no-base", "no-title", "blank-title"],
)
async def test_a_pull_request_missing_what_the_reference_requires_is_422_invalid_request(hub: Hub, sent: Json) -> None:
    async with hub.client(TOMAS) as http:
        refusal(await http.post(PULLS, json=sent), 422, "Invalid request")


@pytest.mark.parametrize(
    ("sent", "named"),
    [
        ({"title": "T", "head": "notes", "base": "release"}, "release"),
        ({"title": "T", "head": "release", "base": "main"}, "no commits of its own"),
        ({"title": "T", "head": "main", "base": "main"}, "no commits of its own"),
        ({"title": "T", "head": "elsewhere:notes", "base": "main"}, "another repository"),
        ({"title": "T", "head": "notes", "base": "main", "head_repo": "lanternworks/other"}, "head_repo"),
        ({"title": "T", "head": "notes", "base": "main", "issue": 1}, "issue"),
        ({"title": "T", "head": "timeout", "base": "main"}, "second open pull request"),
    ],
    ids=["other-base", "alias-head", "same-branch", "other-repository", "head-repo", "from-issue", "second"],
)
async def test_a_pull_request_github_would_make_from_what_the_world_does_not_hold_is_refused_by_name(
    hub: Hub, sent: Json, named: str
) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.post(PULLS, json=sent)
    assert refused.status_code == 501 and named in refused.json()["message"]


async def test_a_pull_request_from_a_branch_that_does_not_exist_is_refused_by_name(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.post(PULLS, json={"title": "T", "head": "nowhere", "base": "main"})
    assert refused.status_code == 501 and "nowhere" in refused.json()["message"]


async def test_an_opened_pull_request_is_the_first_move_of_its_state(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        await http.post(PULLS, json={"title": "Notes", "head": "notes", "base": "main"})
    moves = [(e.actor, e.after) for e in hub.store.events() if isinstance(e.after, TransitionSnapshot)]
    assert [(a, m.name, m.from_state, m.to_state) for a, m in moves] == [(Actor.AGENT, "open", None, "open")]


# ------------------------------------------------------------------------------------------------------ changing


async def test_a_pull_request_is_retitled_closed_and_reopened(hub: Hub) -> None:
    """Documented: update-a-pull-request takes `title`, `body`, `state` (open, closed). The change shows on the issue
    route too, a pull request being an issue."""
    async with hub.client(TOMAS) as http:
        edited = body(await http.patch(f"{PULLS}/4", json={"title": "Raise it", "body": "Sixty."}))
        hub.clock.jump(START + timedelta(hours=1))
        closed = body(await http.patch(f"{PULLS}/4", json={"state": "closed"}))
        as_issue = body(await http.get(f"{LEDGER}/issues/4"))
        reopened = body(await http.patch(f"{PULLS}/4", json={"state": "open"}))
    assert (edited["title"], edited["body"]) == ("Raise it", "Sixty.")
    assert (closed["state"], closed["closed_at"], closed["merged"], closed["merged_at"]) == (
        "closed",
        "2026-08-24T11:50:03Z",
        False,
        None,
    )
    assert as_issue["state"] == "closed" and as_issue["title"] == "Raise it"
    assert (reopened["state"], reopened["closed_at"]) == ("open", None)


async def test_a_closed_pull_request_keeps_the_commits_it_stood_between(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        open_pull = body(await http.get(f"{PULLS}/4"))
        await http.patch(f"{PULLS}/4", json={"state": "closed"})
        await http.post(PULLS, json={"title": "Notes", "head": "notes", "base": "main"})
        await http.put(f"{PULLS}/5/merge", json={})
        closed = body(await http.get(f"{PULLS}/4"))
        tip = listing(await http.get(f"{LEDGER}/commits"))[0]["sha"]
    assert cast(Json, closed["head"])["sha"] == cast(Json, open_pull["head"])["sha"]
    assert cast(Json, closed["base"])["sha"] == cast(Json, open_pull["base"])["sha"] != tip


async def test_only_those_who_may_change_a_pull_request_may(hub: Hub) -> None:
    async with hub.client(OUTSIDER) as http:
        refusal(await http.patch(f"{PULLS}/4", json={"title": "Mine"}), 404, "Not Found")
    async with hub.client(IRIS) as http:
        assert body(await http.patch(f"{PULLS}/4", json={"title": "Theirs"}))["title"] == "Theirs"


async def test_moving_a_pull_requests_base_is_refused_by_name_and_naming_it_again_is_not(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.patch(f"{PULLS}/4", json={"base": "release"})
        same = await http.patch(f"{PULLS}/4", json={"base": "main"})
    assert refused.status_code == 501 and "release" in refused.json()["message"]
    assert same.status_code == 200


# ------------------------------------------------------------------------------------------------------ what it carries


async def test_the_files_of_a_pull_request_are_listed_by_name_with_their_patches(hub: Hub) -> None:
    """Documented: list-pull-requests-files answers the description's `diff-entry`. Recorded: an added file's patch is
    `@@ -0,0 +1,N @@` and its lines, the urls name the head's sha (tests/data/github_rest/)."""
    async with hub.client() as http:
        found = listing(await http.get(f"{PULLS}/4/files"))
        head = cast(Json, body(await http.get(f"{PULLS}/4"))["head"])["sha"]
    added, modified = found
    assert (added["filename"], added["status"], added["additions"], added["deletions"], added["changes"]) == (
        "docs/timeouts.md",
        "added",
        3,
        0,
        3,
    )
    assert added["patch"] == "@@ -0,0 +1,3 @@\n+# Timeouts\n+\n+The payment timeout is 60 seconds."
    assert (modified["filename"], modified["status"], modified["changes"]) == (
        "services/billing/config.py",
        "modified",
        2,
    )
    assert modified["patch"] == PATCH_OF_CONFIG
    assert modified["blob_url"] == f"https://github.com/lanternworks/ledger/blob/{head}/services/billing/config.py"
    assert modified["raw_url"] == f"https://github.com/lanternworks/ledger/raw/{head}/services/billing/config.py"
    assert (
        modified["contents_url"]
        == f"https://api.github.com/repos/lanternworks/ledger/contents/services/billing/config.py?ref={head}"
    )
    assert added["sha"] != modified["sha"] and len(str(added["sha"])) == 40


async def test_a_removed_file_is_listed_with_its_old_sha_and_urls_at_the_base(hub: Hub) -> None:
    """Recorded: a removed file's `sha` is the blob it had, and its urls name the base's sha, where it still is
    (tests/data/github_rest/)."""
    async with hub.client(TOMAS) as http:
        made = body(await http.post(PULLS, json={"title": "Clean", "head": "cleanup", "base": "main"}), 201)
        files = listing(await http.get(f"{PULLS}/{made['number']}/files"))
        guide = (await http.get(f"{LEDGER}/contents/docs/guide.md")).json()
    removed, added = files
    assert (removed["filename"], removed["status"], removed["sha"]) == ("docs/guide.md", "removed", guide["sha"])
    assert str(removed["patch"]).startswith("@@ -1,3 +0,0 @@\n-# Operating the ledger")
    assert (removed["additions"], removed["deletions"]) == (0, 3)
    base = cast(Json, made["base"])["sha"]
    assert str(removed["blob_url"]).endswith(f"/blob/{base}/docs/guide.md")
    assert (added["filename"], added["status"]) == ("docs/moved.md", "added")
    assert (made["commits"], made["changed_files"], made["additions"], made["deletions"]) == (2, 2, 1, 3)


async def test_a_change_git_may_align_two_ways_gets_its_counts_and_no_patch_by_name(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        made = body(await http.post(PULLS, json={"title": "Tune", "head": "edgy", "base": "main"}), 201)
        files = await http.get(f"{PULLS}/{made['number']}/files")
    assert (made["additions"], made["deletions"]) == (2, 2)
    assert files.status_code == 501 and "aligned" in files.json()["message"]


async def test_a_binary_file_in_a_pull_request_is_refused_by_name(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.post(PULLS, json={"title": "Logo", "head": "logo", "base": "main"})
    assert refused.status_code == 501 and "binary" in refused.json()["message"]


async def test_the_files_are_paged(hub: Hub) -> None:
    async with hub.client() as http:
        first = await http.get(f"{PULLS}/4/files", params={"per_page": 1})
        second = await http.get(f"{PULLS}/4/files", params={"per_page": 1, "page": 2})
    assert [f["filename"] for f in listing(first)] == ["docs/timeouts.md"]
    assert [f["filename"] for f in listing(second)] == ["services/billing/config.py"]
    assert 'rel="next"' in first.headers["Link"]


async def test_the_commits_of_a_pull_request_are_listed_oldest_first(hub: Hub) -> None:
    """Recorded: a pull request's commits come oldest first, the first with the base's head as its parent
    (tests/data/github_rest/)."""
    async with hub.client(TOMAS) as http:
        made = body(await http.post(PULLS, json={"title": "Clean", "head": "cleanup", "base": "main"}), 201)
        found = listing(await http.get(f"{PULLS}/{made['number']}/commits"))
        mine = listing(await http.get(f"{PULLS}/4/commits"))
    base = cast(Json, made["base"])["sha"]
    assert [c["commit"]["message"] for c in cast(list[dict[str, Json]], found)] == [
        "Drop the guide",
        "Say where the guide went",
    ]
    assert [p["sha"] for p in cast(list[Json], found[0]["parents"])] == [base]
    assert [p["sha"] for p in cast(list[Json], found[1]["parents"])] == [found[0]["sha"]]
    assert cast(Json, found[0]["author"])["login"] == "iris-calder"
    assert [c["commit"]["message"] for c in cast(list[dict[str, Json]], mine)] == ["Raise the payment timeout"]


# ------------------------------------------------------------------------------------------------------ merging


async def test_a_pull_request_is_merged_by_a_merge_commit_of_two_parents(hub: Hub) -> None:
    """Documented: merge answers 200 with `sha`, `merged` and "Pull Request successfully merged"; the pull request is
    then merged and closed, and `merge_commit_sha` is the merge commit. Recorded: GitHub's merge commit says "Merge
    pull request #N from <owner>/<branch>" and the pull request's title below it (tests/data/github_rest/)."""
    async with hub.client(TOMAS) as http:
        before = (await http.get(f"{LEDGER}/commits")).json()[0]["sha"]
        tip = cast(Json, body(await http.get(f"{PULLS}/4"))["head"])["sha"]
        hub.clock.jump(START + timedelta(hours=1))
        merged = body(await http.put(f"{PULLS}/4/merge", json={}))
        pull = body(await http.get(f"{PULLS}/4"))
        check = await http.get(f"{PULLS}/4/merge")
        log = listing(await http.get(f"{LEDGER}/commits"))
        config = body(await http.get(f"{LEDGER}/contents/services/billing/config.py"))
        issue = body(await http.get(f"{LEDGER}/issues/4"))
    assert merged["merged"] is True and merged["message"] == "Pull Request successfully merged"
    assert (pull["state"], pull["merged"], pull["merged_at"], pull["closed_at"]) == (
        "closed",
        True,
        "2026-08-24T11:50:03Z",
        "2026-08-24T11:50:03Z",
    )
    assert user(pull, "merged_by") == "tomas-b" and pull["merge_commit_sha"] == merged["sha"]
    assert (cast(Json, pull["head"])["sha"], cast(Json, pull["base"])["sha"]) == (tip, before)
    assert check.status_code == 204 and check.content == b""
    top = cast(dict[str, Json], log[0])
    assert top["sha"] == merged["sha"]
    assert top["commit"]["message"] == "Merge pull request #4 from lanternworks/timeout\n\nRaise the payment timeout"
    assert [p["sha"] for p in cast(list[Json], top["parents"])] == [before, tip]
    assert "Raise the payment timeout" in [c["commit"]["message"] for c in cast(list[dict[str, Json]], log)]
    detail = cast(dict[str, Json], top["commit"])
    assert cast(Json, detail["author"])["name"] == "Tomas Brandt" == cast(Json, detail["committer"])["name"]
    assert base64.b64decode(str(config["content"])).decode() == TIMEOUT
    assert issue["state"] == "closed" and cast(Json, issue["pull_request"])["merged_at"] == "2026-08-24T11:50:03Z"


async def test_what_a_merge_brings_shows_on_the_default_branch_and_not_before(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        before = body(await http.get(f"{LEDGER}/contents/services/billing/config.py"))
        branch = body(await http.get(f"{LEDGER}/contents/services/billing/config.py", params={"ref": "timeout"}))
        absent = await http.get(f"{LEDGER}/contents/docs/timeouts.md")
        await http.put(f"{PULLS}/4/merge", json={})
        after = body(await http.get(f"{LEDGER}/contents/docs/timeouts.md"))
    assert base64.b64decode(str(before["content"])).decode() == CONFIG
    assert base64.b64decode(str(branch["content"])).decode() == TIMEOUT
    assert absent.status_code == 404 and after["type"] == "file"


async def test_a_merge_takes_a_title_and_extra_detail_as_the_reference_words_them(hub: Hub) -> None:
    """Documented: `commit_title` is the "Title for the automatic commit message" and `commit_message` "Extra detail to
    append to automatic commit message"."""
    async with hub.client(TOMAS) as http:
        merged = body(
            await http.put(f"{PULLS}/4/merge", json={"commit_title": "Ship it", "commit_message": "With care."})
        )
        log = listing(await http.get(f"{LEDGER}/commits", params={"per_page": 1}))
    assert cast(dict[str, Json], log[0])["commit"]["message"] == "Ship it\n\nRaise the payment timeout\n\nWith care."
    assert log[0]["sha"] == merged["sha"]


async def test_a_merge_that_cannot_be_performed_is_405_and_a_head_that_moved_409(hub: Hub) -> None:
    """Documented: 405 "if merge cannot be performed" and 409 "if sha was provided and pull request head did not match";
    the reference names no message, so the status's name stands."""
    async with hub.client(TOMAS) as http:
        tip = cast(Json, body(await http.get(f"{PULLS}/4"))["head"])["sha"]
        refusal(await http.put(f"{PULLS}/4/merge", json={"sha": "0" * 40}), 409, "Conflict")
        ok = await http.put(f"{PULLS}/4/merge", json={"sha": tip})
        twice = await http.put(f"{PULLS}/4/merge", json={})
        draft = body(await http.post(PULLS, json={"title": "D", "head": "notes", "base": "main", "draft": True}), 201)
        refusal(await http.put(f"{PULLS}/{draft['number']}/merge", json={}), 405, "Method Not Allowed")
        await http.patch(f"{PULLS}/{draft['number']}", json={"state": "closed"})
        refusal(await http.put(f"{PULLS}/{draft['number']}/merge", json={}), 405, "Method Not Allowed")
    assert ok.status_code == 200
    refusal(twice, 405, "Method Not Allowed")


async def test_a_merge_takes_write_access_and_a_pull_request_to_merge(hub: Hub) -> None:
    async with hub.client(IRIS) as http:
        refusal(await http.put(f"{PULLS}/4/merge", json={}), 403, "Forbidden")
        refusal(await http.put(f"{PULLS}/1/merge", json={}), 404, "Not Found")
    async with hub.client(OUTSIDER) as http:
        refusal(await http.put(f"{PULLS}/4/merge", json={}), 404, "Not Found")


@pytest.mark.parametrize("method", ["squash", "rebase"])
async def test_a_squash_or_a_rebase_is_refused_by_name(hub: Hub, method: str) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.put(f"{PULLS}/4/merge", json={"merge_method": method})
        still = body(await http.get(f"{PULLS}/4"))
    assert refused.status_code == 501 and method in refused.json()["message"]
    assert still["merged"] is False


async def test_a_pull_request_stays_clean_while_the_default_branch_changes_other_files(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        await http.post(PULLS, json={"title": "Notes", "head": "notes", "base": "main"})
        await http.put(f"{PULLS}/5/merge", json={})
        again = body(await http.get(f"{PULLS}/4"))
        merged = await http.put(f"{PULLS}/4/merge", json={})
        log = listing(await http.get(f"{LEDGER}/commits"))
    assert (again["mergeable"], again["mergeable_state"]) == (True, "clean")
    assert merged.status_code == 200
    messages = [str(cast(Json, c["commit"])["message"]).splitlines()[0] for c in log]
    assert messages[:2] == [
        "Merge pull request #4 from lanternworks/timeout",
        "Merge pull request #5 from lanternworks/notes",
    ]
    assert {"Describe the notes", "Raise the payment timeout"} <= set(messages)


async def test_a_file_changed_on_both_sides_is_a_mergeability_github_has_not_computed_and_a_merge_is_refused_by_name(
    hub: Hub,
) -> None:
    """Documented: "If the value is `null`, then GitHub has started a background job to compute the mergeability." A
    path changed on the default branch and by the head differently is for a merge of their lines to settle, which is
    not done: `null`, and `unknown`, GraphQL's `MergeStateStatus` for a state that "cannot currently be determined"."""
    async with hub.client(TOMAS) as http:
        other = body(
            await http.post(PULLS, json={"title": "More retries", "head": "retry-config", "base": "main"}), 201
        )
        clean = other["mergeable"]
        await http.put(f"{PULLS}/4/merge", json={})
        after = body(await http.get(f"{PULLS}/{other['number']}"))
        refused = await http.put(f"{PULLS}/{other['number']}/merge", json={})
    assert clean is True
    assert (after["mergeable"], after["mergeable_state"], after["merge_commit_sha"]) == (None, "unknown", None)
    assert refused.status_code == 501 and "same files" in refused.json()["message"]


async def test_a_merge_is_the_agents_in_the_log_and_one_move_of_the_pull_requests_state(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        await http.put(f"{PULLS}/4/merge", json={})
    moves = [(e.actor, e.after) for e in hub.store.events() if isinstance(e.after, TransitionSnapshot)]
    assert [(a, m.name, m.from_state, m.to_state) for a, m in moves] == [(Actor.AGENT, "merge", "open", "merged")]
    written = [e for e in hub.store.events() if e.entity.external_id.startswith("issue/") and e.actor is Actor.AGENT]
    assert len(written) == 1


async def test_the_branches_the_seed_made_read_at_their_own_commits(hub: Hub) -> None:
    """A branch with commits of its own shows its files at its own ref, its commits first in its history, and the
    default branch is as it was."""
    async with hub.client() as http:
        on_branch = body(await http.get(f"{LEDGER}/contents/docs/timeouts.md", params={"ref": "timeout"}))
        on_main = await http.get(f"{LEDGER}/contents/docs/timeouts.md")
        history = listing(await http.get(f"{LEDGER}/commits", params={"sha": "timeout"}))
        trunk = listing(await http.get(f"{LEDGER}/commits"))
        listed = listing(await http.get(f"{LEDGER}/branches"))
        tree = body(await http.get(f"{LEDGER}/git/trees/timeout", params={"recursive": "1"}))
        blob = body(await http.get(f"{LEDGER}/git/blobs/{on_branch['sha']}"))
        by_sha = body(await http.get(f"{LEDGER}/contents/docs/timeouts.md", params={"ref": str(history[0]["sha"])}))
    assert on_branch["type"] == "file" and on_main.status_code == 404 and by_sha["sha"] == on_branch["sha"]
    assert cast(dict[str, Json], history[0])["commit"]["message"] == "Raise the payment timeout"
    assert [c["sha"] for c in history[1:]] == [c["sha"] for c in trunk]
    assert {b["name"]: cast(Json, b["commit"])["sha"] for b in listed}["timeout"] == history[0]["sha"]
    assert "docs/timeouts.md" in [t["path"] for t in cast(list[Json], tree["tree"])]
    assert blob["sha"] == on_branch["sha"] and blob["size"] == len("# Timeouts\n\nThe payment timeout is 60 seconds.\n")


# ------------------------------------------------------------------------------------------------------ the history behind them


def commit_to(hub: Hub, branch: str | None, path: str, text: str) -> None:
    """A commit made to the world without the agent's hand: the default branch when `branch` is None."""
    world = GitHubWorld(hub.store)
    repository = world.repository("lanternworks", "ledger")
    assert repository is not None
    blob = history.stored_blob(text.encode())
    tree = history.trunk_tree(world, repository) if branch is None else None
    line = None if branch is None else history.line_named(repository, branch)
    assert (tree is not None) != (line is not None)
    before = tree if tree is not None else history.line_tree(cast(wire.StoredLine, line))
    previous = before[path] if path in before else None
    parent = repository.commits[0].sha if branch is None else history.tip(repository, branch)
    change = wire.FileChange(
        path=path,
        status=wire.ChangeStatus.ADDED if previous is None else wire.ChangeStatus.MODIFIED,
        sha=blob.sha,
        previous_sha=previous,
    )
    moment = wire.timestamp(hub.clock.now())
    commit = history.new_commit(
        repository,
        message=f"Change {path}",
        by=history.Authorship(
            "tomas-b", "Tomas Brandt", "t@example.com", moment, "tomas-b", "Tomas Brandt", "t@example.com", moment
        ),
        parent=parent,
        merged=None,
        changes=[change],
        salt=branch or "",
    )
    if branch is None:
        history.commit_to_trunk(world, repository, commit, [blob], actor=Actor.SCENARIO)
    else:
        history.commit_to_line(world, repository, branch, commit, [blob], actor=Actor.SCENARIO)


async def test_a_closed_pull_request_keeps_its_head_when_the_branch_moves_on(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        closed = body(await http.patch(f"{PULLS}/4", json={"state": "closed"}))
        commit_to(hub, "timeout", "docs/more.md", "More.\n")
        still = body(await http.get(f"{PULLS}/4"))
        files = listing(await http.get(f"{PULLS}/4/files"))
        reopened = body(await http.patch(f"{PULLS}/4", json={"state": "open"}))
    assert cast(Json, still["head"])["sha"] == cast(Json, closed["head"])["sha"]
    assert (still["commits"], still["changed_files"]) == (1, 2) and len(files) == 2
    assert (reopened["commits"], reopened["changed_files"]) == (2, 3), "reopened, it follows its branch again"


async def test_the_same_change_made_on_both_sides_still_merges_clean(hub: Hub) -> None:
    """Git merges two sides that made the very same change to a file without a conflict."""
    commit_to(hub, None, "services/billing/config.py", TIMEOUT)
    async with hub.client(TOMAS) as http:
        pull = body(await http.get(f"{PULLS}/4"))
        merged = await http.put(f"{PULLS}/4/merge", json={})
    assert (pull["mergeable"], pull["mergeable_state"]) == (True, "clean") and merged.status_code == 200


async def test_a_branch_left_behind_by_the_default_branch_has_no_commits_of_its_own_to_ask_a_merge_of(hub: Hub) -> None:
    """`release` was at the default branch's head; once a merge moves the head on, it stays where it was."""
    async with hub.client(TOMAS) as http:
        before = {b["name"]: cast(Json, b["commit"])["sha"] for b in listing(await http.get(f"{LEDGER}/branches"))}
        merged = body(await http.put(f"{PULLS}/4/merge", json={}))
        after = {b["name"]: cast(Json, b["commit"])["sha"] for b in listing(await http.get(f"{LEDGER}/branches"))}
        refused = await http.post(PULLS, json={"title": "T", "head": "release", "base": "main"})
        old = body(await http.get(f"{LEDGER}/contents/services/billing/config.py", params={"ref": "release"}))
    assert after["release"] == before["main"] != after["main"] == merged["sha"]
    assert refused.status_code == 501 and "no commits of its own" in refused.json()["message"]
    assert base64.b64decode(str(old["content"])).decode() == CONFIG, "it still shows the files it was left with"


async def test_a_branch_that_deleted_a_file_does_not_show_it_and_merging_it_removes_it_from_the_default_branch(
    hub: Hub,
) -> None:
    async with hub.client(TOMAS) as http:
        gone = await http.get(f"{LEDGER}/contents/docs/guide.md", params={"ref": "cleanup"})
        shown = listing(await http.get(f"{LEDGER}/contents/docs", params={"ref": "cleanup"}))
        made = body(await http.post(PULLS, json={"title": "Clean", "head": "cleanup", "base": "main"}), 201)
        merged = await http.put(f"{PULLS}/{made['number']}/merge", json={})
        absent = await http.get(f"{LEDGER}/contents/docs/guide.md")
        moved = body(await http.get(f"{LEDGER}/contents/docs/moved.md"))
        search = body(await http.get("/search/code", params={"q": "ledger repo:lanternworks/ledger"}))
    assert gone.status_code == 404 and [e["name"] for e in shown] == ["moved.md"]
    assert merged.status_code == 200 and absent.status_code == 404 and moved["type"] == "file"
    assert "docs/guide.md" not in [i["path"] for i in cast(list[Json], search["items"])]
