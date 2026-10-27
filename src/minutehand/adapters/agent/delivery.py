"""The one door everything the world sends the agent goes through: a pushed event, a scheduler's delivery, a person's
answer on a captured channel, a decision made in the agent's own product.

Each send is recorded whole (`PushSnapshot`: method, URL, every header, the body, and how the address answered), so
the record holds everything the agent ever received, and a fork sends each again, verbatim, to play the agent's
sandbox back to a checkpoint (`application.playback`). A send goes straight to the agent's address, never through a
proxy, and checks no certificate: it is the service's request, not a check Minutehand makes.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from minutehand.domain.scenario import ProviderKey
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Header, Operation, PushSnapshot
from minutehand.ports.services import Delivered
from minutehand.ports.store import Store

TIMEOUT_SECONDS = 30.0
"""How long a send waits for the address to answer when its service says no other limit."""


@dataclass(frozen=True)
class Sent:
    """One send made: its record, and what the address answered with."""

    snapshot: PushSnapshot
    body: bytes
    timed_out: bool = False
    answer_headers: Mapping[str, str] = field(default_factory=dict)
    """The answer's headers, names lower-cased: what a handshake reads back (Asana echoes its secret in one)."""


class Door:
    """`send` makes and records one send; `resend` makes a recorded one again, recording nothing."""

    async def send(
        self,
        store: Store,
        *,
        service: ProviderKey,
        item: str,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        method: str = "POST",
        timeout: float = TIMEOUT_SECONDS,
        attempt: int = 0,
        retry_reason: str | None = None,
        parent: str | None = None,
        look: bool = False,
    ) -> Sent:
        """One send to the agent, recorded as it ended under `EntityKind.PUSH`, actor SCENARIO, so no check counts
        it as anyone's work; `look` marks a read of what waits on a person (`PushSnapshot.look`)."""
        began = time.monotonic()
        delivered, answer, timed_out, answer_headers = await self._make(method, url, headers, body, timeout)
        snapshot = PushSnapshot(
            service=service,
            item=item,
            method=method,
            url=url,
            headers=[Header(name=name, value=value) for name, value in headers.items()],
            body=body.decode("utf-8", errors="replace"),
            status=delivered.status,
            failure=delivered.failure,
            attempt=attempt,
            retry_reason=retry_reason,
            seconds=round(time.monotonic() - began, 3),
            look=look,
        )
        store.apply(
            Change(
                entity=EntityRef(provider=service, kind=EntityKind.PUSH, external_id=f"{item}/{attempt}"),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=snapshot.model_dump_json(),
                parent=parent,
                after=snapshot,
            )
        )
        return Sent(snapshot=snapshot, body=answer, timed_out=timed_out, answer_headers=answer_headers)

    async def push(
        self, store: Store, *, service: ProviderKey, item: str, url: str, body: str, parent: str | None = None
    ) -> Delivered:
        """`ports.services.DeliversPushes`: a declared service's push, JSON by POST."""
        made = await self.send(
            store,
            service=service,
            item=item,
            url=url,
            body=body.encode("utf-8"),
            headers={"content-type": "application/json"},
            parent=parent,
        )
        return Delivered(status=made.snapshot.status, failure=made.snapshot.failure)

    async def resend(self, snapshot: PushSnapshot, *, timeout: float = TIMEOUT_SECONDS) -> Delivered:
        """A recorded send made again, byte for byte: the agent's sandbox being played back receives what it
        received before. Nothing is recorded."""
        headers = {h.name: h.value for h in snapshot.headers}
        delivered, _, _, _ = await self._make(
            snapshot.method, snapshot.url, headers, snapshot.body.encode("utf-8"), timeout
        )
        return delivered

    async def _make(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> tuple[Delivered, bytes, bool, Mapping[str, str]]:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=timeout, verify=False) as client:
                answered = await client.request(method, url, content=body, headers=dict(headers))
        except httpx.TimeoutException as e:
            return Delivered(status=None, failure=f"no answer within {timeout:g}s ({e!r})"), b"", True, {}
        except httpx.HTTPError as e:
            return Delivered(status=None, failure=f"{type(e).__name__}: {e}"), b"", False, {}
        failure = None if answered.status_code < 400 else f"the address answered {answered.status_code}"
        answer_headers = {name.lower(): value for name, value in answered.headers.items()}
        return Delivered(status=answered.status_code, failure=failure), answered.content, False, answer_headers
