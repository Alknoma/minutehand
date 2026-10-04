"""The checks, run on a real captured run of an agent that went wrong in known ways.

`tests/data/partner_pipeline/` is run f431fc97f427 from alknoma-cloud's field observation,
converted to this package's models: an agent asked to secure three partnerships for "Ayven"
researched "Aiven", filed tickets under that name, sent one reminder 33 hours late and never
filed the legal-review ticket the goal depended on.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from minutehand.checks.effectiveness import measure
from minutehand.checks.expectations import Expectations
from minutehand.checks.near_miss_name import NearMissName
from minutehand.checks.repeated_message import RepeatedMessage
from minutehand.domain.checks import FindingKind, RunView
from minutehand.domain.world import MessageSnapshot, TicketSnapshot

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


def test_only_the_real_repeat_is_flagged_for_review_not_the_template_alike_pairs() -> None:
    # This asserted three flags until the check weighed shared wording by its rarity. Of the
    # three, only [3, 4] asks the same thing twice (who can approve the terms now that the
    # legal reviewer cannot be assigned); [4, 5] and [7, 9] share the agent's template.
    findings = RepeatedMessage().run(WORLD).findings
    assert [(f.kind, f.evidence) for f in findings] == [(FindingKind.REVIEW, [3, 4])]
    assert "18 seconds apart" in findings[0].message


def test_the_missing_legal_review_ticket_is_the_one_unmet_expectation() -> None:
    findings = Expectations().run(WORLD).findings
    assert [f.message for f in findings] == ["ticket created for dania: wanted at least 1, found 0"]


def test_the_scorecard_counts_only_the_time_the_agent_itself_lost() -> None:
    card = measure(TIMELINE, findings=[], met=3, ended_at=TIMELINE.wakes[-1].sim_time)
    assert (card.waits_opened, card.waits_open_at_end) == (12, 5)
    assert (card.follow_ups_due, card.follow_ups_made, card.follow_ups_late) == (3, 3, 1)
    assert timedelta(hours=32) < card.time_lost < timedelta(hours=33)
    assert card.slowest_follow_up == card.time_lost
    assert (card.wakes, card.idle_wakes) == (20, 3)


def test_a_wait_nobody_followed_up_costs_the_whole_stretch() -> None:
    silent = [o.model_copy(update={"agent_touches": []}) for o in TIMELINE.obligations]
    card = measure(
        TIMELINE.model_copy(update={"obligations": silent}), findings=[], met=3, ended_at=TIMELINE.wakes[-1].sim_time
    )
    assert (card.follow_ups_made, card.follow_ups_late) == (0, 3)
    assert card.time_lost > timedelta(days=3)
