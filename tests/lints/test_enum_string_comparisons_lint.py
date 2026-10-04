"""A member's value spelled out, and a value no member has, are both seen to fail."""

from pathlib import Path

from conftest import SRC, Plant

from lints import enum_string_comparisons

KINDS = """
    from enum import StrEnum
    from typing import Literal

    from pydantic import BaseModel


    class FindingKind(StrEnum):
        FAIL = "fail"
        REVIEW = "review"


    class Actor(StrEnum):
        AGENT = "agent"
        PERSON = "person"


    class Finding(BaseModel):
        kind: FindingKind
        actor: Actor | None = None
        title: str


    class TicketSnapshot(BaseModel):
        kind: Literal["ticket"] = "ticket"


    class Person(BaseModel):
        name: str
"""


def _tree(plant: Plant, use: str) -> Path:
    return plant({"minutehand/domain/kinds.py": KINDS, "minutehand/checks/use.py": use})


def _lines(root: Path) -> list[tuple[str, int | None]]:
    return [(f.file, f.line) for f in enum_string_comparisons.run(root)]


def test_a_members_value_spelled_out_is_flagged_and_names_the_member(plant: Plant) -> None:
    root = _tree(
        plant,
        """
        def f(finding, other) -> bool:
            if finding.kind == "fail":
                return True
            return other != "review"
        """,
    )
    found = enum_string_comparisons.run(root)
    assert [(f.file, f.line) for f in found] == [("minutehand/checks/use.py", 2), ("minutehand/checks/use.py", 4)]
    assert "FindingKind.FAIL" in found[0].message
    assert "FindingKind.REVIEW" in found[1].message


def test_a_value_no_member_has_is_flagged_on_an_enum_typed_expression(plant: Plant) -> None:
    root = _tree(
        plant,
        """
        from minutehand.domain.kinds import FindingKind

        def f(finding, kind: FindingKind, actor: "Actor | None") -> bool:
            local: FindingKind = kind
            return finding.kind == "fial" or kind != "fial" or local == "fial" or actor == "agnet"
        """,
    )
    assert _lines(root) == [("minutehand/checks/use.py", 5)] * 4


def test_membership_and_match_are_read_too(plant: Plant) -> None:
    root = _tree(
        plant,
        """
        def f(finding) -> int:
            if finding.kind in ("fail", "review"):
                return 1
            match finding.actor:
                case "agnet":
                    return 2
                case "person" | "agent":
                    return 3
            return 0
        """,
    )
    assert (
        _lines(root)
        == [("minutehand/checks/use.py", 2)] * 2
        + [("minutehand/checks/use.py", 5)]
        + [("minutehand/checks/use.py", 7)] * 2
    )


def test_comparisons_beside_the_rule_are_left_alone(plant: Plant) -> None:
    root = _tree(
        plant,
        """
        from minutehand.domain.kinds import Actor, FindingKind

        def f(finding, person, payload: dict[str, str], name: str, snapshot) -> bool:
            return (
                finding.kind == FindingKind.FAIL
                or finding.actor in (Actor.AGENT, Actor.PERSON)
                or payload["type"] == "fail"
                or name == "agent"
                or person.name == "agent"
                or finding.title == "anything at all"
                or snapshot.kind == "ticket"
                or unknown_thing == "not a value of anything"
            )
        """,
    )
    assert _lines(root) == []


def test_the_file_that_defines_an_enum_may_compare_its_own_values(plant: Plant) -> None:
    root = plant(
        {
            "minutehand/domain/kinds.py": KINDS
            + """
    def parse(raw: str) -> FindingKind:
        return FindingKind.FAIL if raw == "fail" else FindingKind.REVIEW
"""
        }
    )
    assert _lines(root) == []


def test_an_exemption_needs_a_reason(plant: Plant) -> None:
    root = _tree(
        plant,
        """
        def f(event, other) -> bool:
            bare = event.kind == "fail"  # enum-lint: exempt
            argued = other == "fail"  # enum-lint: exempt the provider's own status vocabulary
            return bare or argued
        """,
    )
    assert _lines(root) == [("minutehand/checks/use.py", 2)]


def test_the_package_itself_is_clean() -> None:
    assert enum_string_comparisons.run(SRC) == []
