"""External emulators in the standing mode: one emulator process per server, started with the first world that
declares it and shared by every world that declares it the same; each forwarded call carries its world's id, so
the emulator can tell two worlds apart, and each world keeps only its own calls."""

from __future__ import annotations

import pytest
import requests

from minutehand.domain.emulator import ExternalEmulator, ReadyHttp, Upstream
from minutehand.domain.outbound import Forward
from minutehand.domain.world import CallOutcome, CaptureMode
from minutehand.testing.client import Refused
from minutehand.testing.world import OpenWorld
from tests.emulators.support import stand_in
from tests.serve.support import Served, spec

HOST = "api.tracker.test"


@pytest.fixture(scope="module")
def tracker(tmp_path_factory: pytest.TempPathFactory) -> ExternalEmulator:
    """One declaration for the module: the server it runs on lives as long, and so does an emulator it started."""
    return ExternalEmulator(
        name="tracker",
        upstream=Upstream(url="http://127.0.0.1:{port}"),
        command=stand_in(tmp_path_factory.mktemp("stand-in")),
        ready=ReadyHttp(),
    )


def _client(served: Served, token: str) -> requests.Session:
    configured = requests.Session()
    configured.proxies = {"https": served.proxy}
    configured.verify = served.bundle
    configured.trust_env = False
    configured.headers["authorization"] = f"Bearer {token}"
    return configured


def test_two_worlds_share_one_emulator_and_are_told_apart_by_the_world_header(
    served: Served, tracker: ExternalEmulator
) -> None:
    declared = {"outbound": [Forward(host=HOST, emulator="tracker")], "emulators": [tracker]}
    first = OpenWorld(served.client, served.client.create_world(spec("tracker-one").model_copy(update=declared)))
    second = OpenWorld(served.client, served.client.create_world(spec("tracker-two").model_copy(update=declared)))
    try:
        one = _client(served, "tracker-one").get(f"https://{HOST}/issues")
        two = _client(served, "tracker-two").get(f"https://{HOST}/issues")
        assert one.json() == {"ok": True, "world": first.world_id}
        assert two.json() == {"ok": True, "world": second.world_id}
        for world in (first, second):
            [call] = world.captured_calls()
            assert call.exchange.captured is not None and call.exchange.captured.mode is CaptureMode.FORWARD
            assert call.exchange.outcome is CallOutcome.ANSWERED
    finally:
        served.client.close_world(first.world_id)
        served.client.close_world(second.world_id)


def test_a_world_declaring_a_running_emulator_otherwise_is_refused(served: Served, tracker: ExternalEmulator) -> None:
    declared = {"outbound": [Forward(host=HOST, emulator="tracker")], "emulators": [tracker]}
    first = served.client.create_world(spec("tracker-three").model_copy(update=declared))
    other = tracker.model_copy(update={"env": {"FLAVOUR": "another"}})
    try:
        with pytest.raises(Refused) as refused:
            served.client.create_world(spec("tracker-four").model_copy(update={**declared, "emulators": [other]}))
        assert refused.value.status == 409 and "already running as another world declared it" in refused.value.error
    finally:
        served.client.close_world(first.world_id)
