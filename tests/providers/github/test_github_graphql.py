"""GraphQL: the three queries the client sends, written as it writes them, and what GitHub refuses."""

from __future__ import annotations

from collections.abc import Mapping

from tests.providers.github.github_world import LOGO, README, RETRY, Hub, body

FILE_BATCH = """
query {
    repository(owner: "lanternworks", name: "ledger") {
        file_0: object(expression: "HEAD:services/billing/retry.py") { ... on Blob { text isBinary byteSize oid } }
        file_1: object(expression: "HEAD:assets/logo.png") { ... on Blob { text isBinary byteSize oid } }
        file_2: object(expression: "HEAD:no/such/file.py") { ... on Blob { text isBinary byteSize oid } }
        file_3: object(expression: "HEAD:services") { ... on Blob { text isBinary byteSize oid } }
    }
}
"""

OVERVIEW = """
query($owner: String!, $repo: String!) {
    repository(owner: $owner, name: $repo) {
        description
        homepageUrl
        primaryLanguage { name }
        languages(first: 10) {
            nodes { name }
            totalCount
        }
        repositoryTopics(first: 10) {
            nodes { topic { name } }
        }
        defaultBranchRef { name }
        licenseInfo { name spdxId }
        readme: object(expression: "HEAD:README.md") {
            ... on Blob { text }
        }
        isPrivate
        stargazerCount
        forkCount
    }
}
"""

BRANCHES = """
query($owner: String!, $repo: String!, $first: Int!) {
    repository(owner: $owner, name: $repo) {
        defaultBranchRef { name }
        refs(refPrefix: "refs/heads/", first: $first, orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
            nodes {
                name
                target {
                    ... on Commit {
                        oid
                        messageHeadline
                    }
                }
            }
        }
    }
}
"""

LEDGER = {"owner": "lanternworks", "repo": "ledger"}


async def _ask(hub: Hub, query: str, variables: Mapping[str, object] | None = None, token: str | None = None) -> dict:
    payload: dict[str, object] = {"query": query}
    if variables:
        payload["variables"] = dict(variables)
    async with hub.client(token) if token is not None else hub.client() as http:
        return body(await http.post("/graphql", json=payload))


async def test_a_batch_of_files_answers_text_binary_missing_and_directory(hub: Hub) -> None:
    found = await _ask(hub, FILE_BATCH)
    repository = found["data"]["repository"]
    assert repository["file_0"]["text"] == RETRY and repository["file_0"]["isBinary"] is False
    assert repository["file_0"]["byteSize"] == len(RETRY) and len(repository["file_0"]["oid"]) == 40
    assert repository["file_1"] == {
        "text": None,
        "isBinary": True,
        "byteSize": len(LOGO),
        "oid": repository["file_1"]["oid"],
    }
    assert repository["file_2"] is None
    assert repository["file_3"] == {}  # a tree is no Blob: the fragment selects nothing
    assert "errors" not in found


async def test_the_overview_reads_from_variables(hub: Hub) -> None:
    found = (await _ask(hub, OVERVIEW, LEDGER))["data"]["repository"]
    assert found["primaryLanguage"] == {"name": "Python"}
    assert found["languages"] == {"nodes": [{"name": "Python"}, {"name": "TypeScript"}], "totalCount": 2}
    assert found["repositoryTopics"] == {"nodes": [{"topic": {"name": "billing"}}, {"topic": {"name": "payments"}}]}
    assert found["defaultBranchRef"] == {"name": "main"}
    assert found["licenseInfo"] == {"name": "MIT License", "spdxId": "MIT"}
    assert found["readme"] == {"text": README}
    assert (found["isPrivate"], found["stargazerCount"], found["forkCount"]) == (True, 4, 0)


async def test_branches_come_from_refs_with_their_head_commit(hub: Hub) -> None:
    found = (await _ask(hub, BRANCHES, {**LEDGER, "first": 30}))["data"]["repository"]
    nodes = found["refs"]["nodes"]
    assert [n["name"] for n in nodes] == ["main", "release"]
    assert nodes[0]["target"]["messageHeadline"] == "Add the checkout button"
    assert len(nodes[0]["target"]["oid"]) == 40


async def test_a_repository_the_token_cannot_see_is_not_resolved(hub: Hub) -> None:
    found = await _ask(hub, OVERVIEW, LEDGER, token="ghp_outsider000000000000000000000000000000")
    assert found["data"] == {"repository": None}
    [error] = found["errors"]
    assert error["type"] == "NOT_FOUND" and error["path"] == ["repository"]
    assert error["message"] == "Could not resolve to a Repository with the name 'lanternworks/ledger'."


async def test_a_field_the_schema_does_not_hold_is_refused_with_no_data(hub: Hub) -> None:
    found = await _ask(hub, 'query { repository(owner: "lanternworks", name: "ledger") { issues { totalCount } } }')
    assert "data" not in found
    [error] = found["errors"]
    assert error["extensions"]["code"] == "undefinedField"
    assert error["message"] == "Field 'issues' doesn't exist on type 'Repository'"


async def test_a_missing_required_variable_is_refused(hub: Hub) -> None:
    found = await _ask(hub, OVERVIEW, {"owner": "lanternworks"})
    assert "data" not in found and "$repo" in found["errors"][0]["message"]


async def test_a_connection_without_first_or_last_is_refused_and_nulled(hub: Hub) -> None:
    found = await _ask(
        hub,
        'query { repository(owner: "lanternworks", name: "ledger") { refs(refPrefix: "refs/heads/") { totalCount } } }',
    )
    assert found["data"]["repository"]["refs"] is None
    assert found["errors"][0]["type"] == "MISSING_PAGINATION_BOUNDARIES"


async def test_a_query_that_does_not_parse_is_refused_with_its_location(hub: Hub) -> None:
    found = await _ask(hub, "query { repository(owner: ")
    assert "data" not in found and found["errors"][0]["message"].startswith("Parse error on")


async def test_graphql_without_a_token_is_refused(hub: Hub) -> None:
    async with hub.client(None) as http:
        answered = await http.post("/graphql", json={"query": OVERVIEW, "variables": LEDGER})
    assert answered.status_code == 401
    assert answered.json()["message"] == "This endpoint requires you to be authenticated."
