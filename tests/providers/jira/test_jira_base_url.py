"""Jira by base URL: every `self` link names the base URL, and following one is answered as the site."""

from __future__ import annotations

import httpx

from minutehand.adapters.proxy.base_url import base_url
from tests.providers.jira.jira_site import AGENT, AGENT_EMAIL, AGENT_TOKEN, Site, basic, ok


async def test_self_links_lead_back_through_the_base_url(site: Site) -> None:
    base = base_url(site.proxy.url, "lanternworks.atlassian.net")
    headers = {"Accept": "application/json", "Authorization": basic(AGENT_EMAIL, AGENT_TOKEN)}
    async with httpx.AsyncClient(base_url=base, headers=headers, trust_env=False) as http:
        me = ok(await http.get("/rest/api/3/myself"))
        assert me["self"] == f"{base}/rest/api/3/user?accountId={AGENT}"
        again = ok(await http.get(me["self"]))
    assert again["accountId"] == AGENT
    assert {c.exchange.host for c in site.store.calls()} == {"lanternworks.atlassian.net"}
