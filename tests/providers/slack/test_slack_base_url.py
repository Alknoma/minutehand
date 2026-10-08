"""Slack by base URL, with `slack_sdk` given only a `base_url`: a file's `url_private_download` and the sign-in
redirect an anonymous download gets both lead back through the proxy."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.proxy.base_url import base_url
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import SeededChannel, SeededFile, SeededPost
from tests.providers.slack.intercepted import Intercepted
from tests.providers.slack.slack_workspace import SCENARIO, START

WITH_A_FILE = SCENARIO.model_copy(
    update={
        "channels": [
            SeededChannel(
                provider="slack",
                name="launch",
                members=["iris"],
                history=[
                    SeededPost(
                        by="iris",
                        text="Brief attached",
                        ago=timedelta(hours=2),
                        files=[SeededFile(name="brief.md", mime_type="text/markdown", text="# The brief")],
                    )
                ],
            )
        ]
    }
)


async def test_a_files_urls_lead_back_through_the_base_url(slack: Intercepted, tmp_path: Path) -> None:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "files.db", "files", clock)
    provider = build()
    provider.seed(WITH_A_FILE, store)
    slack.proxy.mount(store, clock, {"slack": provider.app(store, clock)})
    api = AsyncWebClient(token="xoxb-1", base_url=f"{base_url(slack.proxy.url, 'slack.com')}/api/")
    history = (await api.conversations_history(channel=state.named_channel_id("launch"))).data
    assert isinstance(history, dict)
    [file] = history["messages"][0]["files"]
    files = base_url(slack.proxy.url, "files.slack.com")
    assert file["url_private_download"].startswith(f"{files}/"), file["url_private_download"]
    async with httpx.AsyncClient(trust_env=False) as http:
        got = await http.get(file["url_private_download"], headers={"Authorization": "Bearer xoxb-1"})
        anonymous = await http.get(file["url_private"])
    assert got.status_code == 200 and got.text == "# The brief"
    assert anonymous.status_code == 200 and anonymous.text == "# The brief"
    assert {c.exchange.host for c in store.calls()} == {"slack.com", "files.slack.com"}
