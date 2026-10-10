"""The proactive agent (examples/proactive_agent) in its two worlds, through the installed command, its wake decider on
Pydantic AI's test model (AGENT_MODEL=test), so its guard alone decides each wake."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from minutehand import session
from minutehand.domain.run import VerdictKind
from minutehand.domain.world import Actor, MessageSnapshot
from tests.support.people import people_environment

pytestmark = pytest.mark.recipes  # its wake decider is Pydantic AI, the `recipes` dependency group's

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "proactive_agent"
MINUTEHAND = Path(sys.executable).parent / "minutehand"


ENV = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")} | {"AGENT_MODEL": "test"}


def _minutehand(*words: str, cwd: Path = EXAMPLE) -> str:
    """The run's id, from `minutehand run` or `fork` as a user types it."""
    done = subprocess.run(
        [str(MINUTEHAND), *words], cwd=cwd, env=ENV | people_environment(), capture_output=True, text=True, timeout=600
    )
    assert done.returncode == 0, done.stdout + done.stderr[-3000:]
    return done.stdout.split()[1].rstrip(":")


def _said(state: Path, run_id: str) -> list[tuple[str, str, str]]:
    with session.reading(state, run_id) as kept:
        said = [
            (e.after.recipient_emails[0].split("@")[0], f"{e.sim_time:%a %H:%M}", e.after.text)
            for e in kept.events()
            if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot) and e.after.recipient_emails
        ]
    return said


def play(world: str, tmp_path: Path) -> tuple[VerdictKind, list[tuple[str, str, str]]]:
    """The verdict, and each message the agent sent: (to, when in UTC, its words)."""
    state = tmp_path / "state"
    run_id = _minutehand(
        "run", f"worlds/{world}.yaml", "--agent", "agent.yaml", "--state", str(state), "--", sys.executable, "agent.py"
    )
    return session.load(state, run_id).result.verdict.kind, _said(state, run_id)


def test_sam_answers_and_owen_is_told_his_answer(tmp_path: Path) -> None:
    verdict, said = play("answers", tmp_path)

    assert verdict is VerdictKind.PASSED
    assert [to for to, _, _ in said] == ["sam", "owen"], "one question, one report: no chase before it was due"
    assert "CC-4410" in said[1][2]


def test_sam_never_answers_and_it_follows_up_twice_a_working_day_apart_then_tells_owen(tmp_path: Path) -> None:
    verdict, said = play("quiet", tmp_path)

    assert verdict is VerdictKind.PASSED
    assert [(to, when) for to, when, _ in said] == [
        ("sam", "Mon 09:00"),
        ("sam", "Tue 09:00"),
        ("sam", "Wed 09:00"),
        ("owen", "Thu 09:00"),
    ], "each wake the guard's moment: a working day after the last word to Sam, then nothing more"


def test_its_code_changed_and_forked_from_a_checkpoint_it_plays_on_from_there(tmp_path: Path) -> None:
    """The loop of working on an agent: a run, a change to its code, and a fork from the moment after Sam was asked,
    started with the new code where the run's agent file reaches it (`{run.port}`), from its memory then, and the
    world as it stood. The run it forked from is as it was."""
    state = tmp_path / "state"
    run_id = _minutehand(
        "run", "worlds/quiet.yaml", "--agent", "agent.yaml", "--state", str(state), "--", sys.executable, "agent.py"
    )
    before = _said(state, run_id)
    changed = tmp_path / "changed"
    shutil.copytree(EXAMPLE, changed, ignore=shutil.ignore_patterns("__pycache__"))
    code = (changed / "agent.py").read_text(encoding="utf-8")
    (changed / "agent.py").write_text(code.replace("FOLLOW_UPS = 2", "FOLLOW_UPS = 1"), encoding="utf-8")
    (tmp_path / "none.yaml").write_text("overrides: []\n", encoding="utf-8")
    asked = next(p for p in session.fork_points(state, run_id) if p.wake == 1)

    forked = _minutehand(
        "fork", run_id, "--at", str(asked.seq), "--changes", str(tmp_path / "none.yaml"), "--state", str(state),
        "--", sys.executable, "agent.py", cwd=changed,
    )  # fmt: skip

    assert [(to, when) for to, when, _ in _said(state, forked)] == [
        ("sam", "Mon 09:00"),
        ("sam", "Tue 09:00"),
        ("owen", "Wed 09:00"),
    ], "Monday's question is the run's; then the changed code: one follow-up, and Owen told"
    assert _said(state, run_id) == before
