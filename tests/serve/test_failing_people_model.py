"""A model call that fails leaves the person's answer owed: the failure is kept with the world, the next `advance`
tries again, and the answer lands then."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest

from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, dm, event_receiver
from tests.serve.test_written_people import SAID, model_that_fails_first, served_with, sofia, spec
from tests.support.people import people_environment


@pytest.fixture(scope="module")
def failing(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    with model_that_fails_first() as base_url:
        yield from served_with(people_environment() | {"MINUTEHAND_MODEL_BASE_URL": base_url}, tmp_path_factory)


def test_a_failed_model_call_leaves_the_answer_owed_and_the_next_advance_writes_it(failing: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(failing.client, failing.client.create_world(spec("xoxb-fails", receiver.url, sofia())))
        try:
            channel = dm(failing, "xoxb-fails", "sofia@example.com")
            failing.slack("xoxb-fails").chat_postMessage(channel=channel, text="Does Thursday work?")

            first = world.advance(timedelta(hours=2))
            assert receiver.texts() == []
            assert next(f.what for f in first.fired).startswith("sofia's reply (conversing): not written (")
            view = failing.client.world(world.world_id)
            [owed] = view.people_owe
            assert owed.failed is not None and "503" in owed.failed
            [failed] = view.person_calls
            assert failed.answer is None and failed.failure is not None and "503" in failed.failure

            world.advance(timedelta(minutes=1))
            assert receiver.texts() == [SAID]
            calls = failing.client.world(world.world_id).person_calls
            assert [c.failure is None for c in calls] == [False, True]
            assert failing.client.world(world.world_id).people_owe == []
        finally:
            failing.client.close_world(world.world_id)
