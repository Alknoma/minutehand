"""Every pattern a finding names exists, has a page, and the pages name no product."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from minutehand.checks.patterns import PATTERNS, pattern
from minutehand.checks.runner import discover, evaluate
from minutehand.domain.checks import FindingKind
from tests.checks.world import rules
from tests.test_checks_on_reference_run import TIMELINE, WORLD

ROOT = Path(__file__).parents[2]
DESIGN_KEYS = {
    "expiry_on_every_wait",
    "check_world_before_model",
    "absence_aware",
    "budgeted_follow_up",
    "bounded_asking",
    "one_open_ask_per_person",
    "no_double_tick",
    "honest_closure",
    "confirm_names",
    "act_on_the_decision",
}


def test_the_ten_patterns_of_the_design_are_the_data() -> None:
    assert [p.key for p in PATTERNS] == list(dict.fromkeys(p.key for p in PATTERNS))
    assert {p.key for p in PATTERNS} == DESIGN_KEYS


def test_an_unknown_pattern_key_is_refused() -> None:
    with pytest.raises(KeyError, match="no pattern 'expiry_on_some_waits'"):
        pattern("expiry_on_some_waits")


def _declared() -> dict[str, str | None]:
    """Each check's pattern; the reader of the team's rules has none of its own, each rule naming its own."""
    return {c.id: vars(type(c))["pattern"] if "pattern" in vars(type(c)) else None for c in discover()}


def test_every_pattern_a_check_declares_exists() -> None:
    for check, declared in _declared().items():
        assert declared is None or pattern(declared).key == declared, check


def test_every_pattern_a_finding_carries_exists_and_is_its_checks_or_its_rules_own() -> None:
    written = rules(
        """
        - id: no_two_messages_within_minutes
          each: person
          count: {messages: {to: [person]}}
          gap_at_least: PT5M
          pattern: one_open_ask_per_person
        """
    )
    declared = _declared() | {r.id: r.pattern for r in written}
    findings = (
        evaluate(WORLD.model_copy(update={"rules": written}), stop=None).findings
        + evaluate(TIMELINE, stop=None).findings
    )
    assert "no_two_messages_within_minutes" in {f.check for f in findings}
    for finding in findings:
        # A met expectation is reported for the record and hands over no fix.
        met = finding.kind is FindingKind.INFORMATIONAL and finding.check == "expectations"
        assert finding.pattern == (None if met else declared[finding.check])
        assert finding.pattern is None or pattern(finding.pattern)


def test_every_pattern_has_a_page_and_no_page_names_the_reference_product() -> None:
    banned = re.compile("|".join(map(re.escape, WORLD.scenario.protected_names)), re.IGNORECASE)
    for p in PATTERNS:
        assert p.reference is not None
        page = (ROOT / p.reference).read_text()
        assert page.startswith(f"# {p.title}\n"), p.key
        assert not banned.search(page), p.key
