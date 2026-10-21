"""The example agent's own logic (examples/proactive_agent/agent.py): when its follow-ups fall due, what it says, when
it stops, and that it carries no code for Minutehand. Its Slack is a client whose three calls are kept, not sent:
the agent's words are what is checked here, not Slack's; a whole run needs the sandbox (its README)."""

from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "proactive_agent"
T0 = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)  # a Monday


@pytest.fixture
def agent_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("proactive_agent_example", EXAMPLE / "agent.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Said:
    """The three Slack calls the agent makes, kept in order; users and channels named by the email asked for."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []

    def users_lookupByEmail(self, email: str) -> dict[str, dict[str, str]]:
        return {"user": {"id": f"U-{email}"}}

    def conversations_open(self, users: list[str]) -> dict[str, dict[str, str]]:
        return {"channel": {"id": f"D-{users[0][2:]}"}}

    def chat_postMessage(self, channel: str, text: str) -> None:
        self.posted.append((channel[2:], text))


def _agent(agent_module: ModuleType, tmp_path: Path) -> tuple[Any, _Said]:
    said = _Said()
    work = agent_module.read_work(EXAMPLE / "work.json")
    return agent_module.Agent(work, agent_module.Memory(tmp_path / "agent.db"), said), said


def test_it_asks_then_follows_up_twice_a_working_day_apart_then_tells_the_asker_none_came(
    agent_module: ModuleType, tmp_path: Path
) -> None:
    agent, said = _agent(agent_module, tmp_path)
    assert agent.act(T0) == T0 + timedelta(days=1)  # asked; the follow-up falls due a working day on
    assert agent.act(T0 + timedelta(hours=3)) == T0 + timedelta(days=1)  # woken early (nothing due): no new words
    assert agent.act(T0 + timedelta(days=1)) == T0 + timedelta(days=2)
    assert agent.act(T0 + timedelta(days=2)) == T0 + timedelta(days=3)
    assert agent.act(T0 + timedelta(days=3)) is None  # gave up: nothing left open, nothing to wait for
    assert [(to, text.split(":")[0]) for to, text in said.posted] == [
        ("sam@example.com", "Could you tell me the cost centre for PO-7731 (40 laptops for the new starters)?"),
        ("sam@example.com", "Following up (1 of 2) on my question of 2026-08-24"),
        ("sam@example.com", "Following up (2 of 2) on my question of 2026-08-24"),
        ("owen@example.com", 'No answer from sam@example.com to "Could you tell me the cost centre for PO-7731 (40 '
                             'laptops for the new starters)?" after 2 follow-ups.'),
    ]  # fmt: skip


def test_an_answer_is_passed_on_at_once_and_nothing_is_left_open(agent_module: ModuleType, tmp_path: Path) -> None:
    agent, said = _agent(agent_module, tmp_path)
    agent.act(T0)
    agent.heard("sam@example.com", "CC-4471.")
    assert agent.act(T0 + timedelta(hours=2)) is None
    assert said.posted[-1] == (
        "owen@example.com",
        'sam@example.com answered "Could you tell me the cost centre for PO-7731 (40 laptops for the new starters)?": '
        "CC-4471.",
    )


def test_a_follow_up_due_out_of_hours_waits_for_the_next_working_morning(agent_module: ModuleType) -> None:
    in_hours = agent_module.in_hours
    friday_evening = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)
    assert in_hours(friday_evening) == datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
    assert in_hours(datetime(2026, 8, 25, 7, 0, tzinfo=UTC)) == datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    assert in_hours(datetime(2026, 8, 25, 11, 0, tzinfo=UTC)) == datetime(2026, 8, 25, 11, 0, tzinfo=UTC)


def test_the_agent_imports_nothing_of_minutehands_and_serves_no_endpoint_for_it() -> None:
    tree = ast.parse((EXAMPLE / "agent.py").read_text(encoding="utf-8"))
    imported = [
        name
        for node in ast.walk(tree)
        for name in (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    ]
    assert not any(name.startswith("minutehand") for name in imported), imported
    source = (EXAMPLE / "agent.py").read_text(encoding="utf-8")
    assert "/wake" not in source and "/report" not in source
