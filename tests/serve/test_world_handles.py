"""A world handle cannot be reused by accident: a closed world's id is never handed out again, a handle used after
its world closed fails loudly, and a world's own unclaimed calls are asserted empty with the offenders printed."""

from __future__ import annotations

import pytest

from minutehand import serve
from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.testing.client import Refused
from minutehand.testing.world import ClosedWorld, OpenWorld
from tests.serve.support import Served, seed


def _spec(token: str) -> CreateWorld:
    return CreateWorld(seed=seed(("sofia", "Sofia Romano")), claims=Claims(tokens=[token]))


def test_a_handle_used_after_its_world_closed_is_refused(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(_spec("handle-closed-1")))
    world.close()

    with pytest.raises(ClosedWorld, match="was closed"):
        world.events()
    with pytest.raises(ClosedWorld):
        world.close()
    with pytest.raises(Refused, match="is closed, and a closed world is never open again") as refused:
        served.client.world(world.world_id)
    assert refused.value.status == 404


def test_a_closed_worlds_id_is_never_handed_out_again(served: Served, monkeypatch: pytest.MonkeyPatch) -> None:
    first = served.client.create_world(_spec("handle-reuse-1"))
    served.client.close_world(first.world_id, quiet=False)
    minted = iter([first.world_id, first.world_id, "0f0f0f0f0f0f"])
    monkeypatch.setattr(serve.secrets, "token_hex", lambda _: next(minted))

    second = served.client.create_world(_spec("handle-reuse-2"))

    assert second.world_id == "0f0f0f0f0f0f"
    served.client.close_world(second.world_id, quiet=False)


def test_a_world_with_no_unclaimed_call_passes_the_assertion(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(_spec("handle-unclaimed-1")))
    world.assert_nothing_unclaimed()
    world.close()
