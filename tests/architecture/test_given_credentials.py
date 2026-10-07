"""A person's credential given when the world is opened (`CreateWorld.credentials`): a harness that mints a key for
the approver in the product itself, for this run, has no environment variable on the server to name. The approver
here declares no `credential` at all, so the only way Minutehand can read and decide as her is the one given."""

from __future__ import annotations

import pytest

from minutehand.adapters.control.wire import CreateWorld
from minutehand.domain.world import InboxItemSnapshot, ItemStatus
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld
from tests.architecture.support import APPROVER_TOKEN
from tests.architecture.test_driven_approvals import _spec, _until_approval, agent  # noqa: F401  (a fixture)


def _given(credentials: dict[str, str]) -> CreateWorld:
    spec = _spec().model_dump(mode="json")
    for person in spec["seed"]["people"]:
        person["credential"] = None
    return CreateWorld.model_validate({**spec, "credentials": credentials})


@pytest.fixture
def minutehand_spec() -> CreateWorld:
    return _given({"nadia": APPROVER_TOKEN})


@pytest.mark.timeout(600)
def test_a_credential_given_with_the_world_is_whom_minutehand_reads_and_decides_as(
    minutehand_world: OpenWorld,
    agent: int,  # noqa: F811
) -> None:
    answered = _until_approval(minutehand_world, agent)
    [waiting] = minutehand_world.inboxes().pending
    assert waiting.person == "nadia"

    with minutehand_world.step(at=answered, reason="nadia approves"):
        made = minutehand_world.decide("nadia", waiting.item, "approve")
    assert made.accepted and isinstance(made.event.after, InboxItemSnapshot)
    assert made.event.after.status is ItemStatus.DECIDED


def test_a_credential_given_for_someone_who_is_not_in_the_world_refuses_it(minutehand: MinutehandClient) -> None:
    with pytest.raises(Exception, match="not people of this world"):
        minutehand.create_world(_given({"nobody": "x"}))
