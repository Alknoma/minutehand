"""The difference between two versions of a file, as a pull request shows it.

A file is split into lines at `\\n` as git splits it (the last line may have none, which a patch marks). The number of
lines added and deleted is that of a minimal edit script, which every minimal diff agrees on. The patch itself, a
single hunk of changed lines with three lines of context before and after, is written only where the lines changed
make one hunk that git could not place any other way: a file added or deleted whole, or a change whose lines share
nothing with the lines they replace and sit where they cannot slide. Anything else is refused by name, since the
unified diff of a change git may align either of two ways is not something the reference gives.
"""

from __future__ import annotations

from dataclasses import dataclass

from minutehand.domain.errors import NotServed

CONTEXT = 3
"""Lines of context git puts before and after a change."""
NO_NEWLINE = "\\ No newline at end of file"
LCS_CELLS = 4_000_000
"""The most cells of work the count of changed lines is done in."""


def split(text: str) -> list[str]:
    """The lines of a text with their newlines; the last has none when the text does not end in one."""
    parts = text.split("\n")
    lines = [line + "\n" for line in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


@dataclass(frozen=True)
class Counts:
    additions: int
    deletions: int


def counts(old: list[str], new: list[str]) -> Counts:
    """Lines added and deleted by a minimal edit script from `old` to `new`."""
    start = 0
    while start < len(old) and start < len(new) and old[start] == new[start]:
        start += 1
    end = 0
    while end < len(old) - start and end < len(new) - start and old[len(old) - 1 - end] == new[len(new) - 1 - end]:
        end += 1
    left, right = old[start : len(old) - end], new[start : len(new) - end]
    if len(left) * len(right) > LCS_CELLS:
        raise NotServed(f"counting the lines changed between files of {len(old)} and {len(new)} lines")
    previous = [0] * (len(right) + 1)
    for line in left:
        row = [0]
        for column, other in enumerate(right):
            row.append(previous[column] + 1 if line == other else max(previous[column + 1], row[column]))
        previous = row
    common = previous[len(right)]
    return Counts(additions=len(right) - common, deletions=len(left) - common)


def _shown(prefix: str, line: str) -> list[str]:
    if line.endswith("\n"):
        return [prefix + line[:-1]]
    return [prefix + line, NO_NEWLINE]


def _range(start: int, length: int) -> str:
    """A hunk header's range: `start,length`, the length left out when it is one."""
    return str(start) if length == 1 else f"{start},{length}"


def patch(old: list[str], new: list[str]) -> str:
    """The patch of a change as GitHub answers it: a hunk header and its lines, with no newline at the end."""
    if not old and not new:
        raise NotServed("the patch of a file that did not change")
    if not old:
        body = [row for line in new for row in _shown("+", line)]
        return "\n".join([f"@@ -0,0 +{_range(1, len(new))} @@", *body])
    if not new:
        body = [row for line in old for row in _shown("-", line)]
        return "\n".join([f"@@ -{_range(1, len(old))} +0,0 @@", *body])
    start = 0
    while start < len(old) and start < len(new) and old[start] == new[start]:
        start += 1
    end = 0
    while end < len(old) - start and end < len(new) - start and old[len(old) - 1 - end] == new[len(new) - 1 - end]:
        end += 1
    if start == len(old) == len(new):
        raise NotServed("the patch of a file that did not change")
    removed, added = old[start : len(old) - end], new[start : len(new) - end]
    before, after = old[:start], old[len(old) - end :]
    if set(removed) & set(added):
        raise NotServed(
            "the patch of a file whose changed lines are aligned with each other: git may place them either way"
        )
    edges = [line for block in (removed, added) for line in (block[:1] + block[-1:])]
    if (before and before[-1] in edges) or (after and after[0] in edges):
        raise NotServed(
            "the patch of a change whose lines could slide past their neighbours: git may place it either way"
        )
    above, below = before[-CONTEXT:], after[:CONTEXT]
    first = len(before) - len(above) + 1
    rows = [row for line in above for row in _shown(" ", line)]
    rows += [row for line in removed for row in _shown("-", line)]
    rows += [row for line in added for row in _shown("+", line)]
    rows += [row for line in below for row in _shown(" ", line)]
    old_length, new_length = len(above) + len(removed) + len(below), len(above) + len(added) + len(below)
    return "\n".join([f"@@ -{_range(first, old_length)} +{_range(first, new_length)} @@", *rows])


def hunk_through(patch_text: str, position: int) -> str:
    """The hunk from its header down to the line at `position`: the diff of the line a comment on it refers to, as the
    real service shows it (recorded)."""
    return "\n".join(patch_text.split("\n")[: position + 1])


def positions(patch_text: str) -> list[tuple[int, str]]:
    """Every line of a patch below its hunk header with its position (the first is 1: the reference counts "the
    number of lines down from the first `@@` hunk header") and the line itself."""
    rows = patch_text.split("\n")[1:]
    return [(n, row) for n, row in enumerate(rows, start=1)]


def line_numbers(patch_text: str) -> list[tuple[int, int | None, int | None, str]]:
    """Each line of a one-hunk patch as (position, line in the old file, line in the new file, row)."""
    header, *rows = patch_text.split("\n")
    old_at, new_at = _starts(header)
    found: list[tuple[int, int | None, int | None, str]] = []
    for position, row in enumerate(rows, start=1):
        if row.startswith("+"):
            found.append((position, None, new_at, row))
            new_at += 1
        elif row.startswith("-"):
            found.append((position, old_at, None, row))
            old_at += 1
        elif row.startswith("\\"):
            found.append((position, None, None, row))
        else:
            found.append((position, old_at, new_at, row))
            old_at += 1
            new_at += 1
    return found


def _starts(header: str) -> tuple[int, int]:
    _, old_range, new_range, _ = header.split(" ", 3)
    return int(old_range[1:].split(",")[0]), int(new_range[1:].split(",")[0])
