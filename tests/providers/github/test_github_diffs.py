"""The patch and the counts of a file's change, as GitHub shows them in a pull request. Two patches the real service
answered are held in `tests/data/github_rest/`; the writer must reproduce them from the two versions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minutehand.adapters.providers.github import diffs
from minutehand.domain.errors import NotServed

CAPTURE = Path(__file__).parents[2] / "data" / "github_rest" / "observed-2026-10-09.json"


def recorded(claim: str) -> list[dict[str, object]]:
    found = json.loads(CAPTURE.read_text())["exchanges"]
    [exchange] = [e for e in found if e["claim"] == claim]
    return exchange["observed"]["files"]


def versions(patch: str) -> tuple[list[str], list[str]]:
    """The old and the new lines a one-hunk patch is the difference of; a row followed by the marker has no newline."""
    old: list[str] = []
    new: list[str] = []
    rows = patch.split("\n")[1:]
    for n, row in enumerate(rows):
        if row.startswith("\\"):
            continue
        last = n + 1 < len(rows) and rows[n + 1].startswith("\\")
        text = row[1:] + ("" if last else "\n")
        if row[0] != "+":
            old.append(text)
        if row[0] != "-":
            new.append(text)
    return old, new


def test_the_patch_of_a_file_added_whole_is_the_one_the_real_service_answered() -> None:
    """Recorded: `@@ -0,0 +1,2 @@` and the lines added, with no newline after the last."""
    [added] = recorded("files of a pull request: a new file")
    old, new = versions(str(added["patch"]))
    assert old == []
    assert diffs.patch(old, new) == added["patch"]
    assert (diffs.counts(old, new).additions, diffs.counts(old, new).deletions) == (
        added["additions"],
        added["deletions"],
    )


def test_the_patch_of_a_change_to_a_file_with_no_final_newline_is_the_one_the_real_service_answered() -> None:
    """Recorded: a line without its newline is `-` with the marker below it, and the same text with one is `+`; the
    last added line has none, and the marker follows it."""
    [modified] = recorded("files of a pull request: a modified file whose last line has no newline")
    old, new = versions(str(modified["patch"]))
    assert old == ["Hello World!"] and new[0] == "Hello World!\n" and new[-1] == "$ touch README"
    assert diffs.patch(old, new) == modified["patch"]
    counted = diffs.counts(old, new)
    assert (counted.additions, counted.deletions) == (modified["additions"], modified["deletions"])


def test_a_file_deleted_whole_is_all_minus_lines() -> None:
    assert diffs.patch(["a\n", "b\n"], []) == "@@ -1,2 +0,0 @@\n-a\n-b"
    assert diffs.patch(["a\n"], []) == "@@ -1 +0,0 @@\n-a"


def test_a_change_in_the_middle_carries_three_lines_of_context_each_side() -> None:
    old = [f"line {n}\n" for n in range(1, 11)]
    new = [*old[:5], "five and a half\n", *old[6:]]
    assert diffs.patch(old, new) == (
        "@@ -3,7 +3,7 @@\n line 3\n line 4\n line 5\n-line 6\n+five and a half\n line 7\n line 8\n line 9"
    )


def test_a_change_near_the_top_has_what_context_there_is() -> None:
    old = ["a\n", "b\n", "c\n", "d\n", "e\n"]
    new = ["A\n", *old[1:]]
    assert diffs.patch(old, new) == "@@ -1,4 +1,4 @@\n-a\n+A\n b\n c\n d"


def test_lines_inserted_and_lines_deleted_are_one_hunk_each() -> None:
    old = ["a\n", "b\n", "c\n", "d\n"]
    assert diffs.patch(old, ["a\n", "b\n", "x\n", "y\n", "c\n", "d\n"]) == "@@ -1,4 +1,6 @@\n a\n b\n+x\n+y\n c\n d"
    assert diffs.patch(old, ["a\n", "d\n"]) == "@@ -1,4 +1,2 @@\n a\n-b\n-c\n d"


def test_counts_are_those_of_a_minimal_edit_script() -> None:
    old = ["a\n", "b\n", "c\n", "d\n", "e\n"]
    new = ["a\n", "x\n", "c\n", "e\n", "f\n"]
    counted = diffs.counts(old, new)
    assert (counted.additions, counted.deletions) == (2, 2)
    assert (diffs.counts(old, old).additions, diffs.counts(old, old).deletions) == (0, 0)


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        (["a\n", "b\n", "c\n"], ["a\n", "c\n", "b\n"], "aligned with each other"),
        (["a\n", "x\n", "b\n"], ["a\n", "x\n", "x\n", "b\n"], "slide"),
        (["a\n", "b\n"], ["a\n", "b\n"], "did not change"),
    ],
    ids=["interleaved", "duplicate-neighbour", "unchanged"],
)
def test_a_patch_git_may_write_either_way_is_refused_by_name(old: list[str], new: list[str], why: str) -> None:
    with pytest.raises(NotServed, match=why):
        diffs.patch(old, new)


def test_positions_count_the_lines_below_the_hunk_header_and_the_marker_is_one() -> None:
    """Documented: "The line just below the `@@` line is position 1, the next line is position 2, and so on."
    Recorded: a `\\ No newline` row is a position too."""
    patch = "@@ -1 +1,3 @@\n-Hello\n\\ No newline at end of file\n+Hello\n+World"
    assert diffs.positions(patch) == [
        (1, "-Hello"),
        (2, "\\ No newline at end of file"),
        (3, "+Hello"),
        (4, "+World"),
    ]
    assert diffs.line_numbers(patch) == [
        (1, 1, None, "-Hello"),
        (2, None, None, "\\ No newline at end of file"),
        (3, None, 1, "+Hello"),
        (4, None, 2, "+World"),
    ]


def test_lines_split_at_newlines_only_and_the_last_may_have_none() -> None:
    assert diffs.split("a\r\nb\x0bc\n") == ["a\r\n", "b\x0bc\n"]
    assert diffs.split("a\nb") == ["a\n", "b"] and diffs.split("") == [] and diffs.split("\n") == ["\n"]


def test_the_hunk_through_a_position_is_the_diff_hunk_the_real_service_gave_the_comment_there() -> None:
    """Recorded: a review comment's `diff_hunk` is the patch from its header through the line at its `position`."""
    found = json.loads(CAPTURE.read_text())["exchanges"]
    [files] = [e for e in found if e["claim"].endswith("a modified file whose last line has no newline")]
    [comments] = [e for e in found if e["claim"] == "review comments carry the diff up to the line"]
    patch = str(files["observed"]["files"][0]["patch"])
    seen = comments["observed"]["comments"]
    assert [c["position"] for c in seen] == [4, 7, 6]
    for comment in seen:
        assert diffs.hunk_through(patch, int(comment["position"])) == comment["diff_hunk"]


def test_a_patch_gives_each_line_its_number_on_the_side_it_is_on() -> None:
    """The line of the new file a `+` row or a context row is, and of the old file a `-` row or a context row."""
    patch = diffs.patch(["a\n", "b\n", "c\n", "d\n"], ["a\n", "x\n", "c\n", "d\n"])
    assert diffs.line_numbers(patch) == [
        (1, 1, 1, " a"),
        (2, 2, None, "-b"),
        (3, None, 2, "+x"),
        (4, 3, 3, " c"),
        (5, 4, 4, " d"),
    ]


def test_the_patch_of_lines_added_after_the_last_is_the_one_the_real_service_answered() -> None:
    """Recorded: `@@ -1 +1,3 @@`, the line kept as context and the two lines added."""
    [appended] = recorded("files of a pull request: lines added after the last, with context")
    assert diffs.patch(["Hello World!\n"], ["Hello World!\n", "Hello world2\n", "Hello world3\n"]) == appended["patch"]


def test_the_patch_of_a_removed_file_is_the_one_the_real_service_answered() -> None:
    [removed] = recorded("files of a pull request: a removed file")
    assert diffs.patch(["Hello World!\n"], []) == removed["patch"]
    counted = diffs.counts(["Hello World!\n"], [])
    assert (counted.additions, counted.deletions) == (removed["additions"], removed["deletions"])
