"""Vendor claims about GitHub's GraphQL endpoint and its secondary rate limits, each one a fact a retired
home-grown emulator was tested for. Every docstring says whether the claim is documented (with the page) or
observed. `CLAIMS.md` is the index."""

from __future__ import annotations

import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.seed import GitHubSeed
from tests.providers.github.github_world import LOGO, README, Hub, body, github_seed

OVERVIEW = """
query($owner: String!, $name: String!) {
    repository(owner: $owner, name: $name) {
        primaryLanguage { name }
        notes: object(expression: "HEAD:README.md") { ... on Blob { text } }
    }
}
"""


@pytest.fixture
def seeded() -> GitHubSeed:
    return github_seed(
        faults=[
            wire.SecondaryRateLimited(resource=wire.Resource.CORE, retry_after=9),
            wire.SecondaryRateLimited(resource=wire.Resource.CORE, status=429, retry_after=2),
        ]
    )


async def _graph(hub: Hub, payload: dict[str, object]) -> dict[str, object]:
    async with hub.client() as http:
        return body(await http.post("/graphql", json=payload))


async def test_variables_name_the_repository_a_query_reads(hub: Hub) -> None:
    """Documented: a query's `$variables` are given beside it in `variables`.
    https://docs.github.com/en/graphql/guides/forming-calls-with-graphql"""
    found = await _graph(hub, {"query": OVERVIEW, "variables": {"owner": "lanternworks", "name": "ledger"}})
    repository = found["data"]["repository"]  # type: ignore[index]
    assert repository["primaryLanguage"] == {"name": "Python"}
    assert repository["notes"] == {"text": README}


async def test_a_repository_that_does_not_resolve_is_null_with_a_not_found_error(hub: Hub) -> None:
    """Observed: `data.repository` is null and `errors` carries NOT_FOUND, "Could not resolve to a Repository".
    https://docs.github.com/en/graphql/reference/queries#repository"""
    found = await _graph(hub, {"query": OVERVIEW, "variables": {"owner": "someone", "name": "elsewhere"}})
    assert found["data"] == {"repository": None}
    [error] = found["errors"]  # type: ignore[misc]
    assert error["type"] == "NOT_FOUND"
    assert error["message"].startswith("Could not resolve to a Repository")


async def test_a_binary_blob_has_null_text(hub: Hub) -> None:
    """Documented: `Blob.text` is null when the blob is binary. https://docs.github.com/en/graphql/reference/git#blob"""
    query = (
        'query { repository(owner: "lanternworks", name: "ledger") { '
        'logo: object(expression: "HEAD:assets/logo.png") { ... on Blob { text isBinary byteSize } } } }'
    )
    blob = (await _graph(hub, {"query": query}))["data"]["repository"]["logo"]  # type: ignore[index]
    assert blob == {"text": None, "isBinary": True, "byteSize": len(LOGO)}


async def test_an_expression_naming_no_path_is_null(hub: Hub) -> None:
    """Documented: `Repository.object` may be null when the expression names nothing.
    https://docs.github.com/en/graphql/reference/objects#repository"""
    query = (
        'query { repository(owner: "lanternworks", name: "ledger") { '
        'ghost: object(expression: "HEAD:services/ghost/main.py") { ... on Blob { text } } } }'
    )
    assert (await _graph(hub, {"query": query}))["data"]["repository"]["ghost"] is None  # type: ignore[index]


async def test_the_viewer_is_the_token_s_user(hub: Hub) -> None:
    """Documented: `viewer` is the authenticated user; `query { viewer { login } }` is the guide's first example.
    https://docs.github.com/en/graphql/guides/forming-calls-with-graphql
    https://docs.github.com/en/graphql/reference/queries#viewer"""
    assert await _graph(hub, {"query": "query { viewer { login name } }"}) == {
        "data": {"viewer": {"login": "iris-calder", "name": "Iris Calder"}}
    }


async def test_a_body_without_a_query_is_refused_400(hub: Hub) -> None:
    """Documented: the payload must carry a string called `query`.
    https://docs.github.com/en/graphql/guides/forming-calls-with-graphql
    Observed: the 400 says a query attribute must be specified and must be a string."""
    async with hub.client() as http:
        answered = await http.post("/graphql", json={"variables": {}})
        numeric = await http.post("/graphql", json={"query": 7})
    for refused in (answered, numeric):
        assert refused.status_code == 400
        assert refused.json()["message"] == "A query attribute must be specified and must be a string."


async def test_a_secondary_limit_is_a_403_or_429_with_retry_after_and_then_the_call_goes_through(hub: Hub) -> None:
    """Documented: a secondary limit answers 403 or 429 with an error message, and `retry-after` when GitHub says
    how long to wait. https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""
    async with hub.client() as http:
        first = await http.get("/user")
        second = await http.get("/user")
        served = await http.get("/user")
    assert (first.status_code, first.headers["Retry-After"]) == (403, "9")
    assert "secondary rate limit" in first.json()["message"]
    assert (second.status_code, second.headers["Retry-After"]) == (429, "2")
    assert body(served)["login"] == "iris-calder"
