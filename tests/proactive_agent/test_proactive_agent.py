"""The proactive agent (examples/proactive_agent) in its two worlds, through the installed command, its wake decider on
Pydantic AI's test model (AGENT_MODEL=test), so its guard alone decides each wake."""

from __future__ import annotations

import os
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


def play(world: str, tmp_path: Path) -> tuple[VerdictKind, list[tuple[str, str, str]]]:
    """The verdict, and each message the agent sent: (to, when in UTC, its words)."""
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")} | people_environment()
    state = tmp_path / "state"
    done = subprocess.run(
        [str(MINUTEHAND), "run", f"worlds/{world}.yaml", "--agent", "agent.yaml", "--state", str(state), "--",
         sys.executable, str(EXAMPLE / "agent.py")],
        cwd=EXAMPLE, env=env | {"AGENT_MODEL": "test"}, capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr[-3000:]
    run_id = done.stdout.split()[1].rstrip(":")
    with session.reading(state, run_id) as kept:
        said = [
            (e.after.recipient_emails[0].split("@")[0], f"{e.sim_time:%a %H:%M}", e.after.text)
            for e in kept.events()
            if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot) and e.after.recipient_emails
        ]
    return session.load(state, run_id).result.verdict.kind, said


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
