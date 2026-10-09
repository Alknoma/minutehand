"""A run kept before `conveys:` was retired still opens: its agent file is read under today's model."""

from __future__ import annotations

from pathlib import Path

from minutehand.session import stored_agent

KEPT = Path(__file__).parent / "data" / "kept" / "agent_with_conveys.json"


def test_an_agent_file_kept_with_conveys_opens_without_the_rule_that_counted_by_meaning() -> None:
    agent = stored_agent(KEPT)

    ids = [rule.id for rule in agent.assess]
    assert "chases_sam_20h_apart" in ids  # an empty `conveys` list is dropped, the rule kept
    assert "tells_owen_the_decision_conveyed" not in ids  # a rule counting by what messages convey is left out
    assert len(ids) == 5
