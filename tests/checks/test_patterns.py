"""Every pattern a finding names exists, has a page, and the pages name no product."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from minutehand.checks.patterns import PATTERNS, pattern
from minutehand.checks.runner import discover, evaluate

from tests.test_checks_on_reference_run import TIMELINE, WORLD

ROOT = Path(__file__).parents[2]
DESIGN_KEYS = {
    "expiry_on_every_wait", "check_world_before_model", "absence_aware", "budgeted_follow_up", "bounded_asking",
    "one_open_ask_per_person", "no_double_tick", "honest_closure", "confirm_names",
}


def test_the_nine_patterns_of_the_design_are_the_data() -> None:
    assert [p.key for p in PATTERNS] == list(dict.fromkeys(p.key for p in PATTERNS))
    assert {p.key for p in PATTERNS} == DESIGN_KEYS


def test_an_unknown_pattern_key_is_refused() -> None:
    with pytest.raises(KeyError, match="no pattern 'expiry_on_some_waits'"):
        pattern("expiry_on_some_waits")


def test_every_pattern_a_check_declares_exists() -> None:
    for check in discover():
        declared = vars(type(check))["pattern"]
        assert declared is None or pattern(declared).key == declared, check.id


def test_every_pattern_a_finding_carries_exists_and_is_its_checks_own() -> None:
    declared = {c.id: vars(type(c))["pattern"] for c in discover()}
    findings = evaluate(WORLD).findings + evaluate(TIMELINE).findings
    assert findings
    for finding in findings:
        assert finding.pattern == declared[finding.check]
        assert finding.pattern is None or pattern(finding.pattern)


def test_every_pattern_has_a_page_and_no_page_names_the_reference_product() -> None:
    banned = re.compile("|".join(map(re.escape, WORLD.scenario.protected_names)), re.IGNORECASE)
    for p in PATTERNS:
        assert p.reference is not None
        page = (ROOT / p.reference).read_text()
        assert page.startswith(f"# {p.title}\n"), p.key
        assert not banned.search(page), p.key
