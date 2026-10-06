"""What the agent had planned when a wait fell due, read from the run loop's table (`RunView.dues`)."""

from __future__ import annotations

from datetime import datetime

from minutehand.checks.late_follow_up import LateFollowUp
from minutehand.checks.no_follow_up import NoFollowUp
from minutehand.checks.planned_past_due import PlannedPastDue
from minutehand.domain.checks import FindingKind, RunView
from minutehand.domain.clock import Due, DueClosed, DueEntry, DueKind, DueSource
from minutehand.domain.scenario import Silent
from tests.checks.world import Log, at, person, reply, scenario, view

OWNER, SOFIA = person("owner"), person("sofia")
DANIA = person("dania", reply=Silent())


def planned(due_at: datetime, *, entered: float = 0, wake: int = 1, source: DueSource = DueSource.REPORTED) -> DueEntry:
    kind = DueKind.AGENT_WAKE if source is not DueSource.REPLY else DueKind.PERSON_REPLY
    return DueEntry(
        due=Due(at=due_at, kind=kind, ref="next_wake" if kind is DueKind.AGENT_WAKE else "reply:0"),
        source=source,
        entered_at=at(entered),
        entered_wake=wake,
    )


def fired(entry: DueEntry) -> DueEntry:
    return entry.model_copy(update={"closed": DueClosed.FIRED, "closed_at": entry.due.at, "closed_wake": 1})


def _sofia_chased_at(hours: float, *dues: DueEntry) -> RunView:
    """Sofia, asked at 0, answers in at most 2 hours, so the wait falls due at 2; the agent chases her at `hours`."""
    log = Log()
    log.message([SOFIA], 0)
    log.message([SOFIA], hours, text="Any news on the contract?")
    log.message([OWNER], 40, text="status")
    built = view(scenario(OWNER, SOFIA), log, [reply(SOFIA, log.events[0], 30)])
    return built.model_copy(update={"dues": list(dues)})


def test_a_follow_up_in_time_only_because_a_reply_woke_the_agent_is_reviewed() -> None:
    plan = planned(at(30))
    woke = fired(planned(at(2.5), source=DueSource.REPLY))
    [finding] = PlannedPastDue().run(_sofia_chased_at(2.5, plan, woke)).findings
    assert finding.kind is FindingKind.REVIEW and finding.at == at(2)
    assert finding.message == (
        "wait on sofia fell due while the agent's own next wake was 1 day 4 hours later (reported in wake 1); "
        "it followed up in time only because a person's reply woke it"
    )
    assert finding.pattern == "expiry_on_every_wait" and finding.evidence == [1, 2]


def test_a_follow_up_the_agent_planned_for_is_not_reviewed() -> None:
    assert PlannedPastDue().run(_sofia_chased_at(2.5, fired(planned(at(2.5))))).findings == []


def test_a_plan_made_after_the_wait_fell_due_does_not_count_for_it() -> None:
    late_plan = planned(at(2.5), entered=2.2, wake=2)
    [finding] = PlannedPastDue().run(_sofia_chased_at(2.5, fired(late_plan))).findings
    assert "the agent had asked for no wake of its own" in finding.message


def test_planned_past_due_without_a_table_is_blocked_not_clean() -> None:
    built = _sofia_chased_at(2.5).model_copy(update={"dues": None})
    report = PlannedPastDue().run(built)
    assert report.findings == [] and report.blocked == [
        "planned_past_due: the run kept no table of what was due, so nothing says what the agent planned"
    ]


def test_a_late_follow_up_says_how_far_off_the_agents_own_plan_was() -> None:
    [finding] = LateFollowUp().run(_sofia_chased_at(10, fired(planned(at(10))))).findings
    assert finding.message == (
        "followed up 8 hours after the wait expired; when it expired, the agent's own next wake was 8 hours later "
        "(reported in wake 1)"
    )


def test_a_wait_never_followed_up_says_the_agent_had_asked_for_no_wake() -> None:
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 100, text="status")
    built = view(scenario(OWNER, DANIA), log, []).model_copy(update={"dues": []})
    [finding] = NoFollowUp().run(built).findings
    assert finding.message.endswith("; when it fell due, the agent had asked for no wake of its own")
