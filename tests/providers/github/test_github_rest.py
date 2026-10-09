"""Each REST read a code-reading client makes, through the proxy, with the parameters it sends."""

from __future__ import annotations

import base64

from minutehand.domain.world import Actor, Operation
from tests.providers.github.github_world import APP, CONFIG, HUGE, LOGO, RETRY, Hub, body, listing, refusal


async def test_the_token_answers_as_its_user(hub: Hub) -> None:
    async with hub.client() as http:
        me = await http.get("/user")
    found = body(me)
    assert (found["login"], found["name"], found["email"], found["type"]) == (
        "iris-calder",
        "Iris Calder",
        "iris@example.com",
        "User",
    )
    assert me.headers["X-OAuth-Scopes"] == "repo"
    assert me.headers["X-GitHub-Api-Version-Selected"] == "2022-11-28"


async def test_user_repos_lists_what_the_user_owns_collaborates_on_or_reaches_through_an_organization(hub: Hub) -> None:
    async with hub.client() as http:
        iris = listing(await http.get("/user/repos", params={"per_page": 30, "sort": "updated"}))
    async with hub.client("ghp_outsider000000000000000000000000000000") as http:
        outsider = listing(await http.get("/user/repos", params={"per_page": 30, "sort": "updated"}))
    assert {r["full_name"] for r in iris} == {
        "lanternworks/ledger",
        "lanternworks/plans",
        "iris-calder/notes",
        "iris-calder/empty",
    }
    assert iris[-1]["full_name"] == "lanternworks/ledger"  # pushed five hours before the others
    ledger = next(r for r in iris if r["full_name"] == "lanternworks/ledger")
    assert ledger["permissions"] == {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}
    assert outsider == []


async def test_user_repos_are_paged_by_link_header(hub: Hub) -> None:
    async with hub.client() as http:
        first = await http.get("/user/repos", params={"per_page": 3})
        second = await http.get("/user/repos", params={"per_page": 3, "page": 2})
    assert len(listing(first)) == 3 and len(listing(second)) == 1
    assert 'page=2>; rel="next"' in first.headers["Link"]
    assert 'page=1>; rel="prev"' in second.headers["Link"]


async def test_a_repository_reads_with_its_metadata_and_the_callers_permissions(hub: Hub) -> None:
    async with hub.client("github_pat_tomas_selects_only_the_ledger_0000000000000000") as http:
        found = body(await http.get("/repos/lanternworks/ledger"))
    assert found["full_name"] == "lanternworks/ledger" and found["private"] is True
    assert found["permissions"] == {"admin": False, "maintain": False, "push": True, "triage": True, "pull": True}
    assert (found["language"], found["default_branch"], found["topics"]) == ("Python", "main", ["billing", "payments"])
    assert found["license"] is not None and found["visibility"] == "private"


async def test_a_file_comes_back_base64_wrapped_in_sixty_character_lines(hub: Hub) -> None:
    async with hub.client() as http:
        found = body(
            await http.get("/repos/lanternworks/ledger/contents/services/billing/retry.py", params={"ref": "HEAD"})
        )
    encoded = found["content"]
    assert isinstance(encoded, str)
    assert all(len(line) <= 60 for line in encoded.splitlines())
    assert base64.b64decode(encoded.replace("\n", "")).decode() == RETRY
    assert (found["encoding"], found["size"], found["type"]) == ("base64", len(RETRY), "file")


async def test_a_file_over_a_mebibyte_carries_no_content_and_its_blob_does(hub: Hub) -> None:
    async with hub.client() as http:
        contents = body(
            await http.get("/repos/lanternworks/ledger/contents/vendor/bundle.min.js", params={"ref": "HEAD"})
        )
        blob = body(await http.get(f"/repos/lanternworks/ledger/git/blobs/{contents['sha']}"))
    assert (contents["content"], contents["encoding"]) == ("", "none")
    encoded = blob["content"]
    assert isinstance(encoded, str) and base64.b64decode(encoded.replace("\n", "")).decode() == HUGE


async def test_a_directory_lists_its_entries_dirs_and_files(hub: Hub) -> None:
    async with hub.client() as http:
        root = listing(await http.get("/repos/lanternworks/ledger/contents"))
        services = listing(
            await http.get("/repos/lanternworks/ledger/contents/services/billing", params={"ref": "main"})
        )
    assert [(e["name"], e["type"]) for e in root] == [
        ("README.md", "file"),
        ("assets", "dir"),
        ("docs", "dir"),
        ("services", "dir"),
        ("vendor", "dir"),
        ("web", "dir"),
    ]
    assert [(e["path"], e["size"]) for e in services] == [
        ("services/billing/config.py", len(CONFIG)),
        ("services/billing/retry.py", len(RETRY)),
    ]
    assert all("content" not in e for e in services)


async def test_the_recursive_tree_holds_every_object_and_any_recursive_value_recurses(hub: Hub) -> None:
    async with hub.client() as http:
        head = listing(await http.get("/repos/lanternworks/ledger/commits", params={"per_page": 1}))[0]["sha"]
        tree = body(await http.get(f"/repos/lanternworks/ledger/git/trees/{head}", params={"recursive": "1"}))
        also = body(await http.get(f"/repos/lanternworks/ledger/git/trees/{head}", params={"recursive": "0"}))
        flat = body(await http.get(f"/repos/lanternworks/ledger/git/trees/{head}"))
    paths = [e["path"] for e in tree["tree"]]  # type: ignore[union-attr]
    assert "services/billing/retry.py" in paths and "services/billing" in paths and "services" in paths
    assert tree["truncated"] is False and also["tree"] == tree["tree"]
    assert "services/billing/retry.py" not in [e["path"] for e in flat["tree"]]  # type: ignore[union-attr]


async def test_commits_come_newest_first_and_filter_by_path_and_start_point(hub: Hub) -> None:
    async with hub.client() as http:
        every = listing(await http.get("/repos/lanternworks/ledger/commits", params={"sha": "HEAD", "per_page": 20}))
        billing = listing(
            await http.get(
                "/repos/lanternworks/ledger/commits", params={"sha": "main", "path": "services/billing", "per_page": 20}
            )
        )
        older = listing(await http.get("/repos/lanternworks/ledger/commits", params={"sha": str(every[1]["sha"])}))
    messages = [c["commit"]["message"] for c in every]  # type: ignore[index]
    assert messages == ["Add the checkout button", "Retry billing calls\n\nWith a doubling wait.", "Start the ledger"]
    assert every[0]["commit"]["author"]["date"] == "2026-08-24T05:50:03Z"  # type: ignore[index]
    assert every[0]["author"]["login"] == "iris-calder"  # type: ignore[index]
    assert every[0]["parents"][0]["sha"] == every[1]["sha"]  # type: ignore[index]
    assert [c["sha"] for c in billing] == [every[1]["sha"]]
    assert [c["sha"] for c in older] == [every[1]["sha"], every[2]["sha"]]


async def test_a_read_is_recorded_as_the_agent_reading_and_changes_nothing(hub: Hub) -> None:
    before = hub.store.head()
    async with hub.client() as http:
        await http.get("/repos/lanternworks/ledger/contents/web/app.ts")
    spent, read = hub.store.events(since=before)
    assert (spent.actor, spent.operation, spent.entity.external_id) == (
        Actor.SCENARIO,
        Operation.CREATE,
        "budget/iris-calder/core",
    )
    assert (read.actor, read.operation, read.entity.external_id) == (
        Actor.AGENT,
        Operation.READ,
        "repo/lanternworks/ledger",
    )
    assert read.after is None and APP


async def test_a_binary_file_reads_as_its_bytes(hub: Hub) -> None:
    async with hub.client() as http:
        found = body(await http.get("/repos/lanternworks/ledger/contents/assets/logo.png"))
    encoded = found["content"]
    assert isinstance(encoded, str) and base64.b64decode(encoded.replace("\n", "")) == LOGO


async def test_an_unserved_path_is_refused_by_name_not_answered_as_github_s_404(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(
            await http.get("/repos/lanternworks/ledger/milestones"),
            501,
            "minutehand's github fake does not implement GET /repos/lanternworks/ledger/milestones: it is not among the "
            "calls this provider serves (its README's table)",
        )
