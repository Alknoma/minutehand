"""The runner finds every lint without a list, and a lint that did not run is not clean."""

from pathlib import Path

from conftest import SRC, Plant

from lints import __main__ as runner

LINTS = Path(runner.__file__).parent


def test_every_lint_module_in_the_directory_is_discovered() -> None:
    expected = sorted(p.stem for p in LINTS.glob("*.py") if not p.name.startswith("_"))
    assert runner.discover() == expected
    assert {"wall_clock", "import_boundaries", "enum_string_comparisons", "boundary_dicts"} <= set(expected)


def test_a_lint_that_cannot_be_imported_is_blocked_not_clean() -> None:
    result = runner.run_one("no_such_lint")
    assert result.blocked and not result.ok


def test_a_tree_with_any_violation_fails_the_whole_run(plant: Plant) -> None:
    root = plant({"minutehand/domain/a.py": "from minutehand.adapters import store\n"})
    assert runner.main(root) == 1
    assert runner.run_one("import_boundaries", root).findings
    assert runner.run_one("wall_clock", root).ok


def test_the_real_tree_passes() -> None:
    assert runner.main(SRC) == 0
