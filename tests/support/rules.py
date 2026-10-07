"""Rules written as a team writes them, in YAML, for tests that judge a run by them."""

from __future__ import annotations

import yaml

from minutehand.domain.assessments import Rule


def rules(text: str) -> list[Rule]:
    """The rules of an `assess:` list written in YAML."""
    return [Rule.model_validate(r) for r in yaml.safe_load(text)]
