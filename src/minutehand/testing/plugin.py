"""The pytest plugin: fixtures for a suite that uses `minutehand serve`, and nothing else.

    minutehand          (session) a `MinutehandClient`: to the server at $MINUTEHAND_URL, or to one started
                        in this process for the session, with its state in a temporary directory
    minutehand_spec     (test) the `CreateWorld` a test's world is opened from; a suite defines it
    minutehand_world    (test) an `OpenWorld` opened from `minutehand_spec`, closed after the test

No hook, option or marker is added, and nothing of Minutehand is imported until one of these is requested.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from minutehand.adapters.control.wire import CreateWorld
    from minutehand.testing.client import MinutehandClient
    from minutehand.testing.world import OpenWorld

URL_VARIABLE = "MINUTEHAND_URL"


@pytest.fixture(scope="session")
def minutehand(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MinutehandClient]:
    from minutehand.testing.client import MinutehandClient

    if URL_VARIABLE in os.environ:
        with MinutehandClient(os.environ[URL_VARIABLE]) as client:
            yield client
        return
    from minutehand.testing.background import serve_in_background

    with serve_in_background(tmp_path_factory.mktemp("minutehand")) as url, MinutehandClient(url) as client:
        yield client


@pytest.fixture
def minutehand_spec() -> CreateWorld:
    raise pytest.UsageError(
        "minutehand_world opens a world from the fixture `minutehand_spec`: define it in your conftest.py or "
        "test module, returning a minutehand.adapters.control.wire.CreateWorld (its seed and the tokens or "
        "hosts that make a call this test's)"
    )


@pytest.fixture
def minutehand_world(minutehand: MinutehandClient, minutehand_spec: CreateWorld) -> Iterator[OpenWorld]:
    from minutehand.testing.world import OpenWorld

    world = OpenWorld(minutehand, minutehand.create_world(minutehand_spec))
    try:
        yield world
    finally:
        if world.closed is None:
            world.close()
