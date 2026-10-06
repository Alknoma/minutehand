"""GitHub by base URL: the `Link` header's pages lead back through the proxy, and the record keeps GitHub's own."""

from __future__ import annotations

import re

import httpx

from minutehand.adapters.proxy.base_url import base_url
from tests.providers.github.github_world import HEADERS, IRIS, Hub, listing


async def test_the_link_headers_pages_lead_back_through_the_base_url(hub: Hub) -> None:
    base = base_url(hub.proxy.url, "api.github.com")
    headers = {**HEADERS, "Authorization": f"Bearer {IRIS}"}
    async with httpx.AsyncClient(base_url=base, headers=headers, trust_env=False) as http:
        first = await http.get("/user/repos", params={"per_page": 3})
        following = re.search(r'<([^>]+)>; rel="next"', first.headers["Link"])
        assert following is not None, first.headers["Link"]
        assert following.group(1).startswith(f"{base}/user/repos?")
        second = await http.get(following.group(1))
    assert len(listing(first)) == 3 and len(listing(second)) == 1
    [kept, _] = hub.store.calls()
    assert kept.exchange.host == "api.github.com"
