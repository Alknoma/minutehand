"""`DocumentCreated` and `DocumentShared`: a document the agent made, in a place, owned by someone, holding a tell
as it last read; and access the agent gave a named person. Each met and unmet."""

from __future__ import annotations

from minutehand.checks.expectations import Expectations
from minutehand.domain.checks import FindingKind
from minutehand.domain.scenario import AccessRole, DocumentCreated, DocumentShared, Expectation
from minutehand.domain.world import Actor, DocumentSnapshot, EntityKind, EntityRef, GrantSnapshot, Operation
from tests.checks.world import Log, person, scenario, view

OWNER, SOFIA = person("owner"), person("sofia")
NOTES = EntityRef(provider="docs", kind=EntityKind.DOCUMENT, external_id="d1")


def _document(log: Log, hours: float, operation: Operation, text: str, *, actor: Actor = Actor.AGENT) -> None:
    snapshot = DocumentSnapshot(title="Outage write-up", text=text, owner="owner@example.com", space="Incident reviews")
    log._add(hours, actor, operation, NOTES, snapshot, wake=1)


def _verdicts(expect: list[Expectation], log: Log) -> list[FindingKind]:
    return [f.kind for f in Expectations().run(view(scenario(OWNER, SOFIA, expect=expect), log)).findings]


def test_a_document_the_agent_created_and_later_filled_meets_created_holding_its_tell() -> None:
    log = Log()
    _document(log, 1, Operation.CREATE, "")
    _document(log, 2, Operation.UPDATE, "412 orders retried at 14:02.")
    wanted = DocumentCreated(titled=["outage"], space="Incident reviews", owner="owner", holds=["412 orders"])
    assert _verdicts([wanted], log) == [FindingKind.INFORMATIONAL]


def test_a_document_created_elsewhere_by_someone_else_or_without_the_tell_does_not_meet_it() -> None:
    log = Log()
    _document(log, 1, Operation.CREATE, "412 orders")
    assert _verdicts([DocumentCreated(space="Elsewhere")], log) == [FindingKind.FAIL]
    assert _verdicts([DocumentCreated(owner="sofia")], log) == [FindingKind.FAIL]
    assert _verdicts([DocumentCreated(holds=["refund"])], log) == [FindingKind.FAIL]
    by_person = Log()
    _document(by_person, 1, Operation.CREATE, "412 orders", actor=Actor.PERSON)
    assert _verdicts([DocumentCreated()], by_person) == [FindingKind.FAIL]


def test_a_tell_written_after_the_deadline_does_not_count() -> None:
    from datetime import timedelta

    log = Log()
    _document(log, 1, Operation.CREATE, "")
    _document(log, 30, Operation.UPDATE, "412 orders")
    assert _verdicts([DocumentCreated(holds=["412"], by=timedelta(hours=24))], log) == [FindingKind.FAIL]
    assert _verdicts([DocumentCreated(holds=["412"], by=timedelta(hours=31))], log) == [FindingKind.INFORMATIONAL]


def _grant(log: Log, to: str, role: AccessRole) -> None:
    ref = EntityRef(provider="docs", kind=EntityKind.RECORD, external_id=f"g{len(log.events) + 1}")
    log._add(2, Actor.AGENT, Operation.CREATE, ref, GrantSnapshot(document="Outage write-up", to=to, role=role), 1)


def test_a_document_shared_with_the_person_at_least_as_asked_meets_shared() -> None:
    log = Log()
    _grant(log, "SOFIA@example.com", AccessRole.WRITER)
    assert _verdicts([DocumentShared(person="sofia", titled=["outage"], role=AccessRole.COMMENTER)], log) == [
        FindingKind.INFORMATIONAL
    ]


def test_a_share_with_someone_else_or_with_too_little_access_does_not_meet_shared() -> None:
    log = Log()
    _grant(log, "owner@example.com", AccessRole.ORGANIZER)
    _grant(log, "sofia@example.com", AccessRole.READER)
    assert _verdicts([DocumentShared(person="sofia", role=AccessRole.WRITER)], log) == [FindingKind.FAIL]
    assert _verdicts([DocumentShared(person="sofia", titled=["budget"])], log) == [FindingKind.FAIL]


def test_an_expectation_naming_nobody_in_the_scenario_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="no such person: ghost"):
        scenario(OWNER, expect=[DocumentShared(person="ghost")])
