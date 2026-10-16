"""A mailbox's time zone, as the Microsoft seed sets it and `mailboxSettings` answers it: `/me` and `/users/{id}`,
the whole settings and the `timeZone` property alone."""

from __future__ import annotations

from pathlib import Path

import pytest

from minutehand.domain.scenario import Scenario
from tests.providers.microsoft.outlook import AGENT, signed_in
from tests.providers.microsoft.tenant import GRAPH, START, Intercepted, Tenant, bearer, seeded, token


def zoned(time_zones: dict[str, str]) -> Scenario:
    return Scenario.model_validate(
        {
            "name": "zoned_case",
            "goal": "The vendor review is booked with Sofia.",
            "owner": "owen",
            "starts_at": START,
            "people": [
                {"key": "owen", "name": "Owen Okafor", "email": "owen@example.com"},
                {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com"},
            ],
            "provider_seeds": [
                {
                    "provider": "microsoft",
                    "body": {
                        "mailbox": {"key": "agent", "name": "Assistant", "local": "assistant"},
                        "time_zones": time_zones,
                    },
                }
            ],
        }
    )


ZONED = zoned({"owen": "GMT Standard Time", "agent": "Taipei Standard Time"})


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", ZONED)


async def test_a_seeded_mailbox_time_zone_is_answered_from_me_and_from_users_and_an_unseeded_one_is_utc(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    agent = AGENT.replace("outlookcase", "zonedcase")
    async with microsoft.http() as http:
        app = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        owen = await http.get(f"{GRAPH}/users/owen@example.com/mailboxSettings", headers=app)
        sofia = await http.get(f"{GRAPH}/users/sofia@example.com/mailboxSettings", headers=app)
        alone = await http.get(f"{GRAPH}/users/owen@example.com/mailboxSettings/timeZone", headers=app)
        me = bearer(await signed_in(http, tenant, agent))
        mine = await http.get(f"{GRAPH}/me/mailboxSettings", headers=me)
        mine_alone = await http.get(f"{GRAPH}/me/mailboxSettings/timeZone", headers=me)
    assert owen.status_code == 200, owen.text
    assert owen.json()["timeZone"] == "GMT Standard Time"
    assert owen.json()["@odata.context"].endswith("/mailboxSettings")
    assert sofia.json()["timeZone"] == "UTC"
    assert alone.status_code == 200, alone.text
    assert alone.json()["value"] == "GMT Standard Time"
    assert alone.json()["@odata.context"].endswith("/mailboxSettings/timeZone")
    assert mine.status_code == 200, mine.text
    assert mine.json()["timeZone"] == "Taipei Standard Time"
    assert mine_alone.json()["value"] == "Taipei Standard Time"


def test_a_time_zone_for_no_mailbox_of_the_tenant_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="time_zones name no mailbox of the tenant: zed"):
        seeded(tmp_path / "world.db", zoned({"zed": "Taipei Standard Time"}))
