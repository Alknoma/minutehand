"""A world that seeds no user has nobody for a call to act as: there, and only there, a call reads as nobody's, as an
unauthenticated call does on GitHub. Public data is answered on the address's budget, and what needs a user is
GitHub's 401. Minutehand refuses no credential: this follows from the world holding no user, not from what the call
carried."""

from __future__ import annotations

import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.seed import GitHubSeed, SeedBudget, SeedOrganization, SeedRepository
from tests.providers.github.github_world import Hub, body, refusal

NOBODY = GitHubSeed(
    organizations=[SeedOrganization(login="lanternworks", name="Lantern Works")],
    repositories=[SeedRepository(owner="lanternworks", name="open")],
)


@pytest.fixture
def seeded() -> GitHubSeed:
    return NOBODY


async def test_with_nobody_to_act_as_public_data_reads_and_what_needs_a_user_is_refused_401(hub: Hub) -> None:
    """Documented: GitHub serves public data to an unauthenticated call, on 60 an hour, and refuses what needs a user
    with 401 "Requires authentication". https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api
    https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""
    async with hub.client("ghp_anything00000000000000000000000000000") as http:
        public = await http.get("/repos/lanternworks/open")
        me = refusal(await http.get("/user"), 401, "Requires authentication")
        viewer = await http.post("/graphql", json={"query": "query { viewer { login } }"})
        search = await http.get("/search/code", params={"q": "retry repo:lanternworks/open"})
    assert body(public)["full_name"] == "lanternworks/open"
    assert public.headers["X-RateLimit-Limit"] == "60" and public.headers["X-RateLimit-Resource"] == "core"
    assert me["documentation_url"] == "https://docs.github.com/rest"  # observed 2026-10-08
    assert viewer.status_code == 401 and viewer.json()["message"] == "This endpoint requires you to be authenticated."
    refusal(search, 401, "Requires authentication")


async def test_with_nobody_to_act_as_a_304_still_spends_the_addresss_budget(hub: Hub) -> None:
    """Documented: only a correctly authorized conditional request is free.
    https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate"""
    async with hub.client(None) as http:
        first = await http.get("/repos/lanternworks/open")
        again = await http.get("/repos/lanternworks/open", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304
    assert again.headers["X-RateLimit-Used"] == "2"


@pytest.mark.parametrize(
    "seeded",
    [NOBODY.model_copy(update={"budgets": [SeedBudget(login=None, resource=wire.Resource.CORE, remaining=0)]})],
)
async def test_with_nobody_to_act_as_a_spent_address_budget_is_refused_403(hub: Hub, seeded: GitHubSeed) -> None:
    """Documented: calls with no user share the address's 60 an hour, and are refused when it is spent.
    https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api"""
    async with hub.client(None) as http:
        refused = await http.get("/repos/lanternworks/open")
    assert refused.status_code == 403
    assert refused.headers["X-RateLimit-Limit"] == "60" and refused.headers["X-RateLimit-Remaining"] == "0"
    assert body(refused, 403)["message"].startswith("API rate limit exceeded for")  # type: ignore[union-attr]
