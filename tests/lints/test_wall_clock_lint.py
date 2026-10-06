"""The lint is only a check if it is seen to fail."""

from conftest import SRC, Plant

from lints import wall_clock


def test_it_flags_a_machine_clock_read(plant: Plant) -> None:
    root = plant(
        {
            "fake.py": "import time\nts = time.time()\n",
            "pkg/stamp.py": "import datetime as dt\nfrom datetime import date\na = dt.datetime.now()\nb = date.today()\n",
        }
    )
    assert [(f.file, f.line) for f in wall_clock.run(root)] == [
        ("fake.py", 2),
        ("pkg/stamp.py", 3),
        ("pkg/stamp.py", 4),
    ]


def test_it_flags_a_clock_read_under_an_imported_name_or_alias(plant: Plant) -> None:
    root = plant(
        {
            "bare.py": "from time import time\nts = time()\n",
            "renamed.py": "from time import time_ns as ns\nts = ns()\n",
            "cls.py": "from datetime import datetime as dt\nnow = dt.now()\n",
            "mod.py": "import time as t\nts = t.time()\n",
            "today.py": "from datetime import date as d\nday = d.today()\n",
        }
    )
    assert sorted((f.file, f.line, f.message.split("(")[0]) for f in wall_clock.run(root)) == [
        ("bare.py", 2, "time.time"),
        ("cls.py", 2, "datetime.now"),
        ("mod.py", 2, "time.time"),
        ("renamed.py", 2, "time.time_ns"),
        ("today.py", 2, "date.today"),
    ]


def test_a_bare_call_to_a_name_no_clock_import_bound_is_left_alone(plant: Plant) -> None:
    root = plant(
        {
            "own.py": "def time():\n    return 0\nts = time()\n",
            "durations.py": "from time import monotonic as time\nts = time()\n",
            "other.py": "import calendar as t\nts = t.time()\n",
        }
    )
    assert wall_clock.run(root) == []


def test_an_exemption_needs_a_reason(plant: Plant) -> None:
    root = plant(
        {
            "bare.py": "import time\nts = time.time()  # clock-lint: exempt\n",
            "argued.py": "import time\nts = time.time()  # clock-lint: exempt wall_time field\n",
        }
    )
    assert [f.file for f in wall_clock.run(root)] == ["bare.py"]


def test_durations_are_left_alone(plant: Plant) -> None:
    root = plant({"ok.py": "import time\nstarted = time.perf_counter()\nwaited = time.monotonic()\n"})
    assert wall_clock.run(root) == []


def test_a_tests_directory_inside_the_root_is_not_scanned(plant: Plant) -> None:
    root = plant({"tests/test_x.py": "import time\nts = time.time()\n", "fake.py": "import time\nts = time.time()\n"})
    assert [f.file for f in wall_clock.run(root)] == ["fake.py"]


def test_a_root_that_sits_under_a_directory_named_tests_is_still_scanned(plant: Plant) -> None:
    root = plant({"tests/src/fake.py": "import time\nts = time.time()\n"})
    assert [f.file for f in wall_clock.run(root / "tests" / "src")] == ["fake.py"]


def test_the_package_itself_is_clean() -> None:
    assert wall_clock.run(SRC) == []
