"""Vendor claims about paging a large team's roster through the Bot Framework connector, checked against Microsoft's
documentation (https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context): a page holds
200 members by default, never fewer than 50 and never more than 500. The page does not say a smaller or larger
`pageSize` is refused, so this provider answers the nearest size it allows. `CLAIMS.md` lists each claim."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.domain.scenario import Scenario
from tests.providers.microsoft.tenant import CONNECTOR, SCENARIO, Intercepted, Tenant, bearer, seeded, token

CROWD = 260


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    """The usual tenant with a team of 263 people: more than one default page."""
    extra = [
        {"key": f"crew{n:03d}", "name": f"Crew Member {n:03d}", "email": f"crew{n:03d}@example.com"}
        for n in range(CROWD)
    ]
    people = [p.model_dump(mode="json", exclude_defaults=True) for p in SCENARIO.people] + extra
    crowded = Scenario.model_validate({**SCENARIO.model_dump(mode="json", exclude_defaults=True), "people": people})
    return seeded(tmp_path / "world.db", crowded)


@pytest.fixture
async def http(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    async with microsoft.http() as client:
        yield client, bearer(await token(client, tenant, "https://api.botframework.com/.default"))


async def _page(http: tuple[httpx.AsyncClient, dict[str, str]], tenant: Tenant, **params: str) -> dict[str, Any]:
    client, auth = http
    url = f"{CONNECTOR}v3/conversations/{tenant.directory.general_channel_id}/pagedmembers"
    answered = await client.get(url, params=params, headers=auth)
    assert answered.status_code == 200, answered.text
    page: dict[str, Any] = answered.json()
    return page


async def test_a_page_without_a_size_holds_two_hundred_members_and_a_token_for_the_rest(
    http: tuple[httpx.AsyncClient, dict[str, str]], tenant: Tenant
) -> None:
    """Documented: the default page size is 200, a `continuationToken` leads to the rest, and each member's `id` is
    the `29:` id the bot addresses them by, with the directory object id beside it. Class (a), and the connector
    reference's ChannelAccount. The old emulator answered one member per page by default: contradicted, see
    `CLAIMS.md`."""
    first = await _page(http, tenant)
    assert len(first["members"]) == 200 and first["continuationToken"]
    rest = await _page(http, tenant, continuationToken=first["continuationToken"])
    assert len(rest["members"]) == CROWD + 3 - 200 and (
        "continuationToken" not in rest or rest["continuationToken"] is None
    )
    emails = {m["email"] for m in first["members"] + rest["members"]}
    assert len(emails) == CROWD + 3
    for member in first["members"] + rest["members"]:
        assert member["id"].startswith("29:")
        assert member["aadObjectId"] and not member["aadObjectId"].startswith("29:")


@pytest.mark.parametrize("asked", ["1", "2", "49"])
async def test_a_page_size_below_fifty_answers_fifty_members(
    http: tuple[httpx.AsyncClient, dict[str, str]], tenant: Tenant, asked: str
) -> None:
    """Documented: the minimum page size is 50, so a smaller `pageSize` still answers 50 members. Class (a)."""
    page = await _page(http, tenant, pageSize=asked)
    assert len(page["members"]) == 50 and page["continuationToken"]


async def test_a_page_size_inside_the_bounds_is_honoured(
    http: tuple[httpx.AsyncClient, dict[str, str]], tenant: Tenant
) -> None:
    """Documented: a `pageSize` between 50 and 500 is the page's size. Class (a)."""
    page = await _page(http, tenant, pageSize="75")
    assert len(page["members"]) == 75
