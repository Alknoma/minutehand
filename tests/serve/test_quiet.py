"""A world can be waited on to go quiet, closing one waits for that by default, and a call that comes for a world
after it closed is kept in the lobby as that world's late call rather than as a stray.

Nothing here races the clock. A service's turn is the work it does on an event pushed to it: the world is busy
until the service answers the push, so every call the turn makes comes before the world can go quiet, however
slowly a loaded runner makes them. A turn that only slept between its calls raced the quiet window instead: under
load a gap grew past it, the world was quiet by definition, and its next call came late."""

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
    """A service's turn on an event pushed to it: `calls` calls to the world, then its answer to the push."""

    world: OpenWorld
    made: list[float] = field(default_factory=list)
    answered: threading.Event = field(default_factory=threading.Event)


@contextmanager
def turn_on_a_push(served: Served, token: str, *, calls: int) -> Iterator[Turn]:
    """A world whose service is pushed an event and makes `calls` calls before it answers; yielded once the push has
    reached the service, so the turn is under way."""
    slack = served.slack(token)
    made: list[float] = []

    def work() -> None:
        for _ in range(calls):
            # Refused once the world is closed: the call still happened, and is what is asserted on.
            with suppress(SlackApiError):
                slack.auth_test()
            made.append(time.monotonic())

    with event_receiver(working=work) as receiver:
        world = OpenWorld(served.client, served.client.create_world(spec(token, inbound=receiver.url)))
        turn = Turn(world=world, made=made)

        def push() -> None:
            try:
                world.say("sofia", "are you there?")
            finally:
                turn.answered.set()

        saying = threading.Thread(target=push, daemon=True)
        saying.start()
        deadline = time.monotonic() + 20
        while not receiver.pushed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert receiver.pushed, "the event never reached the service"
        try:
            yield turn
        finally:
            saying.join(30)


def test_quiet_returns_only_after_the_turn_still_running_has_made_its_last_call(served: Served) -> None:
    with turn_on_a_push(served, "xoxb-quiet", calls=5) as turn:
        try:
            quieted = turn.world.quiet(quiet_for=timedelta(milliseconds=600), at_most=timedelta(seconds=30))
            returned = time.monotonic()
            assert quieted.quiet, quieted
            assert turn.answered.is_set() and len(turn.made) == 5 and turn.made[-1] <= returned
            assert len([c for c in turn.world.calls() if c.exchange.path.startswith("/api/auth.test")]) == 5
            assert quieted.last_call is not None and "auth.test" in quieted.last_call
        finally:
            served.client.close_world(turn.world.world_id, quiet=False)


def test_quiet_gives_up_at_its_bound_and_says_what_was_still_going_on(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-noisy")))
    try:
        served.slack("xoxb-noisy").auth_test()
        quieted = world.quiet(quiet_for=timedelta(hours=1), at_most=timedelta(milliseconds=300))
        assert not quieted.quiet
        assert quieted.waited >= timedelta(milliseconds=300)
        assert (
            len(quieted.busy) == 1 and "before the wait gave up" in quieted.busy[0] and "auth.test" in quieted.busy[0]
        )
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
    with turn_on_a_push(served, "xoxb-closing", calls=4) as turn:
        checked = served.client.close_world(turn.world.world_id, quiet=Quiet(quiet_for=timedelta(milliseconds=600)))
        closed = time.monotonic()
        assert checked.quiet is not None and checked.quiet.quiet
        assert turn.answered.is_set() and len(turn.made) == 4 and turn.made[-1] <= closed
    assert turn.world.late_calls() == []


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
