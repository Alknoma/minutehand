"""A world's Calendar watch channels, read from outside through the control API: open after `events.watch`, closed
after `channels.stop`."""

from __future__ import annotations

import json
import ssl
from datetime import datetime
from typing import Any

import httpx

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed
from minutehand.domain.world import EntityKind
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served

CALENDAR = "https://www.googleapis.com/calendar/v3"
TOKEN = "ya29.watched"


def _channels(world: OpenWorld) -> list[dict[str, Any]]:
    """The world's Calendar channels, as a test reads them from outside: its entities."""
    return [
        json.loads(s.body)
        for s in world.entities(provider="google_workspace", kind=EntityKind.RECORD)
        if s.parent == "channels" and json.loads(s.body)["kind"] == "calendar_events"
    ]


def test_a_worlds_watch_channel_is_read_open_from_outside_and_closed_once_stopped(served: Served) -> None:
    seed = Seed.model_validate(
        {
            "starts_at": "2026-09-14T08:30:00Z",
            "people": [{"key": "mara", "name": "Mara Lin", "email": "mara@watched.example.com"}],
            "provider_seeds": [{"provider": "google_workspace", "body": {}}],
        }
    )
    world = OpenWorld(served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=[TOKEN]))))
    trust = ssl.create_default_context(cafile=served.bundle)
    try:
        with httpx.Client(proxy=served.proxy, verify=trust, trust_env=False, timeout=30) as http:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            watched = http.post(
                f"{CALENDAR}/calendars/primary/events/watch",
                json={"id": "watch-1", "type": "web_hook", "address": "https://127.0.0.1:9/hook", "token": "t"},
                headers=auth,
            )
            assert watched.status_code == 200, watched.text
            [held] = _channels(world)
            assert (held["id"], held["address"], held["calendar"], held["stopped"]) == (
                "watch-1",
                "https://127.0.0.1:9/hook",
                "mara@watched.example.com",
                False,
            )
            expires = datetime.fromisoformat(held["expiration"].replace("Z", "+00:00"))
            assert expires > world.now(), "open: not yet expired on the world's clock"
            stopped = http.post(
                f"{CALENDAR}/channels/stop", json={"id": "watch-1", "resourceId": held["resourceId"]}, headers=auth
            )
            assert stopped.status_code == 204, stopped.text
            [after] = _channels(world)
            assert after["stopped"] is True, "a stopped channel reads closed"
    finally:
        served.client.close_world(world.world_id)
