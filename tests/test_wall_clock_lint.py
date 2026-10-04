"""The lint is only a check if it is seen to fail."""

from pathlib import Path

from lints import wall_clock


def test_it_flags_a_machine_clock_read(tmp_path: Path) -> None:
    (tmp_path / "fake.py").write_text("import time\nts = time.time()\n")
    findings = wall_clock.run(tmp_path)
    assert [(f.file, f.line) for f in findings] == [("fake.py", 2)]


def test_an_exemption_needs_a_reason(tmp_path: Path) -> None:
    (tmp_path / "bare.py").write_text("import time\nts = time.time()  # clock-lint: exempt\n")
    (tmp_path / "argued.py").write_text("import time\nts = time.time()  # clock-lint: exempt wall_time field\n")
    assert [f.file for f in wall_clock.run(tmp_path)] == ["bare.py"]


def test_durations_are_left_alone(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("import time\nstarted = time.perf_counter()\nwaited = time.monotonic()\n")
    assert wall_clock.run(tmp_path) == []


def test_the_package_itself_is_clean() -> None:
    assert wall_clock.run(Path(__file__).parent.parent / "src") == []
