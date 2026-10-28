"""`minutehand export`: one run as a record a reader outside Minutehand tells its story from (docs/exporting.md)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.export.record import Exported, export
from minutehand.adapters.providers.slack import state as slack_state
from minutehand.checks.runner import evaluate_run
from minutehand.cli import main
from minutehand.domain.presentation import (
    ApprovalStatus,
    RecordChannel,
    RecordDirect,
    RecordGroup,
    RecordMessage,
)
from minutehand.domain.run import RunRecord, StopReason
from minutehand.session import RECORD, RESULT, RUNS, RunRefused
from tests.export.run import (
    ANSWER,
    DRAFT,
    GOAL,
    MADE,
    OLD_POST,
    OLD_REPLY,
    T0,
    TEO_ANSWERS,
    TO_GROUP,
    TO_TEO,
    Written,
    scenario,
    write_run,
)


@pytest.fixture
def run(tmp_path: Path) -> Written:
    return write_run(tmp_path)


@pytest.fixture
def exported(run: Written) -> Exported:
    return export(run.state, run.run_id)


def _message(exported: Exported, text: str) -> RecordMessage:
    return next(m for m in exported.record.messages if m.text == text)


def test_a_channel_is_named_and_a_dm_or_group_says_who_is_in_it(exported: Exported) -> None:
    by_kind = {type(c): c for c in exported.record.conversations}
    channel, direct, group = by_kind[RecordChannel], by_kind[RecordDirect], by_kind[RecordGroup]
    assert isinstance(channel, RecordChannel) and channel.name == "release"
    assert {c.person for c in exported.record.conversations if isinstance(c, RecordDirect)} == {"mira", "teo"}
    assert isinstance(group, RecordGroup) and sorted(group.people) == ["teo", "una"]
    assert isinstance(direct, RecordDirect)


def test_each_message_names_its_author_whoever_wrote_it(exported: Exported) -> None:
    agent = exported.record.agent.key
    assert _message(exported, GOAL).by == "mira"  # a person in their DM: no reply landed as it
    assert _message(exported, OLD_POST).by == "mira"  # seeded before the run
    assert _message(exported, OLD_REPLY).by == "una"
    assert _message(exported, TEO_ANSWERS).by == "teo"
    assert {_message(exported, t).by for t in (ANSWER, TO_TEO, TO_GROUP)} == {agent}


def test_a_message_already_there_is_dated_when_it_was_posted_and_its_thread_kept(exported: Exported) -> None:
    root, reply = _message(exported, OLD_POST), _message(exported, OLD_REPLY)
    assert root.at == T0 - timedelta(days=2) and root.wake == 0
    assert reply.at == T0 - timedelta(days=2) + timedelta(hours=1)
    assert reply.reply_to == root.id and root.reply_to is None


def test_an_edit_keeps_the_last_words_and_when_they_changed(run: Written, exported: Exported) -> None:
    answer = next(m for m in exported.record.messages if m.id == run.answer)
    assert answer.text == ANSWER
    assert answer.at == T0 and answer.edited and answer.edited_until == run.answer_edited_at
    goal = _message(exported, GOAL)
    assert not goal.edited and goal.edited_until is None


def test_wakes_run_from_one_to_the_next_and_the_last_to_the_run_end(run: Written, exported: Exported) -> None:
    wakes = exported.record.wakes
    assert [w.n for w in wakes] == [1, 2]
    assert wakes[0].ends == wakes[1].starts
    assert wakes[1].ends == run.ended == exported.record.run.end
    assert _message(exported, TEO_ANSWERS).wake == 2


def test_a_finished_run_ends_where_its_record_says(run: Written) -> None:
    ended = T0 + timedelta(days=5)
    record = RunRecord(
        run_id=run.run_id,
        scenario="release_checklist",
        seed=0,
        started_at=T0,
        ended_at=ended,
        wall_seconds=1,
        stop=StopReason.WINDOW_ENDED,
        wakes=[],
    )
    directory = run.state / RUNS / run.run_id
    with session.reading(run.state, run.run_id) as world:
        result = evaluate_run(scenario(), world.events(), [], world.replies(), stop=record.stop, ended=ended)
    (directory / RECORD).write_text(record.model_dump_json(), encoding="utf-8")
    (directory / RESULT).write_text(result.model_dump_json(), encoding="utf-8")
    assert export(run.state, run.run_id).record.run.end == ended


def test_what_was_written_down_comes_with_its_key_place_and_cells(exported: Exported) -> None:
    items = {i.title: i for i in exported.record.items}
    ticket = items["Migrate the orders table"]
    assert ticket.kind == "Ticket" and ticket.ref == "PLATFORM-1" and ticket.tool == "Jira"
    assert ticket.place == "Platform" and ticket.state == "open" and ticket.assignee == "teo"
    sheet = items["Release checklist"]
    assert sheet.kind == "Spreadsheet" and sheet.place == "Delivery" and sheet.tool == "Google Workspace"
    assert sheet.rows == [["Step", "Owner"], ["Migration", "Teo"], ["Notes", "Una"]]
    assert MADE not in items  # made during the run: something the agent did, not something already there


def test_an_item_for_approval_is_dated_when_first_seen_and_says_how_it_was_decided(exported: Exported) -> None:
    (approval,) = exported.record.approvals
    assert approval.person == "mira" and approval.summary == DRAFT
    assert approval.status is ApprovalStatus.APPROVED and approval.decision == "approve"
    assert approval.at == T0 + timedelta(minutes=2)


def test_what_the_agent_made_itself_is_an_action(exported: Exported) -> None:
    (made,) = exported.record.actions
    assert made.did == "Created a document" and made.title == MADE and made.place == "Delivery" and made.wake == 1


def test_the_run_is_shown_on_its_owners_clock_and_people_by_their_slack_ids(exported: Exported) -> None:
    record = exported.record
    assert record.run.timezone == "Europe/Lisbon" and record.run.chat == "Slack"
    assert {p.key: p.chat_id for p in record.people}["teo"] == slack_state.user_id("teo")
    assert {p.key: p.title for p in record.people}["una"] is None
    assert record.agent.chat_id == "U0AGENTBOT" and record.agent.name == "agent"  # no agent file: the bot's name


def test_the_file_is_in_the_readers_field_names(
    run: Written, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "record.json"
    assert main(["export", run.run_id[:8], "--out", str(out), "--state", str(run.state)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["schema"] == "minutehand-replay-record/1"
    assert {"ranOn", "chatId"} <= set(written["run"]) | set(written["agent"])
    answer = next(m for m in written["messages"] if m["text"] == ANSWER)
    assert "editedUntil" in answer and "replyTo" not in answer
    assert {"from", "to"} == set(written["wakes"][0]) - {"n"}
    assert written["approvals"][0]["for"] == "mira"
    assert "with" in next(c for c in written["conversations"] if c["kind"] == "direct")
    assert "7 messages in 4 conversations" in capsys.readouterr().err


def test_a_run_whose_people_talk_elsewhere_is_refused(run: Written) -> None:
    from minutehand.adapters.store.sqlite import SqliteStore
    from minutehand.application.run_clock import RunClock
    from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation
    from minutehand.session import WORLD

    store = SqliteStore(run.state / RUNS / run.run_id / WORLD, run.run_id, RunClock(T0 + timedelta(hours=2)))
    store.apply(
        Change(
            entity=EntityRef(provider="microsoft", kind=EntityKind.MESSAGE, external_id="m-1"),
            operation=Operation.CREATE,
            actor=Actor.PERSON,
            body="{}",
            after=MessageSnapshot(text="hello", channel="chat-1"),
        )
    )
    store.close()
    with pytest.raises(RunRefused, match="microsoft"):
        export(run.state, run.run_id)
