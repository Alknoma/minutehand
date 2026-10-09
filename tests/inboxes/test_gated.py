"""The team's rules on going ahead (`tests/inboxes/support.APPROVAL_RULES`), written over asks and transitions: the
agent went ahead while the approval was pending, or after a rejection."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.domain.agent import WakeRequest
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import Take
from tests.inboxes.product import Product, serving
from tests.inboxes.support import NADIA, OWEN, TOKENS, Agent, checks_named, deciding, inbox, play, scenario, sends

CHECK = "acts_only_once_approved"
WENT_AHEAD = "went ahead with the booking before it was approved"
AFTER_REJECTION = "never_goes_ahead_after_a_rejection"


@pytest.fixture
def product() -> Iterator[Product]:
    with serving(Product(tokens=dict(TOKENS))) as served:
        yield served


@pytest.fixture(autouse=True)
def nadias_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NADIA_TOKEN", TOKENS[NADIA])


def agent(product: Product, *, at_once: bool = False, send_anyway: bool = False) -> Callable[[SqliteStore], Agent]:
    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                if at_once:
                    sends(store, OWEN, "Booked: LH-2291", operation="tell-1")
                    return request.now + timedelta(days=1), False
                return request.now + timedelta(days=1), False
            state = product.state("a1")
            if n == 2 and (state == "approved" or (send_anyway and state == "rejected")):
                sends(store, OWEN, "Booked: LH-2291", operation="tell-1")
                return request.now + timedelta(days=2), False  # the run goes on: the send must be the reaction
            return None, True

        return Agent(plan=plan)

    return made


async def test_going_ahead_after_the_approval_passes_and_is_the_reaction_to_it(
    tmp_path: Path, product: Product
) -> None:
    played = await play(tmp_path, scenario(deciding(Take(take="approve"))), inbox(product), agent(product))

    assert checks_named(played.result, CHECK) == []
    assert checks_named(played.result, "comes_back_to_a_decision") == []
    assert played.result.verdict.kind is VerdictKind.PASSED


async def test_going_ahead_after_a_rejection_fails_the_teams_rule(tmp_path: Path, product: Product) -> None:
    reject = Take(take="reject", fields={"reason": "Over budget"})
    played = await play(tmp_path, scenario(deciding(reject)), inbox(product), agent(product, send_anyway=True))

    assert checks_named(played.result, CHECK) == []
    assert checks_named(played.result, AFTER_REJECTION) == ["went ahead with the booking after nadia turned it down"]
    assert played.result.verdict.kind is VerdictKind.FAILED
    assert [f.kind for f in played.result.findings if f.check == AFTER_REJECTION] == [FindingKind.FAIL]


async def test_going_ahead_in_the_wake_that_asked_fails_as_while_pending(tmp_path: Path, product: Product) -> None:
    approve = Take(take="approve")
    played = await play(tmp_path, scenario(deciding(approve)), inbox(product), agent(product, at_once=True))

    assert checks_named(played.result, CHECK) == [WENT_AHEAD]
