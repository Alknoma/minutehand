"""A harness that drives its agent itself marks where each step begins and ends, or only moves the world's clock and
has steps inferred; either way the checks that read wakes run, and a world with neither is not judged, never
passed.

Before, a standing world had no wakes at all: every check that reads them was blocked, the scorecard said "woke 0
times", and the verdict read Passed over whatever the agent had done."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.steps import STEP
from minutehand.checks.runner import NOTHING_ASSESSED
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import VerdictKind
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient, Refused
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, dm, spec


@pytest.fixture(scope="module")
def state(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("steps")


@pytest.fixture(scope="module")
def served(state: Path) -> Iterator[Served]:
    with serve_in_background(state) as url, MinutehandClient(url) as client:
        yield Served(url=url, client=client, environment=client.environment())


def _ask(served: Served, token: str, text: str) -> None:
    channel = dm(served, token, "sofia@example.com")
    served.slack(token).chat_postMessage(channel=channel, text=text)


def test_marked_steps_are_the_worlds_wakes_and_an_idle_one_is_seen(served: Served) -> None:
    idle = "[{id: no_idle_steps, count: {wakes: {changed_world: false}}, at_most: 0, severity: review}]"
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-steps-marked", assess=idle)))
    start = world.view.now
    with world.step(at=start, reason="morning run"):
        _ask(served, "xoxb-steps-marked", "Could you confirm the venue, please?")
    world.advance(timedelta(hours=1))
    with world.step(reason="the hourly look"):
        pass
    checked = world.checks().result
    assert all(e.entity != STEP for e in world.events()), "a step's marks are not the world's events"
    sent = [e for e in world.events() if e.after is not None and e.entity.kind.value == "message"]
    closed = world.close()

    assert closed.result.effectiveness.wakes == 2 and closed.result.effectiveness.idle_wakes == 1
    assert [e.wake for e in sent] == [1], "the message was written in the first step"
    [seen] = [f for f in checked.findings if f.check == "no_idle_steps"]
    assert seen.kind is FindingKind.REVIEW and seen.at == start + timedelta(hours=1)
    assert closed.result.verdict.kind is VerdictKind.UNFINISHED, closed.result.verdict.words


def test_moving_the_clock_forward_infers_steps_and_a_mark_replaces_them(served: Served, state: Path) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-steps-inferred")))
    _ask(served, "xoxb-steps-inferred", "Has the venue been booked yet?")
    moved = world.advance(timedelta(hours=2))
    world.advance(to=moved.now)  # to where the clock already is: no step
    world.advance(timedelta(hours=3))
    inferred = world.checks().result.effectiveness
    world.begin_step(reason="marked by hand")
    _ask(served, "xoxb-steps-inferred", "Just checking on the venue.")
    world.end_step()
    closed = world.close()

    assert (inferred.wakes, inferred.idle_wakes) == (3, 2)
    with session.reading(state, world.world_id) as read:
        steps = session.wakes_of(state, world.world_id, read)
    assert [(w.index, w.inferred, w.reason) for w in steps] == [(4, False, "marked by hand")]
    assert closed.result.effectiveness.wakes == 1


def test_a_world_with_no_step_and_nothing_declared_is_not_judged_and_says_why(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-steps-none")))
    _ask(served, "xoxb-steps-none", "Could you send the slides?")
    closed = world.close()

    verdict = closed.result.verdict
    assert verdict.kind is VerdictKind.NOT_JUDGED and closed.result.exit_code == 5
    assert verdict.unjudged == [NOTHING_ASSESSED]
    assert closed.result.effectiveness.waits_opened == 1, "the wait on the silent person is still a fact"
    assert verdict.words.startswith("Not assessed:")


def test_ending_a_step_that_never_began_is_refused(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-steps-refused")))
    with pytest.raises(Refused) as refused:
        world.end_step()
    world.close()
    assert refused.value.status == 409 and "no step is in progress" in refused.value.error
