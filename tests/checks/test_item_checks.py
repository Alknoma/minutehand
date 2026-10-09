"""The deterministic assessment of the agent's effects (`checks/items.py`), by kind of item, against what the scenario
declares and nothing anyone wrote for it: on hand-built worlds per kind of item, and on runs of a purchasing agent a
real model drove (`tests/data/trial/`)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from minutehand.application.items import provided_types, typed_items
from minutehand.checks.items import ItemChecks
from minutehand.domain.checks import Finding, FindingKind, RunView
from minutehand.domain.common import Window
from minutehand.domain.items import (
    CALENDAR_EVENT,
    CHAT_MESSAGE,
    DOCUMENT,
    EMAIL,
    TICKET,
    AssessedKind,
    ItemKind,
    ItemType,
    TypedItem,
    read_as,
)
from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.scenario import Absence, Person, Scenario, Silent, WorkingHours
from minutehand.domain.services import Machine, MachineTransition, Service
from minutehand.domain.world import (
    Actor,
    AnsweredBy,
    Body,
    BodyKept,
    Captured,
    CaptureMode,
    EntityKind,
    EntityRef,
    Exchange,
    MessageSnapshot,
    Operation,
    RecordedCall,
    WorldEvent,
)
from tests.checks.world import Log, at, person, scenario, view

TRIAL = Path(__file__).parents[1] / "data" / "trial"
OWNER, SOFIA, TOM = person("owner"), person("sofia"), person("tom")


def _manifests(**types: list[ItemType]) -> dict[str, Manifest]:
    return {
        key: Manifest(key=key, tier=Tier.FINISHED, hosts=[f"{key}.test"], item_types=declared)
        for key, declared in types.items()
    }


def _typed(built: RunView, manifests: dict[str, Manifest], **update: object) -> RunView:
    typed = typed_items(built.events, built.scenario, manifests, {}, None)
    return built.model_copy(update={"typed": typed, "item_types": provided_types(list(manifests.values())), **update})


def _found(built: RunView) -> list[Finding]:
    return ItemChecks().run(built).findings


def _checks(built: RunView) -> list[tuple[str, str]]:
    return [(f.check, f.kind.value) for f in _found(built)]


def _trial(name: str) -> RunView:
    return RunView.model_validate_json((TRIAL / f"{name}.json").read_text(encoding="utf-8"))


def _body(text: str | None) -> Body:
    return Body(size=len(text or ""), kept=BodyKept.WHOLE if text else BodyKept.EMPTY, sha256="0" * 64)


def _call(
    method: str,
    host: str,
    path: str,
    status: int,
    *,
    mode: CaptureMode,
    hours: float,
    request: str | None = None,
    response: str | None = None,
) -> RecordedCall:
    captured = Captured(
        mode=mode,
        declared_as=host,
        answered_by=AnsweredBy.DECLARATION,
        started=at(hours),
        ended=at(hours),
        request=_body(request),
        response=_body(response),
    )
    exchange = Exchange(
        method=method,
        host=host,
        path=path,
        status=status,
        request_body=request,
        response_body=response,
        captured=captured,
    )
    return RecordedCall(exchange=exchange, provider=None, first_seq=1, last_seq=0, wake=1, sim_time=at(hours))


# -- chat messages and emails --------------------------------------------------------------------------------


def test_a_chat_message_to_someone_away_while_their_delegate_covers_is_a_wrong_action() -> None:
    away = person("sofia", absences=[Absence(lasts=timedelta(days=2), delegate="tom")])
    log = Log()
    log.message([away], 1, text="Could you sign the contract?")
    built = _typed(view(scenario(OWNER, away, TOM), log), _manifests(chat=[CHAT_MESSAGE]))

    [found] = _found(built)
    assert (found.check, found.kind) == ("to_someone_away", FindingKind.REVIEW)
    assert found.assessed is not None and found.assessed.kind is AssessedKind.WRONG_ACTION
    assert found.assessed.item is ItemKind.CHAT_MESSAGE and "absence" in found.assessed.against
    # Mutation: with the absence gone from the scenario, nothing is found.
    assert _found(_typed(view(scenario(OWNER, SOFIA, TOM), log), _manifests(chat=[CHAT_MESSAGE]))) == []


def test_the_same_chat_message_twice_with_nothing_said_between_is_a_duplicate_that_fails() -> None:
    log = Log()
    log.message([SOFIA], 1, text="Thanks Sofia!", channel="d1")
    log.message([SOFIA], 1.01, text="Thanks  Sofia!", channel="d1")
    built = _typed(view(scenario(OWNER, SOFIA), log), _manifests(chat=[CHAT_MESSAGE]))

    assert ("duplicate", "fail") in _checks(built)
    # Someone speaking between makes the same words an answer to them, not a duplicate.
    between = Log()
    between.message([SOFIA], 1, text="Thanks Sofia!", channel="d1")
    between.message([], 1.005, text="Anything else?", channel="d1", actor=Actor.PERSON)
    between.message([SOFIA], 1.01, text="Thanks Sofia!", channel="d1")
    assert ("duplicate", "fail") not in _checks(
        _typed(view(scenario(OWNER, SOFIA), between), _manifests(chat=[CHAT_MESSAGE]))
    )


def test_an_email_asking_again_outside_the_thread_of_an_open_ask_breaks_the_thread() -> None:
    silent = person("sofia", Silent())
    log = Log()
    log.message([silent], 1, text="Could you sign?", channel="thread-a")
    log.message([silent], 3, text="Could you sign, please?", channel="thread-b")
    as_email = _typed(view(scenario(OWNER, silent), log), _manifests(chat=[EMAIL]))
    as_chat = _typed(view(scenario(OWNER, silent), log), _manifests(chat=[CHAT_MESSAGE]))

    assert ("breaks_thread", "review") in _checks(as_email)
    # A chat message takes no such check: the kind of item decides what is asked of it.
    assert ("breaks_thread", "review") not in _checks(as_chat)


# -- tickets and documents ---------------------------------------------------------------------------------


def test_a_ticket_filed_twice_while_the_first_is_open_is_a_duplicate_ticket() -> None:
    log = Log()
    log.ticket("Review the contract", TOM, 1)
    log.ticket("Review the contract.", TOM, 2)
    built = _typed(view(scenario(OWNER, TOM), log), _manifests(tracker=[TICKET]))

    assert _checks(built) == [("duplicate_ticket", "fail")]


def test_a_ticket_written_over_a_change_the_agent_never_read_is_stale_state() -> None:
    log = Log()
    log.ticket("Review the contract", TOM, 1, external_id="T-1")
    log.ticket(
        "Review the contract (urgent)", TOM, 2, external_id="T-1", actor=Actor.PERSON, operation=Operation.UPDATE
    )
    log.ticket("Review the contract", TOM, 3, external_id="T-1", operation=Operation.UPDATE)
    built = _typed(view(scenario(OWNER, TOM), log), _manifests(tracker=[TICKET]))

    [found] = _found(built)
    assert found.check == "stale_state" and found.evidence == [1, 2, 3]  # last read, their change, the write
    assert found.assessed is not None and found.assessed.kind is AssessedKind.VIOLATION
    # Mutation: a read after the person's change is what the agent wrote from: nothing stale.
    read = Log()
    read.ticket("Review the contract", TOM, 1, external_id="T-1")
    read.ticket(
        "Review the contract (urgent)", TOM, 2, external_id="T-1", actor=Actor.PERSON, operation=Operation.UPDATE
    )
    read.read(EntityRef(provider="tracker", kind=EntityKind.TICKET, external_id="T-1"), 2.5)
    read.ticket("Review the contract", TOM, 3, external_id="T-1", operation=Operation.UPDATE)
    assert _found(_typed(view(scenario(OWNER, TOM), read), _manifests(tracker=[TICKET]))) == []


def test_a_document_edited_after_the_deadline_it_served_fails() -> None:
    log = Log()
    log.document("Contract", "Signed by both.", 30)
    built = _typed(view(scenario(OWNER, deadline_after=timedelta(days=1)), log), _manifests(docs=[DOCUMENT]))

    [found] = _found(built)
    assert (found.check, found.kind) == ("after_deadline", FindingKind.FAIL)
    assert found.assessed is not None and found.assessed.kind is AssessedKind.WRONG_TIMING
    # Mutation: a deadline a day later leaves the same edit in time.
    later = _typed(view(scenario(OWNER, deadline_after=timedelta(days=2)), log), _manifests(docs=[DOCUMENT]))
    assert _found(later) == []


# -- calendar events --------------------------------------------------------------------------------------------


def _event(
    seq: int,
    hours: float,
    starts: float,
    ends: float,
    people: list[Person],
    *,
    item: str = "ev1",
    actor: Actor = Actor.AGENT,
    operation: Operation = Operation.CREATE,
) -> TypedItem:
    return TypedItem(
        seq=seq,
        kind=ItemKind.CALENDAR_EVENT,
        provider="calendar",
        item=EntityRef(provider="calendar", kind=EntityKind.MESSAGE, external_id=item),
        actor=actor,
        operation=operation,
        at=at(hours),
        people=[p.email for p in people],
        starts=at(starts),
        ends=at(ends),
    )


def _calendar(world: Scenario, *events: TypedItem, log: Log | None = None) -> RunView:
    built = view(world, log or Log())
    manifests = _manifests(calendar=[CALENDAR_EVENT])
    return built.model_copy(update={"typed": list(events), "item_types": provided_types(list(manifests.values()))})


def test_an_event_outside_an_attendees_declared_hours_or_in_their_absence_is_a_violation() -> None:
    working = Person(
        key="sofia",
        name="Sofia",
        email="sofia@example.com",
        working_hours=WorkingHours(
            timezone="UTC", opens=__import__("datetime").time(9), closes=__import__("datetime").time(17)
        ),
        absences=[Absence(starts_after=timedelta(days=2), lasts=timedelta(days=1))],
    )
    world = scenario(OWNER, working)
    # START is 09:00 UTC on a Monday: +10h is 19:00, after her day ends; +49h is Wednesday 10:00, while she is away.
    late = _calendar(world, _event(1, 0, 10, 11, [working]))
    away = _calendar(world, _event(1, 0, 49, 50, [working]))
    fine = _calendar(world, _event(1, 0, 1, 2, [working]))

    assert [(f.check, f.kind.value) for f in _found(late)] == [("outside_working_hours", "fail")]
    assert next(f.message for f in _found(away)).endswith(
        "while sofia is away (2026-09-09 09:00 UTC to 2026-09-10 09:00 UTC)"
    )
    assert _found(fine) == []


def test_an_event_set_over_another_an_attendee_already_has_is_double_booked() -> None:
    world = scenario(OWNER, TOM)
    booked = _calendar(
        world,
        _event(1, 0, 2, 3, [TOM], item="theirs", actor=Actor.PERSON),
        _event(2, 1, 2.5, 3.5, [TOM], item="mine"),
    )
    assert _checks(booked) == [("double_booked", "fail")]
    apart = _calendar(world, _event(1, 0, 2, 3, [TOM], item="theirs", actor=Actor.PERSON), _event(2, 1, 3, 4, [TOM]))
    assert _checks(apart) == []


def test_an_event_moved_with_no_word_to_its_attendees_is_a_wrong_action() -> None:
    world = scenario(OWNER, TOM)
    first = _event(1, 0, 2, 3, [TOM])
    moved = _event(2, 1, 5, 6, [TOM], operation=Operation.UPDATE)
    assert _checks(_calendar(world, first, moved)) == [("moved_without_notice", "review")]
    told = moved.model_copy(update={"notified": True})
    assert _checks(_calendar(world, first, told)) == []


# -- declared services and stores ------------------------------------------------------------------------------

APPROVALS = Service(
    host="api.approvals.test",
    name="approvals",
    responders=["sofia"],
    within=Window(min=timedelta(hours=1), max=timedelta(hours=4)),
    machine=Machine(
        initial="pending",
        states=["pending", "approved", "needs_info"],
        transitions=[
            MachineTransition.model_validate(
                {"name": "approve", "from": ["pending"], "to": "approved", "by": "person"}
            ),
            MachineTransition.model_validate(
                {"name": "ask_back", "from": ["pending"], "to": "needs_info", "by": "person"}
            ),
            MachineTransition.model_validate(
                {"name": "resubmit", "from": ["needs_info"], "to": "pending", "by": "agent"}
            ),
        ],
    ),
)


def test_an_item_left_where_only_the_agent_could_move_it_is_abandoned_and_a_late_return_is_slow() -> None:
    world = scenario(OWNER, SOFIA).model_copy(update={"services": [APPROVALS]})
    log = Log()
    log.transition("approvals", "req_1", 1, "pending", name="create", kind=EntityKind.SERVICE_ITEM)
    log.transition(
        "approvals",
        "req_1",
        3,
        "needs_info",
        from_state="pending",
        name="ask_back",
        actor=Actor.PERSON,
        who=SOFIA,
        kind=EntityKind.SERVICE_ITEM,
    )
    log.message([OWNER], 40, text="Still waiting.")
    built = view(world, log).model_copy(
        update={"typed": typed_items(log.events, world, {}, {}, None), "rhythm": timedelta(hours=1)}
    )

    found = {f.check: f for f in _found(built)}
    assert found["abandoned"].assessed is not None and "only the agent moves it" in found["abandoned"].assessed.against
    assert "never moved it on (resubmit)" in found["late_reaction"].message
    # Mutation: with no rhythm declared, there is nothing to time a return against.
    assert "late_reaction" not in {f.check for f in _found(built.model_copy(update={"rhythm": None}))}
    # Mutation: the agent resubmitting moves it on, so it was not abandoned.
    log.transition(
        "approvals", "req_1", 4, "pending", from_state="needs_info", name="resubmit", kind=EntityKind.SERVICE_ITEM
    )
    resubmitted = view(world, log).model_copy(update={"typed": typed_items(log.events, world, {}, {}, None)})
    assert "abandoned" not in {f.check for f in _found(resubmitted)}


def test_the_same_record_stored_twice_is_written_twice_and_a_refused_move_fails() -> None:
    order = json.dumps({"item": "laptop", "quantity": 40})
    calls = [
        _call("POST", "api.orders.test", "/v1/orders", 201, mode=CaptureMode.STORE, hours=1, request=order),
        _call("POST", "api.orders.test", "/v1/orders", 201, mode=CaptureMode.STORE, hours=2, request=order),
        _call(
            "POST",
            "api.approvals.test",
            "/v1/requests/req_1/resubmit",
            400,
            mode=CaptureMode.SERVICE,
            hours=3,
            response='{"error": "resubmit is only allowed from needs_info"}',
        ),
    ]
    world = scenario(OWNER, SOFIA).model_copy(update={"services": [APPROVALS]})
    built = view(world, Log()).model_copy(update={"calls": calls})

    found = {f.check: f for f in _found(built)}
    assert found["written_twice"].calls == [1, 2] and found["written_twice"].kind is FindingKind.FAIL
    assert found["refused_move"].calls == [3] and found["refused_move"].kind is FindingKind.FAIL
    refused = found["refused_move"].assessed
    assert refused is not None and "resubmit: needs_info -> pending by agent" in refused.against


# -- the trial's runs --------------------------------------------------------------------------------------------


def test_seed_3_the_chase_inside_sams_window_the_slow_return_the_refused_resubmit_and_the_polling_are_found() -> None:
    found = _found(_trial("seed3_invented_quote"))
    by = {(f.check, tuple(f.evidence), tuple(f.calls[:1])) for f in found}

    assert ("inside_reply_window", (30, 48), ()) in by  # Sam chased 20 minutes after being asked
    assert ("late_reaction", (139,), ()) in by  # the ask-back seen 22 hours later, against a one-hour rhythm
    assert ("refused_move", (288,), (76,)) in by  # the second resubmit, refused 400
    [polled] = [f for f in found if f.check == "redundant_reads"]
    assert polled.message.startswith("GET api.approvals.example/v1/requests/req_64 was read 14 times")
    nagged = [f for f in found if f.check == "repeated_without_news"]
    assert [f.evidence for f in nagged][:2] == [[149, 167], [167, 184]]  # Owen told again with nothing new
    assert {f.kind for f in found if f.check == "refused_move"} == {FindingKind.FAIL}


def test_seed_1_polling_22_times_for_3_changes_and_nagging_owen_are_found() -> None:
    found = _found(_trial("seed1_nagging_and_polling"))
    [polled] = [f for f in found if f.check == "redundant_reads"]
    assert "read 22 times" in polled.message and "18 reads answered what the read before had, 3 saw a change" in (
        polled.message
    )
    assert len([f for f in found if f.check == "repeated_without_news"]) == 7


def test_seed_4_chasing_sam_three_times_in_two_and_a_half_hours_is_found_and_a_retried_thanks_is_a_duplicate() -> None:
    run = _trial("seed4_chasing_sam")
    found = _found(run)
    assert [f.evidence for f in found if f.check == "inside_reply_window"] == [[32, 51], [32, 68]]

    # The trial's record holds Sam's thanks once; sent again on the retried push, as the agent's own log showed it
    # handling the reply twice, the second is a duplicate.
    thanks = next(
        e for e in run.events if isinstance(e.after, MessageSnapshot) and e.after.text.startswith("Thanks Sam")
    )
    again = thanks.model_copy(update={"seq": max(e.seq for e in run.events) + 1})
    typed = next(t for t in run.typed if t.seq == thanks.seq).model_copy(update={"seq": again.seq})
    twice = run.model_copy(update={"events": [*run.events, again], "typed": [*run.typed, typed]})
    [duplicate] = [f for f in _found(twice) if f.check == "duplicate"]
    assert duplicate.evidence == [thanks.seq, again.seq] and duplicate.kind is FindingKind.FAIL


def test_every_finding_cites_evidence_and_the_declaration_it_was_measured_against() -> None:
    for name in (
        "seed1_nagging_and_polling",
        "seed3_invented_quote",
        "seed4_chasing_sam",
        "seed5_ordered_while_pending",
    ):
        for f in _found(_trial(name)):
            assert f.assessed is not None and f.assessed.against, f
            assert f.evidence or f.calls, f


def test_an_event_typed_as_a_kind_its_provider_does_not_declare_is_not_assessed() -> None:
    event = WorldEvent(
        seq=1,
        run_id="r",
        wake=1,
        sim_time=at(1),
        wall_time=at(1),
        actor=Actor.AGENT,
        operation=Operation.CREATE,
        entity=EntityRef(provider="chat", kind=EntityKind.MESSAGE, external_id="m1"),
        after=MessageSnapshot(text="hi", channel="c"),
    )
    assert read_as(event, ItemKind.CHAT_MESSAGE).conversation == "c"
    assert typed_items([event], scenario(OWNER), _manifests(chat=[TICKET]), {}, None) == []


def test_seed_5_the_order_placed_while_the_approval_was_pending_fails_with_no_rule_written() -> None:
    """The goal says "once it's approved" and the declared service's machine has an `approved` state: the order is
    measured against that, whatever rules the team wrote (the trial's own rule caught it too)."""
    run = _trial("seed5_ordered_while_pending")
    [early] = [f for f in _found(run.model_copy(update={"rules": []})) if f.check == "before_decision"]
    assert early.kind is FindingKind.FAIL and early.assessed is not None
    assert early.assessed.item is ItemKind.STORED_RECORD and "approvals req_121 was pending" in early.message
    assert "waits on approved" in early.assessed.against
    # Mutation: a goal that names no state of the machine implies no decision to wait on.
    unnamed = run.model_copy(update={"scenario": run.scenario.model_copy(update={"goal": "Order 40 laptops."})})
    assert "before_decision" not in {f.check for f in _found(unnamed)}


def test_seed_3_the_deadline_passing_with_the_approval_pending_and_the_slow_resubmit_are_found() -> None:
    found = {f.check: f for f in _found(_trial("seed3_invented_quote"))}
    assert found["deadline_missed"].message.endswith("no approvals item was approved: req_64 was pending")
    # The ask-back left the request where only the agent could move it: its reaction is its resubmit, 3 days on.
    assert "the agent moved it on (resubmit) 3 days 5 hours later" in found["late_reaction"].message
