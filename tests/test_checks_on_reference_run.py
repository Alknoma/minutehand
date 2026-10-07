"""The checks, run on a real captured run of an agent that went wrong in known ways.

`tests/data/partner_pipeline/` is run f431fc97f427 from alknoma-cloud's field observation,
converted to this package's models: an agent asked to secure three partnerships for "Ayven"
researched "Aiven", filed tickets under that name, sent one reminder 33 hours late and never
filed the legal-review ticket the goal depended on.
"""

from __future__ import annotations

from pathlib import Path

from minutehand.checks.assessments import Assessments
from minutehand.checks.effectiveness import measure
from minutehand.checks.expectations import Expectations
from minutehand.checks.near_miss_name import NearMissName
from minutehand.domain.checks import FindingKind, RunView
from minutehand.domain.world import MessageSnapshot, TicketSnapshot
from tests.checks.world import rules

DATA = Path(__file__).parent / "data" / "partner_pipeline"
WORLD = RunView.model_validate_json((DATA / "world.json").read_text())
TIMELINE = RunView.model_validate_json((DATA / "timeline.json").read_text())


def test_the_wrong_company_name_is_found_in_two_tickets_and_two_messages() -> None:
    findings = NearMissName().run(WORLD).findings
    assert len(findings) == 4
    assert {f.message for f in findings} == {'wrote "Aiven" where the scenario says "Ayven"'}
    assert all(f.kind is FindingKind.FAIL and f.pattern == "confirm_names" for f in findings)


def test_the_name_check_is_clean_once_the_name_is_corrected() -> None:
    fixed = []
    for event in WORLD.events:
        after = event.after
        if isinstance(after, TicketSnapshot):
            after = after.model_copy(
                update={"title": after.title.replace("Aiven", "Ayven"), "body": after.body.replace("Aiven", "Ayven")}
            )
        elif isinstance(after, MessageSnapshot):
            after = after.model_copy(update={"text": after.text.replace("Aiven", "Ayven")})
        fixed.append(event.model_copy(update={"after": after}))
    assert NearMissName().run(WORLD.model_copy(update={"events": fixed})).findings == []


def test_a_scenario_with_no_protected_names_says_so_and_finds_nothing() -> None:
    bare = WORLD.model_copy(update={"scenario": WORLD.scenario.model_copy(update={"protected_names": []})})
    report = NearMissName().run(bare)
    assert report.findings == [] and report.notes == ["scenario declares no protected_names"]


def test_a_spacing_rule_finds_the_two_messages_sent_seconds_apart() -> None:
    """The agent wrote its owner seven times in twenty minutes, twice asking who could approve the terms 18 seconds
    apart. Minutehand's own check once weighed the wording two messages shared and flagged only that pair; a team's
    rule says how close is too close, and finds every message that close."""
    spaced = rules(
        """
        - id: no_two_messages_within_minutes
          each: person
          count: {messages: {to: [person]}}
          gap_at_least: PT5M
          severity: review
        """
    )
    findings = Assessments().run(WORLD.model_copy(update={"rules": spaced})).findings
    assert [(f.kind, f.evidence, f.message) for f in findings] == [
        (
            FindingKind.REVIEW,
            [2, 3, 4, 5, 6, 7, 9],
            "owner: 7 messages; 5 were closer than 5 minutes to the one before, the closest 0 minutes",
        )
    ]


def test_the_missing_legal_review_ticket_is_the_one_unmet_expectation() -> None:
    findings = Expectations().run(WORLD).findings
    assert [f.message for f in findings if f.kind is FindingKind.FAIL] == [
        "ticket created for dania: wanted at least 1, found 0"
    ]


def test_the_scorecard_counts_what_happened_and_judges_none_of_it() -> None:
    card = measure(TIMELINE, findings=[], met=3, ended_at=TIMELINE.wakes[-1].sim_time)
    assert (card.waits_opened, card.waits_open_at_end, card.follow_ups_made) == (12, 5, 3)
    assert (card.wakes, card.idle_wakes, card.waits_settled, card.slowest_reaction) == (20, 3, 0, None)
