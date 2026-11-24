"""People carry the work handed to them (docs/design-transitions.md, "Work a person carries"): an issue assigned to a
person and not finished is theirs to carry over their own time, one piece at a time. They pick it up as an answer of
theirs would land (work they were on as the run began, from their first available moment) and move it to a started
state; they finish it once the work it takes has passed in their available
time (what Jira says is left of it, else their pace, else as long as they judge it takes them); a workflow that
needs a review first gets one; an unfinished
issue that blocks it holds it back, and it resumes with what was left; a take pins the move it names."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from minutehand.adapters.providers.jira.state import issue_ref
from minutehand.application.people import People
from minutehand.application.replier import PeopleReplier, work_part
from minutehand.domain.clock import DrawnFrom
from minutehand.domain.experiment import ReplyAt
from minutehand.domain.scenario import Person, Scenario, Silent
from minutehand.domain.world import (
    OWN,
    CourseSnapshot,
    CourseStatus,
    EffortFrom,
    EntityKind,
    EntityRef,
    PendingStatus,
    TransitionSnapshot,
)
from tests.providers.jira.jira_site import API, NOOR, SCENARIO, START, TOMAS, Site, ok
from tests.support.people import people_engine, people_model, people_requests

SILENT = {"reply": Silent().model_dump(mode="json")}
HOURLY = {"reply_within": {"min": "PT1H", "max": "PT1H"}}
"""Answers, and picks work up, exactly an hour of their available time after it reaches them."""


def _scenario(**people: dict[str, Any]) -> Scenario:
    """The site's scenario with the engine playing Jira, each named person changed as given; the rest silent."""
    body = SCENARIO.model_dump(mode="json")
    body["transitions_on"] = ["jira"]
    for person in body["people"]:
        person.update(people.get(person["key"], SILENT))
    return Scenario.model_validate(body)


def _engine(site: Site, scenario: Scenario) -> People:
    return people_engine(scenario, {"jira": site.provider}, people_model())


def _ref(site: Site, key: str) -> EntityRef:
    issue = site.jira.find_issue(key)
    assert issue is not None
    return issue_ref(issue.id)


def _person(key: str) -> Person:
    return next(p for p in _scenario().people if p.key == key)


def _course(site: Site, person: str, key: str) -> CourseSnapshot:
    item = _ref(site, key)
    found = [
        CourseSnapshot.model_validate_json(s.body)
        for s in site.store.children(OWN, EntityKind.COURSE, person, limit=500)
        if CourseSnapshot.model_validate_json(s.body).item == item
    ]
    assert found, f"no course of {person}'s on {key}"
    return found[-1]


def _moved(site: Site) -> list[tuple[str | None, str, str]]:
    """Every move a person made, in order: who, the move's name, the state it reached."""
    return [
        (e.after.who, e.after.name, e.after.to_state)
        for e in site.store.events()
        if isinstance(e.after, TransitionSnapshot) and e.after.who is not None
    ]


async def _act_at(engine: People, site: Site, person: str) -> tuple[str, str]:
    """The person's one move owed now, after a look: the clock moved to its moment, and taken. Its name and the state
    it reached."""
    await engine.look(site.store, site.clock)
    owed = [
        h
        for h in engine.held(person, site.store)
        if h.pending.status is PendingStatus.PENDING and h.pending.due_at is not None
    ]
    assert len(owed) == 1, f"{person} owes {len(owed)} moves: {owed}"
    due = owed[0].pending.due_at
    assert due is not None
    site.clock.jump(max(due, site.clock.now()))
    acted = await engine.act(owed[0].ref, site.store, site.clock)
    assert acted.transition is not None, acted.failure
    return acted.transition.name, acted.transition.to_state


async def test_assigned_work_is_picked_up_started_reviewed_and_finished_when_what_is_left_has_passed(
    site: Site,
) -> None:
    """LAUNCH-1 has 8h estimated and 3h logged: Tomas picks it up an hour after it reaches him, starts it, and
    finishes it five hours of his time later. Its workflow reaches Done only through review, so he submits it for
    review, then approves it when he comes back to it."""
    engine = _engine(site, _scenario(tomas=HOURLY))

    looked = await engine.look(site.store, site.clock)
    pick = [b for b in looked.booked if b.person == "tomas"]
    assert [(b.item, b.at) for b in pick] == [(_ref(site, "LAUNCH-1"), START + timedelta(hours=1))]
    assert _course(site, "tomas", "LAUNCH-1").status is CourseStatus.WORKING

    assert await _act_at(engine, site, "tomas") == ("Start work", "In Progress")
    begun = _course(site, "tomas", "LAUNCH-1")
    assert begun.effort == timedelta(hours=5) and begun.effort_from is EffortFrom.ESTIMATE
    assert begun.finishes_at == START + timedelta(hours=6) == begun.expects_at

    assert await _act_at(engine, site, "tomas") == ("Submit for review", "In Review")
    assert site.clock.now() == START + timedelta(hours=6)
    assert await _act_at(engine, site, "tomas") == ("Approve", "Done")
    assert site.clock.now() == START + timedelta(hours=7), "he comes back to it as an answer of his would land"
    assert _course(site, "tomas", "LAUNCH-1").status is CourseStatus.FINISHED
    assert (await engine.look(site.store, site.clock)).booked == [], "nothing else is his"


async def test_a_person_carries_one_piece_at_a_time_the_earliest_due_first(site: Site) -> None:
    """Tomas also gets FIELD-1, due later than LAUNCH-1: nothing is booked on it until LAUNCH-1 is finished."""
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": TOMAS}), 204)
    engine = _engine(site, _scenario(tomas=HOURLY))

    booked = [b for b in (await engine.look(site.store, site.clock)).booked if b.person == "tomas"]
    assert [b.item for b in booked] == [_ref(site, "LAUNCH-1")], "the one due first; FIELD-1 waits behind it"
    assert _course(site, "tomas", "FIELD-1").status is CourseStatus.QUEUED

    for _ in range(3):  # LAUNCH-1: start, review, done
        await _act_at(engine, site, "tomas")
    assert _course(site, "tomas", "LAUNCH-1").status is CourseStatus.FINISHED
    looked = await engine.look(site.store, site.clock)
    assert [b.item for b in looked.booked if b.person == "tomas"] == [_ref(site, "FIELD-1")], "now the next one"
    assert _course(site, "tomas", "FIELD-1").status is CourseStatus.WORKING


async def test_work_already_under_way_is_carried_on_before_anything_new_whatever_is_due_first(site: Site) -> None:
    """FIELD-1, due nothing, is already in progress when it reaches Tomas; LAUNCH-1, due Friday, is not begun. He
    carries on with FIELD-1 first, as people finish what is under way before they start something new."""
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": TOMAS}), 204)
    transitions = ok(await site.http.get(f"{API}/issue/FIELD-1/transitions"))["transitions"]
    started = next(t["id"] for t in transitions if t["to"]["statusCategory"]["key"] == "indeterminate")
    ok(await site.http.post(f"{API}/issue/FIELD-1/transitions", json={"transition": {"id": started}}), 204)
    engine = _engine(site, _scenario(tomas=HOURLY))

    booked = [b for b in (await engine.look(site.store, site.clock)).booked if b.person == "tomas"]
    assert [b.item for b in booked] == [_ref(site, "FIELD-1")]
    assert _course(site, "tomas", "FIELD-1").under_way
    assert _course(site, "tomas", "LAUNCH-1").status is CourseStatus.QUEUED


async def test_work_under_way_as_the_run_began_is_carried_on_from_the_start_not_as_an_answer_would_land(
    site: Site,
) -> None:
    """VAULT-1 was Iris's and in progress when the run began. She was on it already, so she carries on with it from the
    run's first moment, not three hours in as an answer of hers would land; it moves nothing, and she finishes it as
    long as she judges it takes her after that."""
    engine = _engine(site, _scenario(iris={"reply_within": {"min": "PT3H", "max": "PT3H"}}))

    booked = [b for b in (await engine.look(site.store, site.clock)).booked if b.person == "iris"]
    assert [(b.item, b.at) for b in booked] == [(_ref(site, "VAULT-1"), START)]
    assert booked[0].drawn is not None and booked[0].drawn.source is DrawnFrom.CARRIED, "nothing is drawn"
    assert _course(site, "iris", "VAULT-1").under_way_at_start

    acted = await engine.act(booked[0].pending, site.store, site.clock)
    assert acted.transition is None and acted.failure is None, "it is in progress already: carrying on moves nothing"
    begun = _course(site, "iris", "VAULT-1")
    assert (begun.started_at, begun.effort, begun.effort_from) == (START, timedelta(hours=6), EffortFrom.PERSON)
    assert begun.finishes_at == START + timedelta(hours=6)


async def test_work_handed_over_already_under_way_is_taken_up_as_an_answer_would_land(site: Site) -> None:
    """FIELD-1 was Noor's and in progress as the run began. Once the run is under way it is handed to Tomas: he was not
    on it, so he takes it up three hours later as an answer of his would land, and still before anything new."""
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": NOOR}), 204)
    transitions = ok(await site.http.get(f"{API}/issue/FIELD-1/transitions"))["transitions"]
    started = next(t["id"] for t in transitions if t["to"]["statusCategory"]["key"] == "indeterminate")
    ok(await site.http.post(f"{API}/issue/FIELD-1/transitions", json={"transition": {"id": started}}), 204)
    site.clock.begin_wake()
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": TOMAS}), 204)
    engine = _engine(site, _scenario(tomas={"reply_within": {"min": "PT3H", "max": "PT3H"}}))

    booked = [b for b in (await engine.look(site.store, site.clock)).booked if b.person == "tomas"]
    assert [(b.item, b.at) for b in booked] == [(_ref(site, "FIELD-1"), START + timedelta(hours=3))]
    assert booked[0].drawn is not None and booked[0].drawn.source is DrawnFrom.WINDOW
    handed = _course(site, "tomas", "FIELD-1")
    assert handed.under_way and not handed.under_way_at_start


async def test_work_held_back_by_an_unfinished_blocker_waits_then_resumes_with_what_was_left(site: Site) -> None:
    """Once Tomas has begun LAUNCH-1, FIELD-1 (open, nobody's) is found to block it: his finish is taken off the
    clock, with the work left on it kept. When FIELD-1 is done he finishes the rest from then."""
    engine = _engine(site, _scenario(tomas=HOURLY))
    await _act_at(engine, site, "tomas")  # picked up and started at START+1h
    finish = [b for b in (await engine.look(site.store, site.clock)).booked if b.person == "tomas"]
    assert [b.at for b in finish] == [START + timedelta(hours=6)]
    site.clock.jump(START + timedelta(hours=3))
    ok(
        await site.http.post(
            f"{API}/issueLink",
            json={"type": {"name": "Blocks"}, "outwardIssue": {"key": "FIELD-1"}, "inwardIssue": {"key": "LAUNCH-1"}},
        ),
        201,
    )

    looked = await engine.look(site.store, site.clock)
    assert [(b.person, b.at) for b in looked.moved if b.person == "tomas"] == [("tomas", None)], "off the clock"
    held = _course(site, "tomas", "LAUNCH-1")
    assert held.status is CourseStatus.HELD and held.held_by == [_ref(site, "FIELD-1")]
    assert held.left == timedelta(hours=3), "five hours of work, two of them done"

    site.clock.jump(START + timedelta(hours=10))
    transitions = ok(await site.http.get(f"{API}/issue/FIELD-1/transitions"))["transitions"]
    done = next(t["id"] for t in transitions if t["to"]["statusCategory"]["key"] == "done")
    ok(await site.http.post(f"{API}/issue/FIELD-1/transitions", json={"transition": {"id": done}}), 204)

    looked = await engine.look(site.store, site.clock)
    resumed = [b for b in [*looked.booked, *looked.moved] if b.person == "tomas" and b.at is not None]
    assert [b.at for b in resumed] == [START + timedelta(hours=13)], "the three hours left, from when it was freed"
    assert _course(site, "tomas", "LAUNCH-1").status is CourseStatus.WORKING


async def test_work_blocked_before_it_is_begun_is_not_picked_up(site: Site) -> None:
    ok(
        await site.http.post(
            f"{API}/issueLink",
            json={"type": {"name": "Blocks"}, "outwardIssue": {"key": "FIELD-1"}, "inwardIssue": {"key": "LAUNCH-1"}},
        ),
        201,
    )
    engine = _engine(site, _scenario(tomas=HOURLY))

    assert [b for b in (await engine.look(site.store, site.clock)).booked if b.person == "tomas"] == []
    queued = _course(site, "tomas", "LAUNCH-1")
    assert queued.status is CourseStatus.QUEUED and queued.held_by == [_ref(site, "FIELD-1")]


async def test_a_take_pins_the_finish_and_the_person_still_quotes_the_estimate_so_the_slip_shows(site: Site) -> None:
    """Tomas is pinned to finish LAUNCH-1 two days after it reaches him. He believes the five hours Jira says are
    left, so past them he knows it is taking longer; it is done at the pinned moment."""
    takes = [{"provider": "jira", "take": "done", "after": "P2D"}]
    engine = _engine(site, _scenario(tomas={**HOURLY, "takes": takes}))

    await _act_at(engine, site, "tomas")
    begun = _course(site, "tomas", "LAUNCH-1")
    assert begun.effort_from is EffortFrom.TAKE and begun.finishes_at == START + timedelta(days=2)
    assert begun.expects_at == START + timedelta(hours=6), "what he believes: the estimate"

    site.clock.jump(START + timedelta(hours=8))
    told = work_part(_person("tomas"), site.store, site.clock.now())
    assert "LAUNCH-1: Write the release notes" in told and "taking longer" in told

    name, state = await _act_at(engine, site, "tomas")
    assert state == "Done" and site.clock.now() == START + timedelta(days=2), name
    assert _moved(site)[1:] == [
        ("tomas", "Submit for review", "In Review"),
        ("tomas", "Approve", "Done"),
    ], "the take's done walks the workflow to Done, each step its own move"


KITS_TAKE_TWELVE_HOURS = "Packing and shipping the demo kits takes 12 hours of work."


async def test_work_its_tracker_holds_no_estimate_for_takes_as_long_as_they_judge_it_takes_them(site: Site) -> None:
    """FIELD-1 holds no estimate and Noor has no pace: as she takes it up she judges, from what she knows, that it
    takes her twelve hours of work, and finishes it that much of her time later. Catches work finished as soon as a
    person would answer a message: minutes, for a piece of work of days."""
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": NOOR}), 204)
    engine = _engine(site, _scenario(noor={**HOURLY, "facts": [KITS_TAKE_TWELVE_HOURS]}))

    name, state = await _act_at(engine, site, "noor")
    assert state == "In Progress", name
    begun = _course(site, "noor", "FIELD-1")
    assert begun.effort == timedelta(hours=12) and begun.effort_from is EffortFrom.PERSON
    assert begun.finishes_at == START + timedelta(hours=13) == begun.expects_at


async def test_a_pace_the_scenario_gives_is_what_their_work_takes_whatever_they_would_judge(site: Site) -> None:
    """A pace of exactly three hours is what FIELD-1 takes Noor, though she would judge it twelve: the author's
    pace is kept over her judgement."""
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": NOOR}), 204)
    paced = {**HOURLY, "pace": {"min": "PT3H", "max": "PT3H"}, "facts": [KITS_TAKE_TWELVE_HOURS]}
    engine = _engine(site, _scenario(noor=paced))

    await _act_at(engine, site, "noor")
    begun = _course(site, "noor", "FIELD-1")
    assert begun.effort == timedelta(hours=3) and begun.effort_from is EffortFrom.PACE


async def test_a_silent_person_pinned_to_finish_work_with_no_estimate_expects_it_as_it_lands(site: Site) -> None:
    """Noor never speaks, so no model judges how long FIELD-1 takes her: pinned to finish it a day after it reaches
    her, she does, and expects to then. Catches a pinned finish refused for want of a judgement nobody can give."""
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": NOOR}), 204)
    engine = _engine(site, _scenario(noor={**SILENT, "takes": [{"provider": "jira", "take": "done", "after": "P1D"}]}))

    await engine.look(site.store, site.clock)
    while _course(site, "noor", "FIELD-1").status is not CourseStatus.FINISHED:
        await _act_at(engine, site, "noor")
    finished = _course(site, "noor", "FIELD-1")
    assert site.clock.now() == START + timedelta(days=1) == finished.finishes_at == finished.expects_at
    assert finished.effort_from is EffortFrom.TAKE


async def test_a_take_naming_a_start_pins_when_they_pick_it_up_with_its_words(site: Site) -> None:
    takes = [{"provider": "jira", "take": "Start work", "after": "PT2H", "verbatim": "On it from today."}]
    engine = _engine(site, _scenario(tomas={**HOURLY, "takes": takes}))

    assert await _act_at(engine, site, "tomas") == ("Start work", "In Progress")
    assert site.clock.now() == START + timedelta(hours=2)
    comments = ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment"))["comments"]
    assert "On it from today." in str(comments[-1]["body"])
    assert _course(site, "tomas", "LAUNCH-1").finishes_at == START + timedelta(hours=7), "then five hours of work"


async def test_work_reassigned_away_is_gone_from_its_carrier_and_taken_up_by_the_new_assignee(site: Site) -> None:
    engine = _engine(site, _scenario(tomas=HOURLY, noor=HOURLY))
    await _act_at(engine, site, "tomas")
    ok(await site.http.put(f"{API}/issue/LAUNCH-1/assignee", json={"accountId": NOOR}), 204)

    looked = await engine.look(site.store, site.clock)
    assert _course(site, "tomas", "LAUNCH-1").status is CourseStatus.GONE
    assert [b.item for b in looked.booked if b.person == "noor"] == [_ref(site, "LAUNCH-1")]
    assert _moved(site) == [("tomas", "Start work", "In Progress")], "noor has not moved it yet"


async def test_a_person_is_told_where_their_own_work_stands_when_they_speak(site: Site) -> None:
    engine = _engine(site, _scenario(tomas=HOURLY))
    await _act_at(engine, site, "tomas")

    told = work_part(_person("tomas"), site.store, site.clock.now())
    assert "LAUNCH-1: Write the release notes (In Progress)" in told
    assert "you expect to finish it on" in told
    assert work_part(_person("noor"), site.store, site.clock.now()) == "", "Noor's only issue is done"
    silent = work_part(_person("iris"), site.store, site.clock.now())
    assert "VAULT-1: Rotate the vault keys (In Progress): you have not taken it up yet" in silent


async def test_a_fork_that_changes_a_person_replans_work_not_yet_taken_up_and_keeps_work_begun(site: Site) -> None:
    """A fork that makes Tomas slower to come to things moves his pick-up of FIELD-1, not yet begun, to his new
    moment; LAUNCH-1, which he began before the fork, keeps the finish it was planned with."""
    engine = _engine(site, _scenario(tomas=HOURLY))
    await _act_at(engine, site, "tomas")  # LAUNCH-1 begun at START+1h
    finish = [b for b in (await engine.look(site.store, site.clock)).booked if b.person == "tomas"]
    assert [b.at for b in finish] == [START + timedelta(hours=6)]

    slower = _engine(site, _scenario(tomas={"reply_within": {"min": "PT3H", "max": "PT3H"}}))
    moved = await slower.replan(site.store, site.clock, {"tomas"})
    assert moved == [], "the only move he owes is the finish of work he began: its plan stands"

    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": NOOR}), 204)
    noor = _engine(site, _scenario(tomas=HOURLY, noor=HOURLY))
    picked = [b for b in (await noor.look(site.store, site.clock)).booked if b.person == "noor"]
    assert [b.at for b in picked] == [site.clock.now() + timedelta(hours=1)]
    faster = _engine(site, _scenario(tomas=HOURLY, noor={"reply_within": {"min": "PT3H", "max": "PT3H"}}))
    [replanned] = await faster.replan(site.store, site.clock, {"noor"})
    assert replanned.at == site.clock.now() + timedelta(hours=3), "not yet taken up: picked up as she now answers"


# -- takes on work, forks, and what holds it -------------------------------------------------------------------------


async def _act_until_finished(engine: People, site: Site, person: str, key: str) -> list[tuple[str, str]]:
    moves: list[tuple[str, str]] = []
    await engine.look(site.store, site.clock)
    while _course(site, person, key).status is not CourseStatus.FINISHED:
        moves.append(await _act_at(engine, site, person))
    return moves


async def test_a_take_naming_a_move_that_leaves_the_work_unfinished_is_made_once_until_someone_else_moves_it(
    site: Site,
) -> None:
    """Catches a person pinned to comment on their work commenting again at the same instant, forever, so the run
    neither reaches its deadline nor its wake limit: Tomas, pinned to comment on LAUNCH-1 a day after it reaches him,
    takes it up, comments once at that moment, and nothing more of his is booked on it until someone else moves it."""
    takes = [{"provider": "jira", "take": "comment", "after": "P1D", "verbatim": "Still on it."}]
    engine = _engine(site, _scenario(tomas={**HOURLY, "takes": takes}))

    assert await _act_at(engine, site, "tomas") == ("Start work", "In Progress")
    name, state = await _act_at(engine, site, "tomas")
    assert (name, state, site.clock.now()) == ("comment", "In Progress", START + timedelta(days=1))
    looked = await engine.look(site.store, site.clock)
    assert [b for b in [*looked.booked, *looked.moved] if b.person == "tomas" and b.at is not None] == []
    assert _course(site, "tomas", "LAUNCH-1").stuck


@pytest.mark.parametrize("start", ["Start work", "In Progress"])
async def test_a_take_pinning_their_picking_it_up_leaves_its_finish_to_the_work_left(site: Site, start: str) -> None:
    """Catches a start take read again as the work's finish once it is begun: by the move's name, it is not offered
    from In Progress and the finish was dropped; by the state it reaches, it matched the comment there. Pinned to take
    LAUNCH-1 up two hours after it reaches him, Tomas does, and finishes it when the five hours left have passed."""
    takes = [{"provider": "jira", "take": start, "after": "PT2H"}]
    engine = _engine(site, _scenario(tomas={**HOURLY, "takes": takes}))

    assert await _act_at(engine, site, "tomas") == ("Start work", "In Progress")
    assert site.clock.now() == START + timedelta(hours=2)
    assert await _act_until_finished(engine, site, "tomas", "LAUNCH-1") == [
        ("Submit for review", "In Review"),
        ("Approve", "Done"),
    ]
    assert site.clock.now() == START + timedelta(hours=8), "five hours of work, then back to it an hour later"


async def test_two_takes_on_one_piece_of_work_pin_its_picking_up_and_its_finish_each_at_its_own_moment(
    site: Site,
) -> None:
    """Catches the second of two takes on one item losing its moment: pinned to start LAUNCH-1 two hours after it
    reaches him and to finish it two days after, Tomas starts it at two hours and finishes it at two days, not when
    what is left of it says."""
    takes = [
        {"provider": "jira", "nth": 1, "take": "started", "after": "PT2H"},
        {"provider": "jira", "nth": 1, "take": "done", "after": "P2D"},
    ]
    engine = _engine(site, _scenario(tomas={**HOURLY, "takes": takes}))

    assert await _act_at(engine, site, "tomas") == ("Start work", "In Progress")
    assert site.clock.now() == START + timedelta(hours=2)
    await _act_until_finished(engine, site, "tomas", "LAUNCH-1")
    assert site.clock.now() == START + timedelta(days=2)


async def test_a_silent_person_pinned_to_start_and_finish_work_with_no_estimate_needs_nobody_to_judge_it(
    site: Site,
) -> None:
    """Catches a run refused mid-way for want of a judgement nobody can give: Noor never speaks, FIELD-1 holds no
    estimate, and her takes pin both its start and its finish."""
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": NOOR}), 204)
    takes = [
        {"provider": "jira", "take": "started", "after": "PT2H"},
        {"provider": "jira", "take": "done", "after": "P1D"},
    ]
    engine = _engine(site, _scenario(noor={**SILENT, "takes": takes}))

    await _act_until_finished(engine, site, "noor", "FIELD-1")
    assert site.clock.now() == START + timedelta(days=1)


async def test_a_person_whose_finish_a_take_pins_is_never_offered_dropping_the_work(site: Site) -> None:
    """Catches a model dropping work a take pins to finish as done: Tomas, whose take pins LAUNCH-1's finish, takes it
    up with the move forward alone (no model weighs it against "Drop"), and finishes it at the pinned moment."""
    takes = [{"provider": "jira", "take": "done", "after": "P2D"}]
    engine = _engine(site, _scenario(tomas={**HOURLY, "takes": takes}))
    asked_before = len(people_requests())

    assert await _act_at(engine, site, "tomas") == ("Start work", "In Progress")
    assert not any("Drop" in json.dumps(r) for r in people_requests()[asked_before:])
    await _act_until_finished(engine, site, "tomas", "LAUNCH-1")
    assert site.clock.now() == START + timedelta(days=2)


async def test_a_take_naming_one_piece_of_work_by_its_place_pins_it_once_on_a_tracker_nobody_plays(
    site: Site,
) -> None:
    """Catches a take that names the second piece of work jumping to the first once the second has a course, on a
    tracker the scenario does not play (where only pinned work is carried): the same issue stays pinned, look after
    look, and the other is never carried."""
    ok(await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": TOMAS}), 204)
    body = _scenario(tomas={**HOURLY, "takes": [{"provider": "jira", "nth": 2, "take": "done", "after": "P1D"}]})
    engine = _engine(site, body.model_copy(update={"transitions_on": []}))

    await engine.look(site.store, site.clock)
    pinned_first = [c.item for _, c in engine.courses("tomas", site.store) if c.finish_take is not None]
    await engine.look(site.store, site.clock)
    carried = [(c.item, c.status) for _, c in engine.courses("tomas", site.store)]
    assert len(pinned_first) == 1
    assert carried == [(pinned_first[0], CourseStatus.WORKING)], "one issue pinned, and only it carried"


async def test_an_absence_that_starts_on_a_first_ask_starts_once_for_someone_who_is_only_handed_work(
    site: Site,
) -> None:
    """Catches an absence starting again at every move of someone nobody has messaged: Tomas, away for a day from his
    first ask, is handed LAUNCH-1 at the start and never messaged. His absence runs from then, so he takes it up an hour
    after it ends and finishes the five hours left from that, not a day later at each move."""
    away = {**HOURLY, "absences": [{"trigger": "on_first_ask", "lasts": "P1D"}]}
    engine = _engine(site, _scenario(tomas=away))

    assert await _act_at(engine, site, "tomas") == ("Start work", "In Progress")
    assert site.clock.now() == START + timedelta(days=1, hours=1)
    assert await _act_at(engine, site, "tomas") == ("Submit for review", "In Review")
    assert site.clock.now() == START + timedelta(days=1, hours=6)


async def test_work_held_back_says_why_it_waits_with_no_moment(site: Site) -> None:
    """Catches held work read as a move the engine failed to book (`checks.health`, owed and never booked, which makes
    the run simulation_incomplete): the pending move on LAUNCH-1, taken off the clock while FIELD-1 blocks it, says
    what holds it."""
    engine = _engine(site, _scenario(tomas=HOURLY))
    await _act_at(engine, site, "tomas")
    await engine.look(site.store, site.clock)
    ok(
        await site.http.post(
            f"{API}/issueLink",
            json={"type": {"name": "Blocks"}, "outwardIssue": {"key": "FIELD-1"}, "inwardIssue": {"key": "LAUNCH-1"}},
        ),
        201,
    )
    await engine.look(site.store, site.clock)
    [owed] = [h.pending for h in engine.held("tomas", site.store) if h.pending.status is PendingStatus.PENDING]
    assert owed.due_at is None and owed.held is not None and _ref(site, "FIELD-1").external_id in owed.held


# -- forks -----------------------------------------------------------------------------------------------------------


async def test_a_fork_that_takes_a_persons_takes_away_keeps_the_finish_of_work_they_began(site: Site) -> None:
    """Catches a fork reading begun work's finish by its place in the parent's takes: an IndexError where the fork
    holds fewer, another take where it holds others. Tomas, pinned to finish LAUNCH-1 two days after it reaches him,
    began it; a fork that gives him no takes keeps the finish he was planned with, and he makes it then."""
    takes = [{"provider": "jira", "take": "done", "after": "P2D"}]
    engine = _engine(site, _scenario(tomas={**HOURLY, "takes": takes}))
    await _act_at(engine, site, "tomas")
    await engine.look(site.store, site.clock)  # his finish booked before the fork, carrying its take

    forked = _engine(site, _scenario(tomas=HOURLY))
    assert await forked.replan(site.store, site.clock, {"tomas"}) == []
    await _act_until_finished(forked, site, "tomas", "LAUNCH-1")
    assert site.clock.now() == START + timedelta(days=2)


async def test_a_fork_that_silences_a_person_holds_their_work_not_yet_taken_up(site: Site) -> None:
    """Catches a person a fork made silent still taking their work up as their answers would have landed, and then
    holding it begun forever: once Silent, Tomas's pick-up of LAUNCH-1 is held with no moment, saying why."""
    engine = _engine(site, _scenario(tomas=HOURLY))
    await engine.look(site.store, site.clock)

    silenced = _engine(site, _scenario())
    [replanned] = await silenced.replan(site.store, site.clock, {"tomas"})
    assert replanned.at is None
    assert silenced.pending(replanned.pending, site.store).held is not None


async def test_a_fork_that_pins_when_a_person_moves_their_nth_issue_pins_their_taking_it_up(site: Site) -> None:
    """Catches `ReplyAt` with `provider` ignored for work: a fork pinning Tomas's first Jira item three hours after it
    reached him has him take LAUNCH-1 up then, not as his answers land (an hour)."""
    scenario = _scenario(tomas=HOURLY)
    pins = [ReplyAt(person="tomas", to_ask=1, provider="jira", after=timedelta(hours=3))]
    model = people_model()
    engine = People(scenario, {"jira": site.provider}, PeopleReplier(scenario, model), model, pins=pins)

    assert await _act_at(engine, site, "tomas") == ("Start work", "In Progress")
    assert site.clock.now() == START + timedelta(hours=3)


async def test_a_fork_keeps_the_moment_of_a_pick_up_whose_person_answers_as_before(site: Site) -> None:
    """Catches a fork redrawing every pick-up not yet made from the fork's moment: replanned half an hour in, Tomas,
    who answers as he did, still takes LAUNCH-1 up an hour after it reached him."""
    engine = _engine(site, _scenario(tomas=HOURLY))
    [pick] = [b for b in (await engine.look(site.store, site.clock)).booked if b.person == "tomas"]
    site.clock.jump(START + timedelta(minutes=30))

    [replanned] = await _engine(site, _scenario(tomas=HOURLY)).replan(site.store, site.clock, {"tomas"})
    assert replanned.at == pick.at == START + timedelta(hours=1)
