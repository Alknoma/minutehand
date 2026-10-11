"""A fork whose agent's code changed says so: the code is part of what it changed, not a rerun of the same agent."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.forks import described
from minutehand.application.program import program_changes, program_of
from minutehand.domain.experiment import Fork
from minutehand.domain.scenario import Silent
from tests.e2e.support import AGENT, agent_under_test, scenario


async def test_a_fork_with_the_agents_code_changed_names_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    copy = tmp_path / "agent.py"
    shutil.copy(AGENT, copy)
    command = [str(copy) if word == str(AGENT) else word for word in launched.command]
    state = tmp_path / "state"
    [parent] = await session.play(scenario(Silent()), launched.agent, state=state, command=command)
    parent_id = parent.record.run_id
    point = next(p for p in session.fork_points(state, parent_id) if p.wake == 1)

    [same] = await session.fork(parent_id, Fork(parent_run=parent_id, at_seq=point.seq), state=state, command=command)
    copy.write_text(copy.read_text(encoding="utf-8") + "\n# the agent's code, changed\n", encoding="utf-8")
    [changed] = await session.fork(
        parent_id, Fork(parent_run=parent_id, at_seq=point.seq), state=state, command=command
    )

    unchanged = session.fork_account(state, same.record.run_id)
    assert unchanged is not None and unchanged.changes == [] and unchanged.summary == "Rerun, nothing changed"
    account = session.fork_account(state, changed.record.run_id)
    assert account is not None
    [said] = account.changes
    assert said.startswith("its program's code: ") and account.summary == said
    assert "what it changed: nothing" not in "\n".join(described(account))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_the_code_is_the_repositorys_commit_and_its_uncommitted_work_without_the_state_or_bytecode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "start")
    (root / "agent.py").write_text("print('one')\n")
    state = root / ".minutehand"
    first = program_of(["python", "agent.py"], root, state)
    assert first.commit is not None and first.uncommitted and first.code is not None

    (state / "runs").mkdir(parents=True)
    (state / "runs" / "world.db").write_bytes(b"a run")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "agent.cpython-312.pyc").write_bytes(b"bytecode")
    assert program_of(["python", "agent.py"], root, state).code == first.code

    (root / "agent.py").write_text("print('two')\n")
    second = program_of(["python", "agent.py"], root, state)
    [said] = program_changes(first, second)
    assert said.startswith("its program's code: ") and "uncommitted" in said

    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    assert program_of(["python", "-c", "pass"], elsewhere, state).code is None
    assert program_changes(None, second) == []
