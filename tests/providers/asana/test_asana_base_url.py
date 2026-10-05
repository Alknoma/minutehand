"""Asana by base URL: `next_page.uri` leads back through the proxy, while a task's `permalink_url`, a page for a
person and not under the API's `/api/1.0`, is left as Asana wrote it."""

from __future__ import annotations

from pathlib import Path

import httpx

from minutehand.adapters.proxy.base_url import base_url
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from tests.providers.asana.asana_workspace import AUTH, VENUE, Workspace


async def test_next_page_leads_back_through_the_base_url_and_a_permalink_does_not(
    workspace: Workspace, tmp_path: Path
) -> None:
    store, clock = workspace.store, workspace.clock
    async with Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(store, clock, {"asana": workspace.provider.app(store, clock)})
        api = f"{base_url(proxy.url, 'app.asana.com')}/api/1.0"
        async with httpx.AsyncClient(headers=AUTH, trust_env=False) as http:
            first = (
                await http.get(f"{api}/projects/{VENUE}/tasks", params={"limit": "1", "opt_fields": "permalink_url"})
            ).json()
            following = first["next_page"]["uri"]
            assert following.startswith(f"{api}/projects/{VENUE}/tasks?"), following
            second = (await http.get(following)).json()
    assert second["data"] and second["data"][0]["gid"] != first["data"][0]["gid"]
    assert first["data"][0]["permalink_url"].startswith("https://app.asana.com/0/")
    assert {c.exchange.path.split("?")[0] for c in store.calls()} == {f"/api/1.0/projects/{VENUE}/tasks"}
