"""Vendor claims about GitHub's REST reads, each one a fact a retired home-grown emulator was tested for, carried
here so the fake keeps it. Every docstring says whether the claim is documented (with the page) or observed.
`CLAIMS.md` beside the provider is the index."""

from __future__ import annotations

import base64

import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed, SeedFile, SeedRepository
from tests.providers.github.github_world import (
    APP,
    CONFIG,
    HUGE,
    RETRY,
    START,
    Hub,
    body,
    github_seed,
    ledger,
    listing,
    refusal,
)

WIDE = SeedRepository(
    owner="iris-calder",
    name="sprawl",
    files=[SeedFile(path=f"fixtures/case_{n:04d}.txt", text=f"case {n}\n") for n in range(1040)],
)


@pytest.fixture
def seeded() -> GitHubSeed:
    base = github_seed()
    small = ledger(name="ledger-small", tree_entry_limit=4)
    return base.model_copy(update={"repositories": [*base.repositories, WIDE, small]})


# ---------------------------------------------------------------- credentials


async def test_without_any_credential_the_user_is_refused_requires_authentication(hub: Hub) -> None:
    """Documented. A call with no `Authorization` is not a bad credential: GitHub serves public data to it and
    refuses only what needs a user, `/user` among them, with 401 "Requires authentication".
    https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api
    https://docs.github.com/en/rest/users/users#get-the-authenticated-user"""
    async with hub.client(None) as http:
        refusal(await http.get("/user"), 401, "Requires authentication")
        assert body(await http.get("/repos/iris-calder/notes"))["full_name"] == "iris-calder/notes"


async def test_basic_authentication_with_a_password_is_refused_401(hub: Hub) -> None:
    """Documented: username and password are not accepted.
    https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api"""
    secret = base64.b64encode(b"iris-calder:hunter22").decode()
    async with hub.client(None, Authorization=f"Basic {secret}") as http:
        refusal(await http.get("/repos/lanternworks/ledger"), 401, "Bad credentials")


async def test_a_bearer_scheme_with_no_token_after_it_is_refused_401(hub: Hub) -> None:
    """Observed: the `Bearer` scheme with no token after it names no token, and is refused as a bad credential
    rather than served as an anonymous call."""
    async with hub.client(None, Authorization="Bearer") as http:
        refusal(await http.get("/user"), 401, "Bad credentials")


async def test_a_bearer_token_is_served_as_its_user(hub: Hub) -> None:
    """Documented: `Authorization: Bearer <token>` authenticates.
    https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api"""
    async with hub.client() as http:
        assert body(await http.get("/user"))["login"] == "iris-calder"


# ---------------------------------------------------------------- rate-limit headers


async def test_every_answer_carries_the_rate_limit_headers_for_its_budget(hub: Hub) -> None:
    """Documented: each answer says the limit, what remains, what was used, when it resets and which resource it
    spent. https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""
    async with hub.client() as http:
        answered = await http.get("/user")
        refused = await http.get("/repos/lanternworks/nothing")
    for headers in (answered.headers, refused.headers):
        assert headers["X-RateLimit-Limit"] == "5000"
        assert headers["X-RateLimit-Resource"] == "core"
        assert int(headers["X-RateLimit-Remaining"]) <= 5000
        assert int(headers["X-RateLimit-Used"]) >= 0
        assert int(headers["X-RateLimit-Reset"]) > int(START.timestamp())


async def test_an_unauthenticated_answer_carries_the_sixty_an_hour_budget(hub: Hub) -> None:
    """Documented: without a credential the primary limit is 60 an hour.
    https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""
    async with hub.client(None) as http:
        answered = await http.get("/repos/iris-calder/notes")
    assert answered.headers["X-RateLimit-Limit"] == "60"
    assert answered.headers["X-RateLimit-Resource"] == "core"


async def test_code_search_reports_its_own_ten_a_minute_budget(hub: Hub) -> None:
    """Documented: code search is its own resource, ten requests a minute.
    https://docs.github.com/en/rest/search/search#search-code"""
    async with hub.client() as http:
        answered = await http.get("/search/code", params={"q": "retry repo:lanternworks/ledger"})
    assert answered.headers["X-RateLimit-Resource"] == "code_search"
    assert answered.headers["X-RateLimit-Limit"] == "10"
    assert int(answered.headers["X-RateLimit-Reset"]) == int(START.timestamp()) + 60


# ---------------------------------------------------------------- repositories


@pytest.mark.parametrize(
    "path",
    [
        "/repos/someone/elsewhere",
        "/repos/someone/elsewhere/languages",
        "/repos/someone/elsewhere/branches",
        "/repos/someone/elsewhere/contents/README.md",
        "/repos/someone/elsewhere/commits",
        "/repos/someone/elsewhere/git/trees/main",
        f"/repos/someone/elsewhere/git/blobs/{'a' * 40}",
    ],
)
async def test_an_unknown_repository_is_not_found_on_every_route(hub: Hub, path: str) -> None:
    """Documented: each of these reads lists 404 for a repository that is not there (or not visible).
    https://docs.github.com/en/rest/repos/repos#get-a-repository"""
    async with hub.client() as http:
        refusal(await http.get(path), 404, "Not Found")


async def test_user_repos_lists_only_what_the_token_can_see_with_its_permissions(hub: Hub) -> None:
    """Documented: the list is the repositories the authenticated user can reach, each with the caller's
    `permissions`. https://docs.github.com/en/rest/repos/repos#list-repositories-for-the-authenticated-user"""
    async with hub.client("github_pat_tomas_selects_only_the_ledger_0000000000000000") as http:
        repos = listing(await http.get("/user/repos", params={"per_page": 30, "sort": "updated"}))
    assert [r["full_name"] for r in repos] == ["lanternworks/ledger"]
    permissions = repos[0]["permissions"]
    assert isinstance(permissions, dict) and permissions["pull"] is True


async def test_languages_counts_bytes_of_code_leaving_out_prose_and_vendored_files(hub: Hub) -> None:
    """Documented: an object of language name to bytes of code in that language.
    https://docs.github.com/en/rest/repos/repos#list-repository-languages
    Observed: prose (Markdown) and vendored paths are not counted, as linguist does not count them."""
    async with hub.client() as http:
        languages = body(await http.get("/repos/lanternworks/ledger/languages"))
    assert languages == {"Python": len(RETRY) + len(CONFIG), "TypeScript": len(APP)}
    assert list(languages) == ["Python", "TypeScript"]


async def test_branches_list_each_branch_with_its_commit(hub: Hub) -> None:
    """Documented: each branch has its name, the commit it points at and whether it is protected.
    https://docs.github.com/en/rest/branches/branches#list-branches"""
    async with hub.client() as http:
        head = listing(await http.get("/repos/lanternworks/ledger/commits", params={"per_page": 1}))[0]["sha"]
        branches = listing(await http.get("/repos/lanternworks/ledger/branches"))
    assert [b["name"] for b in branches] == ["main", "release"]
    assert all(b["commit"]["sha"] == head for b in branches)  # type: ignore[index]
    assert all(b["protected"] is False for b in branches)


# ---------------------------------------------------------------- contents


async def test_contents_at_a_ref_that_names_no_commit_is_refused_404(hub: Hub) -> None:
    """Observed: the 404 says no commit was found for the ref.
    https://docs.github.com/en/rest/repos/contents#get-repository-content (404 listed)"""
    async with hub.client() as http:
        refusal(
            await http.get("/repos/lanternworks/ledger/contents/README.md", params={"ref": "no-such-branch"}),
            404,
            "No commit found for the ref no-such-branch",
        )


async def test_contents_takes_a_branch_or_a_commit_sha_as_its_ref(hub: Hub) -> None:
    """Documented: `ref` is a commit sha, a branch or a tag.
    https://docs.github.com/en/rest/repos/contents#get-repository-content"""
    async with hub.client() as http:
        oldest = listing(await http.get("/repos/lanternworks/ledger/commits"))[-1]["sha"]
        by_branch = body(await http.get("/repos/lanternworks/ledger/contents/README.md", params={"ref": "release"}))
        by_sha = body(await http.get("/repos/lanternworks/ledger/contents/README.md", params={"ref": str(oldest)}))
    assert by_branch["encoding"] == "base64" and by_sha["encoding"] == "base64"


async def test_a_file_over_a_mebibyte_answers_no_content_and_encoding_none(hub: Hub) -> None:
    """Documented: between 1 MB and 100 MB the content is empty and the encoding is `none`.
    https://docs.github.com/en/rest/repos/contents#get-repository-content"""
    async with hub.client() as http:
        found = body(await http.get("/repos/lanternworks/ledger/contents/vendor/bundle.min.js"))
    size = found["size"]
    assert isinstance(size, int) and size > 1024 * 1024
    assert (found["content"], found["encoding"]) == ("", "none")


async def test_the_blob_endpoint_carries_the_bytes_the_contents_endpoint_left_out(hub: Hub) -> None:
    """Documented: the blob read returns the object's bytes as base64.
    https://docs.github.com/en/rest/git/blobs#get-a-blob"""
    async with hub.client() as http:
        contents = body(await http.get("/repos/lanternworks/ledger/contents/vendor/bundle.min.js"))
        blob = body(await http.get(f"/repos/lanternworks/ledger/git/blobs/{contents['sha']}"))
    encoded = blob["content"]
    assert blob["encoding"] == "base64" and blob["size"] == contents["size"]
    assert isinstance(encoded, str) and base64.b64decode(encoded.replace("\n", "")).decode() == HUGE


async def test_a_directory_listing_stops_at_a_thousand_entries_and_says_nothing(hub: Hub) -> None:
    """Documented: the contents endpoint lists at most 1,000 files of a directory; the trees API is the way past.
    https://docs.github.com/en/rest/repos/contents#get-repository-content"""
    async with hub.client() as http:
        listed = await http.get("/repos/iris-calder/sprawl/contents/fixtures")
    assert len(listing(listed)) == 1000
    assert "Link" not in listed.headers


# ---------------------------------------------------------------- commits


async def test_commits_from_a_sha_that_names_no_commit_are_refused_404(hub: Hub) -> None:
    """Observed: the 404 says no commit was found for the SHA.
    https://docs.github.com/en/rest/commits/commits#list-commits (404 listed)"""
    async with hub.client() as http:
        refusal(
            await http.get("/repos/lanternworks/ledger/commits", params={"sha": "0badc0de"}),
            404,
            "No commit found for SHA: 0badc0de",
        )


async def test_commits_filter_by_path(hub: Hub) -> None:
    """Documented: `path` keeps the commits that touched it.
    https://docs.github.com/en/rest/commits/commits#list-commits"""
    async with hub.client() as http:
        commits = listing(await http.get("/repos/lanternworks/ledger/commits", params={"path": "web"}))
    assert [c["commit"]["message"] for c in commits] == ["Add the checkout button"]  # type: ignore[index]


async def test_commits_honour_per_page(hub: Hub) -> None:
    """Documented: `per_page` sets the page size (up to 100).
    https://docs.github.com/en/rest/commits/commits#list-commits"""
    async with hub.client() as http:
        assert len(listing(await http.get("/repos/lanternworks/ledger/commits", params={"per_page": 2}))) == 2


async def test_a_listed_commit_carries_its_author_and_link_and_no_file_list(hub: Hub) -> None:
    """Observed: the list carries each commit's author (with email) and `html_url`; the changed files come only
    from reading one commit. https://docs.github.com/en/rest/commits/commits#list-commits"""
    async with hub.client() as http:
        commits = listing(await http.get("/repos/lanternworks/ledger/commits"))
    for commit in commits:
        assert "files" not in commit
        detail = commit["commit"]
        assert isinstance(detail, dict) and detail["author"]["email"]
        assert str(commit["html_url"]).startswith("https://github.com/lanternworks/ledger/commit/")


# ---------------------------------------------------------------- trees


async def test_a_tree_without_recursive_is_one_level_deep(hub: Hub) -> None:
    """Documented: without `recursive` only the tree's own entries are returned.
    https://docs.github.com/en/rest/git/trees#get-a-tree"""
    async with hub.client() as http:
        tree = body(await http.get("/repos/lanternworks/ledger/git/trees/main"))
    entries = tree["tree"]
    assert isinstance(entries, list)
    assert sorted(e["path"] for e in entries) == ["README.md", "assets", "docs", "services", "vendor", "web"]
    assert tree["truncated"] is False


async def test_a_recursive_tree_holds_every_object(hub: Hub) -> None:
    """Documented: `recursive` returns objects and subtrees all the way down.
    https://docs.github.com/en/rest/git/trees#get-a-tree"""
    async with hub.client() as http:
        tree = body(await http.get("/repos/lanternworks/ledger/git/trees/main", params={"recursive": "true"}))
    paths = [e["path"] for e in tree["tree"]]  # type: ignore[union-attr]
    assert {"services", "services/billing", "services/billing/retry.py", "web/app.ts"} <= set(paths)


async def test_a_tree_at_a_ref_that_names_nothing_is_not_found(hub: Hub) -> None:
    """Documented: an unknown tree sha or ref name is a 404.
    https://docs.github.com/en/rest/git/trees#get-a-tree"""
    async with hub.client() as http:
        refusal(await http.get("/repos/lanternworks/ledger/git/trees/no-such-branch"), 404, "Not Found")


async def test_a_tree_past_its_entry_limit_is_answered_partial_and_marked_truncated(hub: Hub) -> None:
    """Documented: past the limit the tree array is cut and `truncated` is true.
    https://docs.github.com/en/rest/git/trees#get-a-tree"""
    async with hub.client() as http:
        tree = body(await http.get("/repos/lanternworks/ledger-small/git/trees/main", params={"recursive": "1"}))
    assert tree["truncated"] is True
    assert len(tree["tree"]) == 4  # type: ignore[arg-type]
