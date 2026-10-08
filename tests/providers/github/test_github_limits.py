"""GitHub's ceilings on what one read answers, met with a repository seeded to reach them."""

from __future__ import annotations

from datetime import timedelta

import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed, SeedCommit, SeedFile, SeedRepository
from tests.providers.github.github_world import Hub, body, github_seed, ledger, listing


@pytest.fixture
def seeded() -> GitHubSeed:
    wide = SeedRepository(
        owner="iris-calder",
        name="wide",
        files=[SeedFile(path=f"generated/file_{n:05d}.py", text=f"# {n}\n") for n in range(1200)],
        commits=[SeedCommit(message="Generate the files", author="iris-calder", before=timedelta(days=1))],
    )
    return github_seed(repositories=[ledger(tree_entry_limit=3), wide])


async def test_a_directory_is_cut_at_a_thousand_entries_without_saying_so(hub: Hub) -> None:
    async with hub.client() as http:
        listed = await http.get("/repos/iris-calder/wide/contents/generated")
        under = await http.get("/repos/iris-calder/wide/contents/generated", params={"per_page": 5})
    assert len(listing(listed)) == 1000
    assert "Link" not in listed.headers
    assert len(listing(under)) == 1000  # the contents endpoint takes no paging


async def test_a_tree_past_its_entry_limit_is_answered_truncated(hub: Hub) -> None:
    async with hub.client() as http:
        tree = body(await http.get("/repos/lanternworks/ledger/git/trees/main", params={"recursive": "1"}))
        small = body(await http.get("/repos/iris-calder/wide/git/trees/main", params={"recursive": "1"}))
    assert tree["truncated"] is True
    entries = tree["tree"]
    assert isinstance(entries, list) and len(entries) == 3
    assert small["truncated"] is False
