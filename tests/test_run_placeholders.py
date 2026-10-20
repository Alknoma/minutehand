"""`{run.port}` and `{run.dir}` in an agent file: filled once by `minutehand run`, as `run-all` fills them per
scenario, and refused, by name, by every reader that starts no agent."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

from minutehand import run_all
from minutehand.application.files import FileRefused, load_agent, problems
from minutehand.cli import _parser, _passed_on
from minutehand.domain.agent import Reported

AGENT = """\
version: 1
name: placeholder_agent
wakes:
  - {kind: reported, wake_url: "http://127.0.0.1:{run.port}/wake", report_url: "http://127.0.0.1:{run.port}/report"}
"""


def _agent(tmp_path: Path) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(AGENT, encoding="utf-8")
    return path


def test_run_fills_the_port_and_folder_in_the_agent_file_and_the_command(tmp_path: Path) -> None:
    filled = run_all.filled_for_run(
        _agent(tmp_path), ["python", "agent.py", "--port", "{run.port}", "--dir", "{run.dir}"], state=tmp_path
    )
    wake = filled.agent.wakes[0]
    assert isinstance(wake, Reported)
    port = urlsplit(wake.wake_url).port  # the trial's crash: "Port could not be cast to integer value"
    assert port is not None
    assert filled.command == [
        "python",
        "agent.py",
        "--port",
        str(port),
        "--dir",
        filled.environment["MINUTEHAND_RUN_DIR"],
    ]
    assert filled.environment["MINUTEHAND_RUN_PORT"] == str(port)
    assert Path(filled.environment["MINUTEHAND_RUN_DIR"]).is_dir()


def test_an_agent_file_with_a_run_placeholder_and_no_command_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileRefused, match=r"\{run\.port\}, which only `minutehand run` and `run-all` fill"):
        run_all.filled_for_run(_agent(tmp_path), None, state=tmp_path)


def test_load_agent_rejects_an_unfilled_run_placeholder(tmp_path: Path) -> None:
    with pytest.raises(FileRefused, match=r"holds \{run\.port\}"):
        load_agent(_agent(tmp_path))


def test_validate_reads_the_agent_file_as_a_run_fills_it(tmp_path: Path) -> None:
    _, model, said = problems(_agent(tmp_path))
    assert said == []
    assert model is not None


def test_run_all_hands_each_run_the_flags_that_tunnel_model_calls_and_capture(tmp_path: Path) -> None:
    args = _parser().parse_anywhere(
        [
            "run-all",
            str(tmp_path),
            "--agent",
            "agent.yaml",
            "--tunnel-model-calls",
            "--model-host",
            "llm.internal",
            "--capture-unknown",
            "reads",
            "--no-proxy",
            "db.local",
        ]
    )
    passed = _passed_on(args)
    argv = run_all.run_argv(
        tmp_path / "s.yaml", tmp_path / "a.yaml", state=tmp_path, judge=True, seed=3, passed_on=passed, command=["x"]
    )
    assert argv[3:] == [
        "run",
        str(tmp_path / "s.yaml"),
        "--agent",
        str(tmp_path / "a.yaml"),
        "--state",
        str(tmp_path),
        "--json",
        "--judge",
        "--seed",
        "3",
        "--no-proxy",
        "db.local",
        "--tunnel-model-calls",
        "--model-host",
        "llm.internal",
        "--capture-unknown=reads",
        "--",
        "x",
    ]
    recorded = _passed_on(_parser().parse_anywhere(["run-all", str(tmp_path), "--agent", "agent.yaml"]))
    assert "--tunnel-model-calls" not in recorded and "--record-model-calls" not in recorded, "recording is the default"


def test_the_wake_and_push_waits_default_to_room_for_a_models_turn() -> None:
    from datetime import timedelta

    from minutehand.domain.agent import Marked
    from minutehand.domain.people import InboundTarget

    assert Reported(wake_url="http://a/w", report_url="http://a/r").wake_timeout == timedelta(minutes=10)
    assert Marked(wake_url="http://a/w").wake_timeout == timedelta(minutes=10)
    assert InboundTarget(provider="slack", url="http://a/e").push_timeout == timedelta(minutes=5)


def test_an_agent_files_instructions_are_read_when_it_is_and_name_their_file_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its work file's words are kept with the agent as read, so the run keeps what the agent was told; the file may
    be the one the agent's own environment names, and one named by an unset variable with no default is refused."""
    (tmp_path / "work.json").write_text('["ask sam"]', encoding="utf-8")
    (tmp_path / "team.json").write_text('["ask priya"]', encoding="utf-8")
    agent = tmp_path / "agent.yaml"
    wakes = "wakes: [{kind: reported, wake_url: 'http://a.test/w', report_url: 'http://a.test/r'}]\n"
    agent.write_text("name: a\n" + wakes + 'instructions: [{file: "${AGENT_WORK:-work.json}"}]\n', encoding="utf-8")
    monkeypatch.delenv("AGENT_WORK", raising=False)
    assert [i.text for i in load_agent(agent).instructions] == ['["ask sam"]']
    monkeypatch.setenv("AGENT_WORK", "team.json")
    assert [i.text for i in load_agent(agent).instructions] == ['["ask priya"]']
    agent.write_text("name: a\n" + wakes + 'instructions: [{file: "${NOT_SET_ANYWHERE}"}]\n', encoding="utf-8")
    with pytest.raises(FileRefused, match="NOT_SET_ANYWHERE"):
        load_agent(agent)
