"""`HEAD` as a ref, in each place the fake accepts a ref or a SHA, as GitHub's public reference states it, or as
accepted where the reference does not say. `CLAIMS.md` beside the provider records each with its page.

The REST reference names a SHA, a branch or a tag for every ref parameter and never mentions `HEAD`; the GraphQL
reference takes a revision expression "suitable for rev-parse", for which `HEAD` is the first thing it reads."""

from __future__ import annotations

from tests.providers.github.github_world import Hub, body, listing


async def test_list_commits_with_sha_head_starts_at_the_default_branch(hub: Hub) -> None:
    """Not stated by the documentation ("SHA or branch to start listing commits from"); kept as accepted, the same
    listing as the default branch's. https://docs.github.com/en/rest/commits/commits#list-commits"""
    async with hub.client() as http:
        default = listing(await http.get("/repos/lanternworks/ledger/commits"))
        head = listing(await http.get("/repos/lanternworks/ledger/commits", params={"sha": "HEAD"}))
    assert [c["sha"] for c in head] == [c["sha"] for c in default]
    assert len(head) == 3


async def test_repository_content_with_ref_head_is_the_default_branchs(hub: Hub) -> None:
    """Not stated by the documentation ("The name of the commit/branch/tag"); kept as accepted.
    https://docs.github.com/en/rest/repos/contents#get-repository-content"""
    async with hub.client() as http:
        default = body(await http.get("/repos/lanternworks/ledger/contents/README.md"))
        head = body(await http.get("/repos/lanternworks/ledger/contents/README.md", params={"ref": "HEAD"}))
    assert head["sha"] == default["sha"] and head["content"] == default["content"]


async def test_get_a_tree_named_head_is_the_root_tree_of_the_default_branch(hub: Hub) -> None:
    """Not stated by the documentation ("The SHA1 value or ref (branch or tag) name of the tree"); kept as
    accepted. https://docs.github.com/en/rest/git/trees#get-a-tree"""
    async with hub.client() as http:
        default = body(await http.get("/repos/lanternworks/ledger/git/trees/main"))
        head = body(await http.get("/repos/lanternworks/ledger/git/trees/HEAD"))
    assert head["sha"] == default["sha"]
    assert [e["path"] for e in head["tree"]] == [e["path"] for e in default["tree"]]  # type: ignore[union-attr]


async def test_graphql_object_expression_head_names_the_head_commit(hub: Hub) -> None:
    """Documented: `expression` is "A Git revision expression suitable for rev-parse", and rev-parse reads `HEAD`.
    https://docs.github.com/en/graphql/reference/repos#repository"""
    query = """
    query {
        repository(owner: "lanternworks", name: "ledger") {
            head: object(expression: "HEAD") { ... on Commit { oid } }
            readme: object(expression: "HEAD:README.md") { ... on Blob { byteSize } }
        }
    }
    """
    async with hub.client() as http:
        newest = listing(await http.get("/repos/lanternworks/ledger/commits"))[0]["sha"]
        answer = body(await http.post("/graphql", json={"query": query}))
    repository = answer["data"]["repository"]  # type: ignore[index]
    assert repository["head"] == {"oid": newest}
    assert repository["readme"]["byteSize"] > 0
