"""Vendor claims about GitHub's primary rate limit, counted: every call spends one from its user's budget for its
resource, the call after the last is refused, and the budget comes back at `X-RateLimit-Reset` on the run's clock.
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.seed import GitHubSeed, SeedBudget
from minutehand.domain.world import Actor, Operation
from tests.providers.github.github_world import NO_SCOPES, START, TOMAS, Hub, body, github_seed, refusal

CORE_RESET = str(int(START.timestamp()) + 3600)


@pytest.fixture
def seeded() -> GitHubSeed:
    return github_seed(
        budgets=[
            SeedBudget(login="outsider", resource=wire.Resource.CORE, remaining=1),
            SeedBudget(login="outsider", resource=wire.Resource.GRAPHQL, remaining=0),
            SeedBudget(login=None, resource=wire.Resource.CORE, remaining=0),
        ]
    )


def _spent(answer: httpx.Response) -> tuple[str, str, str, str]:
    h = answer.headers
    return h["X-RateLimit-Remaining"], h["X-RateLimit-Used"], h["X-RateLimit-Reset"], h["X-RateLimit-Resource"]


async def test_each_call_spends_one_from_the_users_budget_and_the_reset_stays_put(hub: Hub) -> None:
    """Documented: `X-RateLimit-Remaining` is what is left in the window and `X-RateLimit-Reset` the moment the
    window ends, in epoch seconds; a refused read is a request like any other and spends too.
    https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""
    async with hub.client() as http:
        first = await http.get("/user")
        second = await http.get("/repos/lanternworks/nothing")
        hub.clock.jump(START + timedelta(minutes=20))
        third = await http.get("/user")

    assert _spent(first) == ("4999", "1", CORE_RESET, "core")
    assert second.status_code == 404 and _spent(second) == ("4998", "2", CORE_RESET, "core")
    assert _spent(third) == ("4997", "3", CORE_RESET, "core")


async def test_two_tokens_of_one_user_spend_one_budget_and_another_user_has_their_own(hub: Hub) -> None:
    """Documented: the limit is the user's, shared by every token that acts as them.
    https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""
    async with hub.client() as http:
        await http.get("/user")
    async with hub.client(NO_SCOPES) as http:
        same_user = await http.get("/user")
    async with hub.client(TOMAS) as http:
        other_user = await http.get("/user")

    assert same_user.headers["X-RateLimit-Remaining"] == "4998"
    assert other_user.headers["X-RateLimit-Remaining"] == "4999"


async def test_each_resource_is_its_own_budget(hub: Hub) -> None:
    """Documented: core, search, code search and GraphQL are separate budgets, each named in
    `X-RateLimit-Resource`. https://docs.github.com/en/rest/rate-limit/rate-limit"""
    async with hub.client() as http:
        await http.get("/user")
        code = await http.get("/search/code", params={"q": "retry repo:lanternworks/ledger"})
        graph = await http.post("/graphql", json={"query": "query { viewer { login } }"})
        core = await http.get("/user")

    assert _spent(code) == ("9", "1", str(int(START.timestamp()) + 60), "code_search")
    assert graph.headers["X-RateLimit-Resource"] == "graphql" and graph.headers["X-RateLimit-Remaining"] == "4999"
    assert core.headers["X-RateLimit-Remaining"] == "4998"


async def test_the_call_after_the_last_is_refused_403_until_the_reset_on_the_runs_clock(hub: Hub) -> None:
    """Documented: once the budget is spent the answer is a 403 (or 429) with `X-RateLimit-Remaining: 0`, and a
    client must not try again before `X-RateLimit-Reset`. The reset is a moment on the run's clock, which does not
    move inside a wake: a budget spent there stays spent until the clock passes it.
    https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""
    async with hub.client(TOMAS) as http:
        tomas_id = body(await http.get("/user"))["id"]
    async with hub.client("ghp_outsider000000000000000000000000000000") as http:
        last = await http.get("/user")
        outsider_id = body(last)["id"]
        refused = await http.get("/user")
        again = await http.get("/repos/iris-calder/notes")
        hub.clock.jump(START + timedelta(minutes=59, seconds=59))
        still = await http.get("/user")
        hub.clock.jump(START + timedelta(hours=1))
        back = await http.get("/user")

    assert outsider_id != tomas_id
    assert _spent(last) == ("0", "5000", CORE_RESET, "core")
    for spent in (refused, again, still):
        refusal(spent, 403, f"API rate limit exceeded for user ID {outsider_id}.")
        assert _spent(spent) == ("0", "5000", CORE_RESET, "core")
    assert back.status_code == 200
    assert _spent(back) == ("4999", "1", str(int(START.timestamp()) + 2 * 3600), "core")


async def test_a_refused_call_writes_nothing_but_a_spent_call_records_its_budget(hub: Hub) -> None:
    """The budget is the world's bookkeeping, kept in the run's store as the scenario's doing: a call spends it
    and the refusal at zero changes nothing."""
    async with hub.client("ghp_outsider000000000000000000000000000000") as http:
        before = hub.store.head()
        await http.get("/user")
        between = hub.store.head()
        await http.get("/user")

    spent, read = hub.store.events(since=before)
    assert (spent.actor, spent.operation, spent.entity.external_id) == (
        Actor.SCENARIO,
        Operation.UPDATE,
        "budget/outsider/core",
    )
    assert (read.actor, read.operation) == (Actor.AGENT, Operation.READ)
    assert hub.store.head() == between


async def test_a_spent_graphql_budget_answers_200_with_rate_limited_errors(hub: Hub) -> None:
    """Documented: GraphQL reports its spent budget in `errors` with type RATE_LIMITED.
    https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api"""
    async with hub.client("ghp_outsider000000000000000000000000000000") as http:
        answered = await http.post("/graphql", json={"query": "query { viewer { login } }"})
    assert answered.status_code == 200
    assert answered.json()["errors"][0]["type"] == "RATE_LIMITED"
    assert answered.headers["X-RateLimit-Remaining"] == "0"


async def test_rate_limit_reports_every_budget_and_spends_none(hub: Hub) -> None:
    """Documented: `GET /rate_limit` reports each resource's limit, remaining, used and reset, keeps the older
    `rate` as the core budget, and calling it does not count.
    https://docs.github.com/en/rest/rate-limit/rate-limit"""
    async with hub.client() as http:
        await http.get("/user")
        first = body(await http.get("/rate_limit"))
        second = body(await http.get("/rate_limit"))

    assert first == second
    resources = first["resources"]
    assert isinstance(resources, dict)
    assert resources["core"] == {
        "limit": 5000,
        "remaining": 4999,
        "used": 1,
        "reset": int(CORE_RESET),
        "resource": "core",
    }
    assert resources["code_search"]["remaining"] == 10 and resources["code_search"]["limit"] == 10
    assert first["rate"] == resources["core"]
