"""The simulated world's health (`checks.health`) over hand-built worlds: each fact about the world, with its evidence,
and which of them make the run `SIMULATION_INCOMPLETE`."""

from __future__ import annotations

import json
from datetime import timedelta

from minutehand.checks.health import health
from minutehand.checks.runner import evaluate as evaluated
from minutehand.checks.runner import exit_code, verdict
from minutehand.domain.checks import (
    DeclaredCollection,
    Effectiveness,
    Finding,
    FindingKind,
    HealthKind,
    RunView,
    Severity,
)
from minutehand.domain.clock import Drawn, DrawnFrom, Due, DueEntry, DueKind, DueSource
from minutehand.domain.conversation import PersonCall, Wrote
from minutehand.domain.people import Plan, Writing
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import AfterScript, Scenario, Scripted, ScriptedReply, Silent
from minutehand.domain.services import Service
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    Exchange,
    Operation,
    PendingSnapshot,
    PendingStatus,
    PushSnapshot,
    RecordedCall,
    ServiceItemSnapshot,
    WorldEvent,
)
from tests.checks.world import Log, at, person, scenario, view

HOST = "api.approvals.example"
ITEM = EntityRef(provider="approvals", kind=EntityKind.SERVICE_ITEM, external_id="req_1")
OWEN = person("owen", Silent())
NADIA = person("nadia")
MACHINE = {
    "initial": "pending",
    "states": ["pending", "approved"],
    "transitions": [{"name": "approve", "from": ["pending"], "to": "approved", "by": "person"}],
}


def _pending(log: Log, who: str, hours: float, **changes: object) -> WorldEvent:
    snapshot = PendingSnapshot(
        person=who, item=ITEM, nth=1, state="pending", turn=0, status=PendingStatus.PENDING, due_at=None
    ).model_copy(update=changes)
    ref = EntityRef(provider="minutehand", kind=EntityKind.PENDING, external_id=f"{who}|req_1|{len(log.events) + 1}")
    return log._add(hours, Actor.SCENARIO, Operation.CREATE, ref, snapshot, 1)  # pyright: ignore[reportPrivateUsage]


def _push(log: Log, attempt: int, hours: float, *, status: int | None = None) -> WorldEvent:
    snapshot = PushSnapshot(
        service="slack",
        item="Ev0000000042",
        url="http://127.0.0.1:8745/slack/events",
        body="{}",
        status=status,
        failure=None if status is not None else "ReadTimeout('')",
        attempt=attempt,
        retry_reason=None if attempt == 0 else "http_timeout",
    )
    ref = EntityRef(provider="slack", kind=EntityKind.PUSH, external_id=f"push:{len(log.events) + 1}")
    return log._add(hours, Actor.SCENARIO, Operation.CREATE, ref, snapshot, 1)  # pyright: ignore[reportPrivateUsage]


def _with_service(world: Scenario, responders: list[str]) -> Scenario:
    service = Service.model_validate(
        {
            "host": "api.approvals.example",
            "name": "approvals",
            "responders": responders,
            "within": {"min": "PT1H", "max": "PT2H"},
            "machine": MACHINE,
        }
    )
    return world.model_copy(update={"services": [service]})


def _kinds(found: RunView) -> list[tuple[HealthKind, bool]]:
    return [(f.kind, f.incomplete) for f in health(found) if f.kind is not HealthKind.NEVER_EXERCISED]


def test_a_move_owed_by_someone_who_speaks_for_themselves_and_never_booked_is_incomplete() -> None:
    log = Log()
    log.message([OWEN], 0)
    waited = _pending(log, "nadia", 1)
    [found] = [f for f in health(view(scenario(OWEN, NADIA), log)) if f.kind is HealthKind.OWED_UNBOOKED]
    assert (found.incomplete, found.person, found.entity, found.since, found.evidence) == (
        True,
        "nadia",
        ITEM,
        at(1),
        [waited.seq],
    )


def test_a_moment_booked_that_the_table_of_what_is_due_never_held_is_incomplete() -> None:
    log = Log()
    waited = _pending(log, "nadia", 1, due_at=at(3))
    unbooked = view(scenario(OWEN, NADIA), log).model_copy(update={"dues": []})
    assert _kinds(unbooked) == [(HealthKind.OWED_UNBOOKED, True)]
    entry = DueEntry(
        due=Due(at=at(3), kind=DueKind.TRANSITION, ref=f"transition:{waited.entity.external_id}"),
        source=DueSource.TRANSITION,
        entered_at=at(1),
        entered_wake=1,
    )
    assert _kinds(unbooked.model_copy(update={"dues": [entry]})) == []


def test_a_service_responder_scripted_to_silence_never_acts_and_is_incomplete() -> None:
    silenced = person("nadia", Scripted(then=AfterScript.SILENT))
    log = Log()
    _pending(log, "nadia", 1)
    world = _with_service(scenario(OWEN, silenced), ["nadia"])
    assert _kinds(view(world, log)) == [(HealthKind.RESPONDER_NEVER_ACTS, True)]


def test_an_item_pending_on_someone_declared_silent_is_coverage_not_incomplete() -> None:
    log = Log()
    _pending(log, "nadia", 1)
    world = _with_service(scenario(OWEN, person("nadia", Silent())), ["nadia"])
    assert _kinds(view(world, log)) == [(HealthKind.WAITS_BY_DECLARATION, False)]


def test_an_item_only_a_person_moves_held_pending_on_nobody_is_incomplete() -> None:
    log = Log()
    filed = log._add(  # pyright: ignore[reportPrivateUsage]
        1,
        Actor.AGENT,
        Operation.CREATE,
        ITEM,
        ServiceItemSnapshot(service="approvals", item="req_1", state="pending"),
        1,
    )
    world = _with_service(scenario(OWEN, NADIA), ["nadia"])
    [found] = [f for f in health(view(world, log)) if f.kind is HealthKind.WAITS_ON_NOBODY]
    assert (found.incomplete, found.evidence, found.since) == (True, [filed.seq], at(1))
    _pending(log, "nadia", 1, due_at=at(2))
    assert HealthKind.WAITS_ON_NOBODY not in [f.kind for f in health(view(world, log))]


def test_a_people_model_call_that_failed_and_was_never_answered_is_incomplete() -> None:
    def call(answer: str | None, failure: str | None) -> PersonCall:
        return PersonCall(
            key="k1",
            person="nadia",
            wrote=Wrote.REPLY,
            model="m",
            prompt_version="person-reply/3",
            answer=answer,
            failure=failure,
            sim_time=at(2),
            wake=1,
        )

    failed = view(scenario(OWEN, NADIA), Log()).model_copy(update={"person_calls": [call(None, "502 from the API")]})
    [found] = [f for f in health(failed) if f.kind is HealthKind.MODEL_FAILED]
    assert (found.incomplete, found.person, found.since) == (True, "nadia", at(2))
    assert "502 from the API" in found.words
    recovered = failed.model_copy(update={"person_calls": [*failed.person_calls, call('{"text": "ok"}', None)]})
    assert HealthKind.MODEL_FAILED not in [f.kind for f in health(recovered)]


def test_a_push_the_agent_never_took_after_every_retry_is_incomplete_with_each_send_as_evidence() -> None:
    log = Log()
    sends = [_push(log, n, 1) for n in range(4)]
    [found] = [f for f in health(view(scenario(OWEN, NADIA), log)) if f.kind is HealthKind.PUSH_FAILED]
    assert (found.incomplete, found.evidence) == (True, [s.seq for s in sends])
    assert "4 time(s)" in found.words and "ReadTimeout" in found.words
    taken = Log()
    _push(taken, 0, 1)
    _push(taken, 1, 1, status=200)
    assert HealthKind.PUSH_FAILED not in [f.kind for f in health(view(scenario(OWEN, NADIA), taken))]


def test_what_was_declared_and_never_touched_is_coverage() -> None:
    log = Log()
    log.message([OWEN], 0)
    world = _with_service(scenario(OWEN, NADIA), ["nadia"])
    declared = view(world, log).model_copy(
        update={"collections": [DeclaredCollection(host="api.orders.example", collection="orders")]}
    )
    found = [(f.words, f.incomplete) for f in health(declared) if f.kind is HealthKind.NEVER_EXERCISED]
    assert found == [
        ("service approvals (api.approvals.example) was declared and nothing in the run touched it", False),
        ("collection orders of api.orders.example was declared and held nothing in the run", False),
        ("nadia was declared and was never written to, asked or heard from in the run", False),
    ]


def test_a_scripted_step_whose_ask_never_came_is_coverage() -> None:
    steps = [ScriptedReply(to_ask=1, facts=["cost centre CC-4410"]), ScriptedReply(to_ask=2, verbatim="Done.")]
    sam = person("sam", Scripted(replies=steps))
    log = Log()
    asked = log.message([sam], 0)
    planned = Plan(
        person="sam",
        asked=asked.entity,
        nth=1,
        writing=Writing.SCRIPT,
        step=steps[0],
        drawn=Drawn(source=DrawnFrom.DELAY, seed=1, asked_at=at(0), offset=timedelta(hours=1), lands_at=at(1)),
    )
    _pending(log, "sam", 0, asked=asked.seq, plan=planned.model_dump_json(), due_at=at(1))
    found = [f for f in health(view(scenario(OWEN, sam), log)) if f.kind is HealthKind.STEP_NEVER_FIRED]
    assert [(f.person, f.incomplete) for f in found] == [("sam", False)]
    assert "ask 2" in found[0].words


def _card(failed: int = 0) -> Effectiveness:
    return Effectiveness(
        expectations_met=0,
        expectations_total=0,
        waits_opened=0,
        waits_open_at_end=0,
        follow_ups_made=0,
        wakes=1,
        idle_wakes=0,
        failed_checks=failed,
    )


def test_an_incomplete_world_keeps_the_agents_failure_beside_it_and_exits_6() -> None:
    log = Log()
    _pending(log, "nadia", 1)
    world = view(scenario(OWEN, NADIA), log)
    said = verdict(world, _card(failed=1), StopReason.DEADLINE_PASSED, simulation=health(world))
    assert (said.kind, said.on_what_happened, said.exit_code) == (
        VerdictKind.SIMULATION_INCOMPLETE,
        VerdictKind.FAILED,
        6,
    )
    assert said.words.startswith("Simulation incomplete: 1 thing in the simulated world did not play")
    assert said.words.endswith(
        "on what did happen: Failed: 1 check failed; the run stopped at the scenario's deadline."
    )
    whole = verdict(world, _card(failed=1), StopReason.DEADLINE_PASSED)
    assert (whole.kind, whole.on_what_happened) == (VerdictKind.FAILED, None)


def test_an_environment_that_failed_outranks_an_incomplete_world() -> None:
    log = Log()
    _pending(log, "nadia", 1)
    world = view(scenario(OWEN, NADIA), log)
    said = verdict(world, _card(), StopReason.ENVIRONMENT_FAILED, simulation=health(world))
    assert said.kind is VerdictKind.ENVIRONMENT_FAILED


def test_the_health_of_a_run_is_kept_apart_from_its_findings_and_noted() -> None:
    log = Log()
    _pending(log, "nadia", 1)
    result = evaluated(view(scenario(OWEN, NADIA), log, wakes=None), stop=StopReason.DEADLINE_PASSED)
    assert result.verdict.kind is VerdictKind.SIMULATION_INCOMPLETE
    assert [f.kind for f in result.simulation if f.incomplete] == [HealthKind.OWED_UNBOOKED]
    assert all(f.check != "simulation" for f in result.findings)
    assert [n for n in result.notes if n.startswith("simulation:") and "req_1" in n]


def test_over_samples_an_incomplete_world_exits_6_before_a_failure() -> None:
    log = Log()
    _pending(log, "nadia", 1)
    incomplete = evaluated(view(scenario(OWEN, NADIA), log), stop=StopReason.DEADLINE_PASSED)
    failing = evaluated(view(scenario(OWEN, NADIA), Log()), stop=StopReason.DEADLINE_PASSED)
    failing = failing.model_copy(
        update={
            "findings": [Finding(check="x", severity=Severity.ERROR, kind=FindingKind.FAIL, message="m")],
            "verdict": failing.verdict.model_copy(update={"kind": VerdictKind.FAILED}),
        }
    )
    assert exit_code([failing, incomplete]) == 6


def test_a_declared_service_answering_the_agent_with_minutehands_own_failure_is_incomplete() -> None:
    """A 502 holding the error Minutehand could not answer past is the world failing the agent; the service's own
    refusal of a move its machine does not allow (a 4xx) is the world answering it."""

    def call(status: int, body: dict[str, str], hours: float) -> RecordedCall:
        exchange = Exchange(
            method="GET", host=HOST, path="/v1/requests/req_1", status=status, response_body=json.dumps(body)
        )
        return RecordedCall(exchange=exchange, provider=None, first_seq=1, last_seq=0, wake=1, sim_time=at(hours))

    world = _with_service(scenario(OWEN, NADIA), ["nadia"])
    failure = {"error": "the answer could not be rendered: $.quote: is not a field this shape has", "host": HOST}
    failed = view(world, Log()).model_copy(update={"calls": [call(502, failure, 3), call(502, failure, 5)]})

    [found] = [f for f in health(failed) if f.kind is HealthKind.SERVICE_FAILED]
    assert (found.incomplete, found.since) == (True, at(3))
    assert f"{HOST} could not answer 2 of the agent's calls" in found.words and "$.quote" in found.words
    refused = view(world, Log()).model_copy(update={"calls": [call(409, {"error": "not from pending"}, 3)]})
    assert HealthKind.SERVICE_FAILED not in [f.kind for f in health(refused)]
