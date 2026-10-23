"""The example agent's own logic (examples/proactive_agent/agent.py): what it says when it asks, what it books with
Slack for later and when, what it deletes when an answer comes, and that it carries no code for Minutehand. Its Slack
is a client whose calls are kept, not sent: the agent's words and bookings are what is checked here, not Slack's; a
whole run needs the sandbox (its README)."""

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
QUESTION = "Could you tell me the cost centre for PO-7731 (40 laptops for the new starters)?"


@pytest.fixture
def agent_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("proactive_agent_example", EXAMPLE / "agent.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Said:
    """The Slack calls the agent makes, kept in order; users and channels named by the email asked for."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []
        self.scheduled: dict[str, tuple[str, datetime, str]] = {}
        self.deleted: list[str] = []

    def users_lookupByEmail(self, email: str) -> dict[str, dict[str, str]]:
        return {"user": {"id": f"U-{email}"}}

    def conversations_open(self, users: list[str]) -> dict[str, dict[str, str]]:
        return {"channel": {"id": f"D-{users[0][2:]}"}}

    def chat_postMessage(self, channel: str, text: str) -> None:
        self.posted.append((channel[2:], text))

    def chat_scheduleMessage(self, channel: str, post_at: int, text: str) -> dict[str, str]:
        ident = f"Q{len(self.scheduled) + 1}"
        self.scheduled[ident] = (channel[2:], datetime.fromtimestamp(post_at, UTC), text)
        return {"scheduled_message_id": ident}

    def chat_deleteScheduledMessage(self, channel: str, scheduled_message_id: str) -> None:
        assert self.scheduled[scheduled_message_id][0] == channel[2:]
        self.deleted.append(scheduled_message_id)


def _agent(agent_module: ModuleType, tmp_path: Path) -> tuple[Any, _Said]:
    said = _Said()
    work = agent_module.read_work(EXAMPLE / "work.json")
    return agent_module.Agent(work, agent_module.Memory(tmp_path / "agent.db"), said), said


def test_it_asks_and_books_two_follow_ups_a_working_day_apart_then_word_to_the_asker_that_none_came(
    agent_module: ModuleType, tmp_path: Path
) -> None:
    agent, said = _agent(agent_module, tmp_path)
    agent.act(T0)
    assert said.posted == [("sam@example.com", QUESTION)]
    assert [(to, at - T0, text) for to, at, text in said.scheduled.values()] == [
        ("sam@example.com", timedelta(days=1), f"Following up (1 of 2) on my question of 2026-08-24: {QUESTION}"),
        ("sam@example.com", timedelta(days=2), f"Following up (2 of 2) on my question of 2026-08-24: {QUESTION}"),
        ("owen@example.com", timedelta(days=3), f'No answer from sam@example.com to "{QUESTION}" after 2 follow-ups.'),
    ]  # fmt: skip
    agent.act(T0 + timedelta(hours=3))  # acting again with nothing new says and books nothing
    assert len(said.posted) == 1 and len(said.scheduled) == 3 and said.deleted == []


def test_an_answer_is_passed_on_at_once_and_what_was_not_yet_posted_is_deleted(
    agent_module: ModuleType, tmp_path: Path
) -> None:
    agent, said = _agent(agent_module, tmp_path)
    agent.act(T0)
    agent.memory.heard("sam@example.com", "CC-4471.")
    agent.act(T0 + timedelta(days=1, hours=2))  # after the first follow-up was posted
    assert said.posted[-1] == ("owen@example.com", f'sam@example.com answered "{QUESTION}": CC-4471.')
    assert said.deleted == ["Q2", "Q3"]  # the second follow-up and the word that none came; the first is posted
    agent.act(T0 + timedelta(days=2))
    assert len(said.posted) == 2 and said.deleted == ["Q2", "Q3"]  # ended: nothing more is said or deleted


def test_a_booking_too_close_to_its_moment_to_delete_is_left_to_be_posted(
    agent_module: ModuleType, tmp_path: Path
) -> None:
    agent, said = _agent(agent_module, tmp_path)
    agent.act(T0)
    agent.memory.heard("sam@example.com", "CC-4471.")
    agent.act(T0 + timedelta(days=1) - timedelta(seconds=30))
    assert said.deleted == ["Q2", "Q3"]


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
