"""Faults armed in the seed, answered in GitHub's shapes and read the way the client under study reads them:
429, or 403 whose body says "rate limit"; `Retry-After` for how long to wait; anything 5xx is retried."""

from __future__ import annotations

import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.domain.world import Actor, Operation
from tests.providers.github.github_world import START, Hub, body, github_seed

OVERVIEW = '{ repository(owner: "lanternworks", name: "ledger") { name } }'


@pytest.fixture
def seeded() -> GitHubSeed:
    return github_seed(
        faults=[
            wire.RateLimited(resource=wire.Resource.CODE_SEARCH),
            wire.RateLimited(resource=wire.Resource.CORE, status=429),
            wire.SecondaryRateLimited(resource=wire.Resource.CORE, retry_after=7),
            wire.ServerError(resource=wire.Resource.CORE, status=502, times=2),
            wire.RateLimited(resource=wire.Resource.GRAPHQL),
        ]
    )


def _rate_limited_as_the_client_reads_it(status: int, text: str) -> bool:
    lowered = text.lower()
    return status == 429 or (status == 403 and ("rate limit" in lowered or "too many requests" in lowered))


async def test_each_fault_answers_once_in_order_and_then_the_call_goes_through(hub: Hub) -> None:
    async with hub.client() as http:
        search = await http.get("/search/code", params={"q": "retry repo:lanternworks/ledger"})
        primary = await http.get("/user")
        secondary = await http.get("/user")
        failed = [await http.get("/user"), await http.get("/user")]
        served = await http.get("/user")

    assert search.status_code == 403 and _rate_limited_as_the_client_reads_it(403, search.text)
    assert search.headers["X-RateLimit-Remaining"] == "0"
    assert search.headers["X-RateLimit-Resource"] == "code_search"
    assert search.headers["X-RateLimit-Reset"] == str(int(START.timestamp()) + 60)
    assert "Retry-After" not in search.headers

    assert primary.status_code == 429 and primary.headers["X-RateLimit-Reset"] == str(int(START.timestamp()) + 3600)

    assert secondary.status_code == 403 and _rate_limited_as_the_client_reads_it(403, secondary.text)
    assert secondary.headers["Retry-After"] == "7"
    assert "secondary rate limit" in secondary.json()["message"]

    assert [f.status_code for f in failed] == [502, 502]
    assert body(served)["login"] == "iris-calder"


async def test_the_graphql_budget_is_spent_as_a_200_whose_errors_say_rate_limited(hub: Hub) -> None:
    async with hub.client() as http:
        for _ in range(5):
            await http.get("/user")
        limited = await http.post("/graphql", json={"query": OVERVIEW})
        served = await http.post("/graphql", json={"query": OVERVIEW})
    assert limited.status_code == 200 and limited.headers["X-RateLimit-Remaining"] == "0"
    assert limited.json()["errors"][0]["type"] == "RATE_LIMITED" and "data" not in limited.json()
    assert served.json() == {"data": {"repository": {"name": "ledger"}}}


async def test_a_fault_for_one_budget_leaves_the_others_alone(hub: Hub) -> None:
    async with hub.client() as http:
        graph = await http.post("/graphql", json={"query": OVERVIEW})
    assert graph.json()["errors"][0]["type"] == "RATE_LIMITED"  # the code-search and core faults are still armed
    async with hub.client() as http:
        search = await http.get("/search/code", params={"q": "retry repo:lanternworks/ledger"})
    assert search.status_code == 403


async def test_a_spent_fault_is_the_world_s_doing_in_the_log(hub: Hub) -> None:
    before = hub.store.head()
    async with hub.client() as http:
        await http.get("/search/code", params={"q": "retry repo:lanternworks/ledger"})
    budget, spent = hub.store.events(since=before)
    assert (budget.actor, budget.entity.external_id) == (Actor.SCENARIO, "budget/iris-calder/code_search")
    assert (spent.actor, spent.operation) == (Actor.SCENARIO, Operation.UPDATE)
    assert spent.entity.external_id == "fault/000000"


async def test_a_credential_the_world_does_not_hold_meets_the_armed_fault_as_any_call_does(hub: Hub) -> None:
    """Nothing refuses a credential, so no credential is refused ahead of a fault: an unknown token's call is
    answered by the first fault armed for its budget, as a known token's is."""
    async with hub.client("ghp_nobodyissuedthis000000000000000000000") as http:
        limited = await http.get("/search/code", params={"q": "retry repo:lanternworks/ledger"})
    assert limited.status_code == 403 and "rate limit" in limited.text
