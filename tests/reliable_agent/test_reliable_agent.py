"""The reliable agent (examples/reliable_agent) in each of its worlds, with a careful model and with a reckless one
that tries the trial's mistakes on purpose: ordering whatever is offered, and a per-unit price nobody gave it in every
message. What reaches the world is the same either way; the agent's own record of what its structure stopped says
why. Each run is the installed command against the example's own files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from examples.reliable_agent.offline_model import agent_model
from minutehand import session
from minutehand.application.memory import memory_of
from minutehand.domain.run import VerdictKind
from minutehand.domain.world import Actor, MessageSnapshot, StoredSnapshot, TransitionSnapshot
from tests.support.people import people_environment

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "reliable_agent"
MINUTEHAND = Path(sys.executable).parent / "minutehand"
OWEN, SAM = "owen@example.com", "sam@example.com"


@dataclass
class Played:
    verdict: VerdictKind
    failed: list[str]
    said: list[tuple[str, str]]  # (to, text) of every message the agent sent
    moves: list[tuple[str, str]]  # (name, to_state) of every move on the approval request
    orders: int
    blocked: list[str]


def play(world: str, tmp_path: Path, *, reckless: bool, flaky: bool = False, wordy: bool = False) -> Played:
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")} | people_environment()
    state = tmp_path / "state"
    with agent_model(reckless=reckless, flaky=flaky, wordy=wordy) as url:
        done = subprocess.run(
            [str(MINUTEHAND), "run", f"worlds/{world}.yaml", "--agent", "agent.yaml", "--state", str(state), "--",
             sys.executable, str(EXAMPLE / "agent.py")],
            cwd=EXAMPLE, env=env | {"AGENT_MODEL_BASE_URL": url, "AGENT_MODEL_API_KEY": "test"},
            capture_output=True, text=True, timeout=600,
        )  # fmt: skip
    assert done.returncode in (0, 1), done.stdout + done.stderr[-3000:]
    run_id = done.stdout.split()[1].rstrip(":")
    outcome = session.load(state, run_id)
    with session.reading(state, run_id) as kept:
        events = kept.events()
    said = [
        (e.after.recipient_emails[0], e.after.text)
        for e in events
        if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot) and e.after.recipient_emails
    ]
    moves = [(e.after.name, e.after.to_state) for e in events if isinstance(e.after, TransitionSnapshot)]
    orders = sum(1 for e in events if isinstance(e.after, StoredSnapshot) and e.actor is Actor.AGENT)
    remembered = json.loads(memory_of(events)[("default", "state")])
    return Played(
        verdict=outcome.result.verdict.kind,
        failed=[f.check for f in outcome.result.findings if f.kind.value == "fail"],
        said=said,
        moves=moves,
        orders=orders,
        blocked=remembered["blocked"],
    )


MOODS = pytest.mark.parametrize("reckless", [False, True], ids=["careful", "reckless"])


@MOODS
def test_approved_it_asks_once_files_waits_orders_once_approved_and_tells_the_requester(
    tmp_path: Path, reckless: bool
) -> None:
    played = play("approved", tmp_path, reckless=reckless)

    assert played.verdict is VerdictKind.PASSED and played.failed == []
    assert [to for to, _ in played.said] == [SAM, OWEN], "one question to Sam, one outcome to Owen, nothing more"
    assert [m for m in played.moves if m[0] != "reply"] == [("create", "pending"), ("approve", "approved")]
    assert played.orders == 1
    assert not [text for _, text in played.said if "1,200" in text], "no price nobody gave it reached anyone"
    if reckless:
        assert "not on the menu: 'order'" in played.blocked, "it tried to order early, and was offered no such move"
        assert any(b.startswith("plain words:") for b in played.blocked), "its invented price was never sent"


@MOODS
def test_asked_back_for_what_it_does_not_hold_it_asks_the_requester_and_resubmits_their_words(
    tmp_path: Path, reckless: bool
) -> None:
    played = play("asks_back", tmp_path, reckless=reckless)

    assert played.verdict is VerdictKind.PASSED and played.failed == []
    assert [m for m in played.moves if m[0] != "reply"] == [
        ("create", "pending"),
        ("ask_back", "needs_info"),
        ("resubmit", "pending"),
        ("approve", "approved"),
    ]
    assert [to for to, _ in played.said] == [SAM, OWEN, OWEN], "Sam, then Owen for the quote, then the outcome"
    assert played.orders == 1
    assert not [text for _, text in played.said if "1,200" in text]


@MOODS
def test_when_finance_never_answers_it_chases_twice_a_working_day_apart_then_tells_the_requester(
    tmp_path: Path, reckless: bool
) -> None:
    played = play("finance_quiet", tmp_path, reckless=reckless)

    assert played.verdict is VerdictKind.PASSED and played.failed == [], "no duplicate, no chase before it was due"
    assert [to for to, _ in played.said] == [SAM, SAM, SAM, OWEN], "the ask, two follow-ups, then Owen told"
    assert len({text for to, text in played.said if to == SAM}) == 3, "each follow-up says something new"
    assert played.moves == [] and played.orders == 0, "nothing filed without a cost centre"


def test_a_model_down_for_a_while_ends_wakes_cleanly_and_the_agent_tries_again_until_it_is_back(
    tmp_path: Path,
) -> None:
    played = play("approved", tmp_path, reckless=False, flaky=True)

    assert played.verdict is VerdictKind.PASSED and played.failed == [], "the run is not stopped by its model failing"
    assert [to for to, _ in played.said] == [SAM, OWEN] and played.orders == 1, "the same outcome, a little later"
    assert any(b.startswith("model or service failed") for b in played.blocked)


def test_a_model_that_names_a_move_by_its_description_still_moves_the_work_on(tmp_path: Path) -> None:
    """A real model answered with the move's description instead of its name, and the agent, refusing it, stalled
    for good: the menu now reads a description as the one move it describes, and a wake that leaves a move open plans
    its return."""
    played = play("approved", tmp_path, reckless=False, wordy=True)

    assert played.verdict is VerdictKind.PASSED
    assert [to for to, _ in played.said] == [SAM, OWEN] and played.orders == 1
