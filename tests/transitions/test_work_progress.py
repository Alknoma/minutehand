"""People carry the work handed to them (docs/design-transitions.md, "Work a person carries"): an issue assigned to a
person and not finished is theirs to carry over their own time, one piece at a time. They pick it up as an answer of
theirs would land and move it to a started state; they finish it once the work it takes has passed in their available
time (what Jira says is left of it, else their pace, else as long as they judge it takes them); a workflow that
needs a review first gets one; an unfinished
issue that blocks it holds it back, and it resumes with what was left; a take pins the move it names."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from minutehand.adapters.providers.jira.state import issue_ref
from minutehand.application.people import People
from minutehand.application.replier import work_part
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
from tests.support.people import people_engine, people_model

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
