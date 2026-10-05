"""People behave as declared.

Each test plays one person's declaration through `minutehand run` with the stand-in agent, and reads WHEN (on the
simulated clock) and WHETHER their answer landed from the viewer's `messages`, the record a person reads. The
agent asks Rosa at the start (Monday 2026-08-24 09:00 UTC unless the scenario says otherwise) and relays whatever
she answers to Owen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from minutehand.adapters.control.wire import CreateWorld
from tests.acceptance.support import (
    OWEN,
    PYTHON,
    ROSA,
    SILENT,
    STANDIN,
    TOLD_ONLY,
    free_port,
    person,
    run_standin,
    scenario,
    scripted,
    served,
    standin,
    started,
    through_proxy,
    viewer,
)

MONDAY = datetime(2026, 8, 24, 9, tzinfo=UTC)
ANSWER = "The lakeside hall is booked."
Message = dict[str, object]


def played(tmp_path: Path, rosa: dict[str, object], *, start: str | None = None, **behaviour: str) -> list[Message]:
    """The run's messages as the viewer serves them, in record order."""
    story = scenario("people", person("owen", OWEN, TOLD_ONLY), rosa)
    if start is not None:
        story["starts_at"] = start
    finished = run_standin(tmp_path, story, standin(tmp_path, ask=ROSA, relay_to=OWEN, **behaviour))
    assert finished.exit in (0, 1, 3), finished.explain()
    with viewer(tmp_path, tmp_path / "state") as view:
        return cast(list[Message], view.get(f"/api/runs/{finished.run_id}/messages")["messages"])


def said_by_people(messages: list[Message]) -> list[tuple[datetime, str]]:
    return [(datetime.fromisoformat(str(m["at"])), str(m["text"])) for m in messages if m["actor"] == "person"]


def relays(messages: list[Message]) -> list[tuple[datetime, str]]:
    return [(datetime.fromisoformat(str(m["at"])), str(m["text"])) for m in messages if m["to"] == ["Owen Example"]]


def test_a_scripted_delay_lands_the_answer_exactly_that_long_after_the_ask(tmp_path: Path) -> None:
    messages = played(tmp_path, person("rosa", ROSA, scripted({"to_ask": 1, "text": ANSWER}, hours=5)))
    assert said_by_people(messages) == [(MONDAY + timedelta(hours=5), ANSWER)]


def test_working_hours_hold_an_answer_until_the_persons_own_next_working_day(tmp_path: Path) -> None:
    """Asked on Friday at 20:00 UTC (16:00 in New York) with a two-hour delay, by someone working 9 to 5 in New York,
    weekdays only: the answer would be due at 18:00 their time, so it lands inside their hours on Monday, never at
    the weekend."""
    hours = {"timezone": "America/New_York", "opens": "09:00", "closes": "17:00", "weekdays_only": True}
    rosa = person("rosa", ROSA, scripted({"to_ask": 1, "text": ANSWER}, hours=2), working_hours=hours)
    messages = played(tmp_path, rosa, start="2026-08-28T20:00:00Z")
    [(at, text)] = said_by_people(messages)
    assert text == ANSWER
    assert at >= datetime(2026, 8, 28, 22, tzinfo=UTC), "never before the delay has passed"
    assert at.weekday() == 0 and datetime(2026, 8, 31, 13, tzinfo=UTC) <= at < datetime(2026, 8, 31, 21, tzinfo=UTC)


def test_an_absent_person_does_not_answer_until_the_absence_is_over(tmp_path: Path) -> None:
    rosa = person(
        "rosa",
        ROSA,
        scripted({"to_ask": 1, "text": ANSWER}, hours=1),
        absences=[{"trigger": "at_start", "lasts": "P2D", "reason": "on leave"}],
    )
    [(at, text)] = said_by_people(played(tmp_path, rosa))
    assert text == ANSWER
    assert at >= MONDAY + timedelta(days=2)


def test_a_silent_person_never_answers_however_often_asked(tmp_path: Path) -> None:
    messages = played(tmp_path, person("rosa", ROSA, SILENT), follow_ups="3", every_hours="24")
    assert len([m for m in messages if m["to"] == ["Rosa Example"]]) == 4
    assert said_by_people(messages) == []


def test_nobody_answers_a_question_they_were_not_declared_to_answer(tmp_path: Path) -> None:
    """Rosa has an answer for her second question only, and is asked once; Owen has none at all and is asked too."""
    rosa = person("rosa", ROSA, scripted({"to_ask": 2, "text": ANSWER}, hours=1))
    story = scenario("undeclared", person("owen", OWEN, TOLD_ONLY), rosa)
    finished = run_standin(tmp_path, story, standin(tmp_path, ask=ROSA, relay_to=OWEN, question="Owen too?"))
    with viewer(tmp_path, tmp_path / "state") as view:
        messages = cast(list[Message], view.get(f"/api/runs/{finished.run_id}/messages")["messages"])
    assert [m["to"] for m in messages] == [["Rosa Example"]]
    assert said_by_people(messages) == []


def test_a_button_press_lands_after_the_persons_delay_and_reaches_the_agent(tmp_path: Path) -> None:
    rosa = person("rosa", ROSA, scripted({"to_ask": 1, "press": {"label": "Approve"}}, hours=3))
    messages = played(tmp_path, rosa, button="Approve")
    assert relays(messages) == [(MONDAY + timedelta(hours=3), "Relaying: pressed Approve")]


def test_a_form_submitted_after_a_press_carries_what_the_person_typed(tmp_path: Path) -> None:
    press = {"label": "Answer", "form": [{"value": "Lakeside hall, the 14th"}]}
    rosa = person("rosa", ROSA, scripted({"to_ask": 1, "press": press}, hours=3))
    messages = played(tmp_path, rosa, button="Answer", form="1")
    assert relays(messages) == [(MONDAY + timedelta(hours=3), "Relaying: typed Lakeside hall, the 14th")]


def test_a_standing_world_lists_a_scripted_answer_still_to_land_as_owed(tmp_path: Path) -> None:
    """A harness that moves the clock itself learns where to move it from `WorldView.owed`: "what falls due as the
    clock moves" (`docs/serve.md`). Rosa is asked; her answer is due five hours later."""
    port, secret, token = free_port(), "owed-secret", "xoxb-owed"
    rosa = person("rosa", ROSA, scripted({"to_ask": 1, "text": ANSWER}, hours=5))
    with served(tmp_path) as server:
        receiver = {"STANDIN_PORT": str(port), "STANDIN_SECRET": secret}
        with started(
            [PYTHON, str(STANDIN)], receiver, tmp_path / "receiver.log", ready=f"http://127.0.0.1:{port}/report"
        ):
            spec = CreateWorld.model_validate(
                {
                    "seed": {"starts_at": "2026-08-24T09:00:00Z", "people": [person("owen", OWEN, TOLD_ONLY), rosa]},
                    "claims": {"tokens": [token]},
                    "inbound": [
                        {"provider": "slack", "url": f"http://127.0.0.1:{port}/slack/events", "secret": secret}
                    ],
                    "scripted_people": True,
                }
            )
            opened = server.client.create_world(spec)
            with through_proxy(server, tmp_path / "ca.pem") as http:
                auth = {"Authorization": f"Bearer {token}"}
                user = http.post("https://slack.com/api/users.lookupByEmail", data={"email": ROSA}, headers=auth)
                channel = http.post(
                    "https://slack.com/api/conversations.open", data={"users": user.json()["user"]["id"]}, headers=auth
                )
                posted = http.post(
                    "https://slack.com/api/chat.postMessage",
                    data={"channel": channel.json()["channel"]["id"], "text": "Rosa, which venue is booked?"},
                    headers=auth,
                )
                assert posted.json()["ok"] is True
            owed = [o.at for o in server.client.world(opened.world_id).owed]
            fired = server.client.advance(opened.world_id, by=timedelta(hours=6)).fired
            assert [f.at for f in fired] == [MONDAY + timedelta(hours=5)], "the answer does fall due then"
            assert owed == [MONDAY + timedelta(hours=5)]
