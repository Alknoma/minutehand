"""A server with no model configured refuses a world whose people a model speaks for, naming them and what to set:
a standing world never goes silent mid-test for want of one."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from minutehand.testing.client import Refused
from tests.serve.support import Served, event_receiver
from tests.serve.test_written_people import served_with, sofia, spec


@pytest.fixture(scope="module")
def unmodelled(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    yield from served_with({}, tmp_path_factory)


def test_a_world_whose_people_a_model_speaks_for_is_refused_at_creation_on_a_server_with_no_model(
    unmodelled: Served,
) -> None:
    with event_receiver() as receiver, pytest.raises(Refused) as refused:
        unmodelled.client.create_world(spec("xoxb-unmodelled", receiver.url, sofia()))
    said = str(refused.value)
    assert "409" in said and "sofia (reply kind 'answers')" in said and "MINUTEHAND_MODEL" in said
