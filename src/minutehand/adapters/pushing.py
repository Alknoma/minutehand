"""A service's pushes (`docs/services.md`), sent to the address the agent gave it as an HTTP POST of JSON."""

from __future__ import annotations

import httpx

from minutehand.ports.services import Delivered

TIMEOUT_SECONDS = 30.0


class HttpPushes:
    """`ports.services.DeliversPushes` over HTTP, straight to the agent's address: never through a proxy."""

    async def push(self, url: str, body: str) -> Delivered:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=TIMEOUT_SECONDS) as client:
                answered = await client.post(
                    url, content=body.encode("utf-8"), headers={"content-type": "application/json"}
                )
        except httpx.HTTPError as e:
            return Delivered(status=None, failure=f"{type(e).__name__}: {e}")
        failure = None if answered.status_code < 400 else f"the address answered {answered.status_code}"
        return Delivered(status=answered.status_code, failure=failure)
