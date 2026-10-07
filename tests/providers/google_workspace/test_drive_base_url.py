"""Drive by base URL: a resumable upload's session `Location` leads back through the proxy, and the bytes sent there
make the file."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from minutehand.adapters.proxy.base_url import base_url
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from tests.providers.google_workspace.drive_world import AUTH, Drive


async def test_a_resumable_uploads_session_location_leads_back_through_the_base_url(
    drive: Drive, tmp_path: Path
) -> None:
    async with Proxy(Routing(Registry.installed()), drive.store, drive.clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(drive.store, drive.clock, {"google_workspace": drive.provider.app(drive.store, drive.clock)})
        base = base_url(proxy.url, "www.googleapis.com")
        async with httpx.AsyncClient(base_url=base, headers=AUTH, trust_env=False) as http:
            begun = await http.post(
                "/upload/drive/v3/files",
                params={"uploadType": "resumable"},
                content=json.dumps({"name": "notes.txt"}),
                headers={
                    "Content-Type": "application/json",
                    "X-Upload-Content-Type": "text/plain",
                    "X-Upload-Content-Length": "20",
                },
            )
            assert begun.status_code == 200, begun.text
            session = begun.headers["location"]
            assert session.startswith(f"{base}/upload/drive/v3/files?"), session
            sent = await http.put(session, content=b"three vendors remain")
    assert sent.status_code == 200, sent.text
    assert sent.json()["name"] == "notes.txt"
    assert {c.exchange.host for c in drive.store.calls()} == {"www.googleapis.com"}
