"""A person uses the agent's own product unprompted: the scenario says when, and with what; Minutehand makes the call
as them, records it as their act, and wakes the agent as a decision does.

Each run test plays the real loop over the real store against a product served over real HTTP, with an inbox that
declares only `uses`: a website's visitor, whom nothing waits on, who opens pages and writes to the agent."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import WakeReason, WakeRequest
from minutehand.domain.inboxes import HttpInbox
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, InboxUseSnapshot, Operation, WorldEvent
from tests.inboxes.product import Product, serving
from tests.inboxes.support import NADIA, T0, TOKENS, Agent, deciding, hours, play, scenario


@pytest.fixture
def product() -> Iterator[Product]:
    with serving(Product(tokens=dict(TOKENS))) as served:
        yield served


@pytest.fixture(autouse=True)
def nadias_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NADIA_TOKEN", TOKENS[NADIA])


def site(product: Product) -> HttpInbox:
    """A website where nothing waits on anyone: its visitors open pages and write to the agent."""
    events = f"{product.base}/events"
    return HttpInbox.model_validate(
        {
            "name": "site",
            "uses": [
                {
                    "name": "views",
                    "description": "Opens a page",
                    "request": {
                        "kind": "template",
                        "method": "POST",
                        "url": events,
                        "body": {
                            "visitor": "{person.email}",
                            "kind": "view",
                            "page": "{input.page}",
                            "at": "{clock.now}",
                        },
                    },
                    "inputs": [{"name": "page", "description": "The page's path"}],
                },
                {
                    "name": "writes",
                    "request": {
                        "kind": "template",
                        "method": "POST",
                        "url": events,
                        "body": {"visitor": "{person.email}", "kind": "reply", "text": "{input.text}"},
                    },
                    "inputs": [{"name": "text", "description": "What they write"}],
                },
            ],
        }
    )


def visiting(*happenings: dict[str, object]) -> Scenario:
    scn = scenario(deciding(hours=2), days=1)
    return Scenario.model_validate(
        {
            **scn.model_dump(mode="json"),
            "happenings": [{"kind": "uses", "person": "nadia", "inbox": "site", **h} for h in happenings],
        }
    )


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)


def listening(woken: list[WakeRequest]) -> Agent:
    def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
        del n
        woken.append(request)
        return None, False

    return Agent(plan=plan)


def uses(events: list[WorldEvent]) -> list[WorldEvent]:
    return [e for e in events if isinstance(e.after, InboxUseSnapshot)]


def snapshot(event: WorldEvent) -> InboxUseSnapshot:
    assert isinstance(event.after, InboxUseSnapshot)
    return event.after


async def test_a_use_is_made_as_its_person_at_its_moment_and_wakes_the_agent(tmp_path: Path, product: Product) -> None:
    scn = visiting(
        {"use": "views", "after": "PT1H", "inputs": {"page": "/pricing"}},
        {"use": "writes", "after": "PT2H", "inputs": {"text": "Is a project enough for a candle brand?"}},
    )
    woken: list[WakeRequest] = []
    played = await play(tmp_path, scn, site(product), lambda _: listening(woken))

    assert product.events == [
        {"visitor": NADIA, "kind": "view", "page": "/pricing", "at": (T0 + hours(1)).isoformat()},
        {"visitor": NADIA, "kind": "reply", "text": "Is a project enough for a candle brand?"},
    ]
    recorded = uses(played.store.events())
    assert [(e.actor, e.operation, e.sim_time, snapshot(e).use, snapshot(e).taken) for e in recorded] == [
        (Actor.PERSON, Operation.CREATE, T0 + hours(1), "views", None),
        (Actor.PERSON, Operation.UPDATE, T0 + hours(1), "views", True),
        (Actor.PERSON, Operation.CREATE, T0 + hours(2), "writes", None),
        (Actor.PERSON, Operation.UPDATE, T0 + hours(2), "writes", True),
    ]
    assert snapshot(recorded[1]).inputs == {"page": "/pricing"} and snapshot(recorded[1]).person == "nadia"
    assert [(w.now, w.reason) for w in woken[1:]] == [
        (T0 + hours(1), WakeReason.PERSON_REPLIED),
        (T0 + hours(2), WakeReason.PERSON_REPLIED),
    ]


async def test_a_use_with_a_gap_comes_that_long_after_their_last_drawn_by_the_seed(
    tmp_path: Path, product: Product
) -> None:
    gap = {"min": "PT10M", "max": "PT50M"}
    scn = visiting(
        {"use": "views", "after": "PT1H", "inputs": {"page": "/"}},
        {"use": "views", "gap": gap, "inputs": {"page": "/pricing"}},
        {"use": "writes", "gap": gap, "inputs": {"text": "Is a project enough?"}},
    )
    moments = scn.happening_moments()
    played = await play(tmp_path, scn, site(product), lambda _: listening([]))

    taken = [e.sim_time for e in uses(played.store.events()) if snapshot(e).taken]
    assert taken == moments
    assert moments[0] == T0 + hours(1)
    assert all(minutes(10) <= b - a <= minutes(50) for a, b in pairwise(moments))
    others = [scn.seeded(seed).happening_moments() for seed in range(scn.seed + 1, scn.seed + 6)]
    assert any(m != moments for m in others), "five other seeds drew the same moments"
    assert [scn.seeded(scn.seed).happening_moments() for _ in range(2)] == [moments, moments]


def test_a_use_both_after_a_time_and_a_gap_is_refused() -> None:
    with pytest.raises(ValueError, match="`after` a fixed time or a `gap` after the last, not both"):
        visiting({"use": "views", "after": "PT1H", "gap": {"min": "PT1M", "max": "PT2M"}, "inputs": {"page": "/"}})


async def test_a_use_the_product_refuses_is_recorded_refused_and_the_run_goes_on(
    tmp_path: Path, product: Product
) -> None:
    product.refuse = True
    scn = visiting(
        {"use": "views", "after": "PT1H", "inputs": {"page": "/"}},
        {"use": "views", "after": "PT2H", "inputs": {"page": "/"}},
    )
    played = await play(tmp_path, scn, site(product), lambda _: listening([]))

    recorded = uses(played.store.events())
    assert [e.entity.external_id for e in recorded] == ["use:1", "use:1", "use:2", "use:2"]  # the same words, twice
    answered = [snapshot(e) for e in recorded if e.operation is Operation.UPDATE]
    assert [(u.taken, u.refused) for u in answered] == [
        (False, '409: {"error": "events are closed"}'),
        (False, '409: {"error": "events are closed"}'),
    ]


@pytest.mark.parametrize(
    ("happening", "said"),
    [
        ({"use": "views", "inputs": {"page": "/"}, "inbox": "shop"}, "inbox shop, which"),
        ({"use": "buys", "inputs": {}}, "declares no such use"),
        ({"use": "views", "inputs": {"page": "/", "colour": "red"}}, "gives use 'views' colour"),
        ({"use": "views", "inputs": {}}, "without page"),
    ],
)
async def test_a_use_the_agent_file_does_not_declare_as_given_is_refused(
    tmp_path: Path, product: Product, happening: dict[str, object], said: str
) -> None:
    with pytest.raises(RunRefused, match=said):
        await play(tmp_path, visiting(happening), site(product), lambda _: listening([]))


@pytest.mark.parametrize(
    ("declared", "said"),
    [
        ({"name": "site"}, "neither `pending` nor `uses`"),
        (
            {
                "name": "site",
                "uses": [{"name": "views", "request": {"kind": "template", "url": "http://x/{item.id}"}}],
            },
            "item.id",
        ),
        (
            {
                "name": "site",
                "uses": [
                    {"name": "views", "request": {"kind": "template", "url": "http://x/a"}},
                    {"name": "views", "request": {"kind": "template", "url": "http://x/b"}},
                ],
            },
            "declares use views twice",
        ),
        (
            {
                "name": "site",
                "decisions": [{"name": "ok", "request": {"kind": "template", "url": "http://x/"}}],
                "uses": [{"name": "views", "request": {"kind": "template", "url": "http://x/a"}}],
            },
            "decisions with no `pending`",
        ),
    ],
)
def test_an_inbox_declaring_uses_wrongly_is_refused(declared: dict[str, object], said: str) -> None:
    with pytest.raises(ValueError, match=said):
        HttpInbox.model_validate(declared)


def test_a_use_with_no_inputs_takes_none() -> None:
    declared = HttpInbox.model_validate(
        {
            "name": "site",
            "uses": [{"name": "leaves", "request": {"kind": "template", "method": "POST", "url": "http://x/"}}],
        }
    )
    found = declared.use("leaves")
    assert found is not None
    found.refuse_inputs({}, "nadia")
    with pytest.raises(ValueError, match="takes nothing"):
        found.refuse_inputs({"page": "/"}, "nadia")
