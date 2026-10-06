"""The agent's reported commitments held against the waits the world shows."""

from __future__ import annotations

from minutehand.checks.reported_against_world import ReportedAgainstWorld
from minutehand.domain.agent import Commitment, CommitmentStatus, WaitingOn
from minutehand.domain.checks import CommitmentsReported, FindingKind, RunView
from minutehand.domain.scenario import Silent
from tests.checks.world import Log, at, person, reply, scenario, view

OWNER, SOFIA = person("owner"), person("sofia")
DANIA = person("dania", reply=Silent())


def said(key: str, status: CommitmentStatus, *, opened: float = 0, email: str = "sofia@example.com") -> Commitment:
    return Commitment(
        key=key,
        description="the contract",
        waiting_on=WaitingOn.PERSON,
        person_email=email,
        opened_at=at(opened),
        status=status,
    )


def reports(*at_hours: tuple[float, list[Commitment]]) -> list[CommitmentsReported]:
    return [CommitmentsReported(wake=n + 1, at=at(h), commitments=c) for n, (h, c) in enumerate(at_hours)]


def sofia_asked(*, answered_at: float | None) -> RunView:
    """Sofia, asked at 0, answers within two hours when she answers at all."""
    log = Log()
    log.message([SOFIA], 0)
    log.message([OWNER], 20, text="status")
    replies = [reply(SOFIA, log.events[0], answered_at)] if answered_at is not None else []
    return view(scenario(OWNER, SOFIA), log, replies)


def dania_asked() -> RunView:
    """Dania is silent: asked at 0, she never answers, and the wait falls due 66 hours on."""
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 100, text="status")
    return view(scenario(OWNER, DANIA), log, [])


def check(built: RunView, *said_at: tuple[float, list[Commitment]]) -> list[tuple[FindingKind, str]]:
    found = ReportedAgainstWorld().run(built.model_copy(update={"reported": reports(*said_at)})).findings
    return [(f.kind, f.message) for f in found]


def test_a_commitment_reported_met_while_the_person_never_answered_fails() -> None:
    met = said("contract", CommitmentStatus.MET, email="dania@example.com")
    assert check(dania_asked(), (5, [met]), (9, [met])) == [
        (
            FindingKind.FAIL,
            "wake 1 reported 'contract' met, waiting on dania, while dania had not answered: a wait on them was open "
            "and nothing of theirs had settled since the commitment opened",
        )
    ]


def test_a_commitment_reported_met_after_the_person_answered_passes() -> None:
    assert check(sofia_asked(answered_at=1), (5, [said("contract", CommitmentStatus.MET)])) == []


def test_a_commitment_still_open_hours_after_the_answer_landed_is_reviewed_once() -> None:
    found = check(
        sofia_asked(answered_at=1),
        (5, [said("contract", CommitmentStatus.OPEN)]),
        (9, [said("contract", CommitmentStatus.OPEN)]),
    )
    assert found == [
        (
            FindingKind.REVIEW,
            "wake 1 still reported 'contract' open, waiting on sofia, 4 hours after sofia's answer settled their last "
            "wait",
        )
    ]


def test_a_commitment_still_open_within_the_grace_of_the_answer_is_not_reviewed() -> None:
    assert check(sofia_asked(answered_at=1), (1.5, [said("contract", CommitmentStatus.OPEN)])) == []


def test_an_overdue_wait_the_agent_no_longer_reports_is_reviewed_as_lost() -> None:
    assert check(dania_asked(), (70, []), (80, [])) == [
        (
            FindingKind.REVIEW,
            "a wait on dania was overdue by 4 hours as wake 1 ended, and none of the commitments the agent reported "
            "was an open one on dania",
        )
    ]


def test_an_overdue_wait_the_agent_still_reports_is_not_lost() -> None:
    assert check(dania_asked(), (70, [said("contract", CommitmentStatus.OPEN, email="dania@example.com")])) == []


def test_an_agent_that_reports_no_commitments_is_held_to_none_silently() -> None:
    report = ReportedAgainstWorld().run(dania_asked())
    assert report.findings == [] and report.blocked == [] and report.notes == []


def test_a_commitment_met_by_an_answer_is_not_failed_by_a_later_question_still_open() -> None:
    log = Log()
    first = log.message([SOFIA], 0)
    log.message([OWNER], 2, text="status")
    second = log.message([SOFIA], 3, text="One more thing: who signs?")
    log.message([OWNER], 40, text="status")
    built = view(scenario(OWNER, SOFIA), log, [reply(SOFIA, first, 1), reply(SOFIA, second, 30)])
    assert any(o.settled_at is None or o.settled_at > at(5) for o in built.obligations if o.opened_at == at(3))

    assert check(built, (5, [said("contract", CommitmentStatus.MET)])) == []
