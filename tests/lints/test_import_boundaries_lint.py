"""Each boundary is shown to fail when crossed, and to stay quiet beside it."""

import pytest
from conftest import SRC, Plant

from lints import import_boundaries


@pytest.mark.parametrize(
    ("rel", "line"),
    [
        ("minutehand/domain/a.py", "from minutehand.adapters.store.sqlite import SqliteStore"),
        ("minutehand/domain/b.py", "from minutehand.application.run_clock import RunClock"),
        ("minutehand/domain/c.py", "from minutehand.ports.clock import Clock"),
        ("minutehand/domain/d.py", "import minutehand.adapters.store"),
        ("minutehand/domain/e.py", "from minutehand import adapters"),
        ("minutehand/domain/f.py", "from ..adapters import store"),
        ("minutehand/domain/sub/__init__.py", "from ...application import run_clock"),
        ("minutehand/ports/a.py", "from minutehand.application.run_clock import RunClock"),
        ("minutehand/ports/b.py", "from minutehand.adapters.store import sqlite"),
        ("minutehand/application/a.py", "from minutehand.adapters.store.sqlite import SqliteStore"),
        ("minutehand/checks/a.py", "from minutehand.application.run_clock import RunClock"),
        ("minutehand/checks/b.py", "from minutehand.adapters.store import sqlite"),
        ("minutehand/checks/c.py", "import minutehand"),
        ("minutehand/adapters/providers/slack/a.py", "from minutehand.adapters.providers.jira.wire import Issue"),
        ("minutehand/adapters/providers/slack/b.py", "from minutehand.adapters.providers import jira"),
        ("minutehand/adapters/providers/slack/c.py", "from ..jira import wire"),
        ("minutehand/adapters/providers/slack.py", "import minutehand.adapters.providers.jira"),
    ],
)
def test_crossing_a_boundary_is_flagged_with_file_and_line(plant: Plant, rel: str, line: str) -> None:
    root = plant({rel: f'"""doc"""\n\n{line}\n', "minutehand/adapters/providers/jira/__init__.py": ""})
    found = import_boundaries.run(root)
    assert [(f.file, f.line) for f in found] == [(rel, 3)]


def test_an_import_inside_a_function_or_type_checking_block_is_still_an_import(plant: Plant) -> None:
    root = plant(
        {
            "minutehand/domain/a.py": """
                from typing import TYPE_CHECKING
                if TYPE_CHECKING:
                    from minutehand.adapters.store.sqlite import SqliteStore

                def load() -> None:
                    from minutehand.application import run_clock
            """
        }
    )
    assert [f.line for f in import_boundaries.run(root)] == [3, 6]


def test_legal_imports_beside_the_boundaries_are_left_alone(plant: Plant) -> None:
    root = plant(
        {
            "minutehand/domain/a.py": "from minutehand.domain.world import EntityRef\nfrom minutehand import domain\n",
            "minutehand/domain/b.py": "from minutehand.adapters_notes import x\nfrom . import world\n",
            "minutehand/ports/a.py": "from minutehand.domain.world import EntityRef\nfrom minutehand.ports.clock import Clock\n",
            "minutehand/application/a.py": "from minutehand.domain import world\nfrom minutehand.ports.store import Store\n",
            "minutehand/checks/a.py": (
                "from minutehand.domain.checks import Finding\nfrom minutehand.ports import store\n"
                "from minutehand.checks.expectations import x\nfrom minutehand import domain\nimport re\n"
            ),
            "minutehand/adapters/store/sqlite.py": (
                "from minutehand.ports.clock import Clock\nfrom minutehand.application.run_clock import RunClock\n"
            ),
            "minutehand/adapters/providers/slack/a.py": (
                "from minutehand.adapters.providers.slack.wire import Message\nfrom . import wire\n"
                "from minutehand.adapters.providers import Registry\nfrom minutehand.ports.store import Store\n"
            ),
        }
    )
    assert import_boundaries.run(root) == []


def test_an_exemption_needs_a_reason(plant: Plant) -> None:
    root = plant(
        {
            "minutehand/domain/bare.py": "from minutehand.adapters import store  # import-lint: exempt\n",
            "minutehand/domain/argued.py": (
                "from minutehand.adapters import (  # import-lint: exempt the reason names why\n    store,\n)\n"
            ),
            "minutehand/domain/later.py": (
                "from minutehand.adapters import (\n    store,  # import-lint: exempt on any line of the statement\n)\n"
            ),
        }
    )
    assert [f.file for f in import_boundaries.run(root)] == ["minutehand/domain/bare.py"]


def test_the_package_itself_is_clean() -> None:
    assert import_boundaries.run(SRC) == []
