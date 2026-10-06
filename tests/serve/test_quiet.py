"""A world can be waited on to go quiet, closing one waits for that by default, and a call that comes for a world
after it closed is kept in the lobby as that world's late call rather than as a stray.

Moments are recorded, never summed: each test asserts on the order of what happened (the last call of a turn
before the wait returned), so a slow runner changes only how long it takes."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta

from slack_sdk.errors import SlackApiError

from minutehand.adapters.control.wire import Quiet
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, event_receiver, spec


@dataclass
class Turn:
    """A service's turn still running in the background: a call every `gap` seconds, `calls` of them."""

    made: list[float] = field(default_factory=list)
    finished: threading.Event = field(default_factory=threading.Event)


@contextmanager
def running_turn(served: Served, token: str, *, calls: int, gap: float) -> Iterator[Turn]:
    turn = Turn()
    slack = served.slack(token)

    def work() -> None:
        try:
            for _ in range(calls):
                time.sleep(gap)
                # Refused once the world is closed: the call still happened, and is what is asserted on.
                with suppress(SlackApiError):
                    slack.auth_test()
                turn.made.append(time.monotonic())
        finally:
            turn.finished.set()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        yield turn
    finally:
        thread.join(30)


def test_quiet_returns_only_after_the_turn_still_running_has_made_its_last_call(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-quiet")))
    try:
        served.slack("xoxb-quiet").auth_test()
        with running_turn(served, "xoxb-quiet", calls=5, gap=0.1) as turn:
            quieted = world.quiet(quiet_for=timedelta(milliseconds=600), at_most=timedelta(seconds=20))
            returned = time.monotonic()
            assert quieted.quiet, quieted
            assert turn.finished.is_set() and turn.made[-1] <= returned
        assert len(world.calls()) == 6
        assert quieted.last_call is not None and "auth.test" in quieted.last_call
    finally:
        served.client.close_world(world.world_id, quiet=False)


def test_quiet_gives_up_at_its_bound_and_says_what_was_still_going_on(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-noisy")))
    try:
        served.slack("xoxb-noisy").auth_test()
        with running_turn(served, "xoxb-noisy", calls=40, gap=0.05) as turn:
            quieted = world.quiet(quiet_for=timedelta(seconds=5), at_most=timedelta(milliseconds=300))
            assert not quieted.quiet and quieted.busy
            assert not turn.finished.is_set()
    finally:
        served.client.close_world(world.world_id, quiet=False)


def test_a_delivery_still_awaiting_the_services_answer_keeps_the_world_from_being_quiet(served: Served) -> None:
    answer = threading.Event()
    with event_receiver(answers_after=answer) as receiver:
        world = OpenWorld(served.client, served.client.create_world(spec("xoxb-pushing", inbound=receiver.url)))
        saying = threading.Thread(target=lambda: world.say("sofia", "are you there?"), daemon=True)
        try:
            saying.start()
            deadline = time.monotonic() + 20
            while not receiver.pushed and time.monotonic() < deadline:
                time.sleep(0.01)
            assert receiver.pushed, "the event never reached the service"
            held = world.quiet(quiet_for=timedelta(0), at_most=timedelta(milliseconds=200))
            assert not held.quiet and any("sofia says" in b for b in held.busy), held
            answer.set()
            saying.join(20)
            assert world.quiet(quiet_for=timedelta(0), at_most=timedelta(seconds=20)).quiet
        finally:
            answer.set()
            served.client.close_world(world.world_id, quiet=False)


def test_closing_a_world_waits_for_it_to_go_quiet_so_no_call_of_the_turn_comes_late(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-closing")))
    served.slack("xoxb-closing").auth_test()
    with running_turn(served, "xoxb-closing", calls=4, gap=0.1) as turn:
        checked = served.client.close_world(world.world_id, quiet=Quiet(quiet_for=timedelta(milliseconds=600)))
        closed = time.monotonic()
        assert checked.quiet is not None and checked.quiet.quiet
        assert turn.finished.is_set() and turn.made[-1] <= closed
    assert world.late_calls() == []


def test_a_call_that_comes_after_its_world_closed_is_kept_as_that_worlds_late_call(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-late")))
    checked = served.client.close_world(world.world_id, quiet=False)
    assert checked.quiet is None
    try:
        served.slack("xoxb-late").auth_test()
        raise AssertionError("a call for a closed world was answered")
    except SlackApiError as refused:
        assert refused.response.status_code == 502
        assert world.world_id in str(refused.response.data)
    with suppress(SlackApiError):
        served.slack("xoxb-never-claimed").auth_test()
    late = world.late_calls()
    assert [c.exchange.late_for for c in late] == [world.world_id]
    assert late[0].exchange.path.startswith("/api/auth.test")
    strays = [c for c in served.client.unmatched().calls if c.exchange.late_for is None]
    assert any(c.exchange.path.startswith("/api/auth.test") for c in strays)
