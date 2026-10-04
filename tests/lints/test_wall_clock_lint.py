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
