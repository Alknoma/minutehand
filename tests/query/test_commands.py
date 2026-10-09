"""`minutehand query`, `trace` and `explain` on the command line, over the hand-written run and its fork."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from minutehand.cli import main
from tests.query.fixture import ANSWER, FOLLOW_UP, FORK_FOLLOW_UP, PRICES, QUESTION, TO_DANIA, TOLD, Fixture


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_query_prints_a_table(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run_cli(
        capsys, "query", run.run_id, "SELECT seq, text FROM messages WHERE is_ask = 1", "--state", str(run.state)
    )
    assert code == 0
    assert out.splitlines() == [
        "seq  text",
        "---  " + "-" * len(QUESTION),
        f"{run.asked}    {QUESTION}",
        f"{run.to_dania}    {TO_DANIA}",
        "(2 rows)",
    ]


def test_query_prints_json_and_csv(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    sql = "SELECT seq, from_person, text FROM messages WHERE is_reply = 1"
    _, out, _ = run_cli(capsys, "query", run.run_id, sql, "--format", "json", "--state", str(run.state))
    assert json.loads(out) == [{"seq": run.answered, "from_person": "sofia", "text": ANSWER}]
    _, out, _ = run_cli(capsys, "query", run.run_id, sql, "--format", "csv", "--state", str(run.state))
    assert list(csv.reader(io.StringIO(out))) == [["seq", "from_person", "text"], [str(run.answered), "sofia", ANSWER]]


def test_query_reads_a_fork_by_its_id_or_the_start_of_it(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    sql = "SELECT text FROM messages WHERE is_follow_up = 1"
    _, whole, _ = run_cli(capsys, "query", run.fork_id, sql, "--format", "json", "--state", str(run.state))
    _, start, _ = run_cli(capsys, "query", "fork", sql, "--format", "json", "--state", str(run.state))
    _, parent, _ = run_cli(capsys, "query", "run0", sql, "--format", "json", "--state", str(run.state))
    assert json.loads(whole) == json.loads(start) == [{"text": FORK_FOLLOW_UP}]
    assert json.loads(parent) == [{"text": FOLLOW_UP}]


def test_query_of_an_unknown_run_is_refused(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run_cli(capsys, "query", "nosuchrun", "SELECT 1", "--state", str(run.state))
    assert code == 2
    assert "no run nosuchrun" in err


def test_query_that_writes_is_refused(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run_cli(capsys, "query", run.run_id, "DELETE FROM messages", "--state", str(run.state))
    assert code == 2
    assert "read-only" in err


def test_query_exports_the_read_model_for_any_sqlite_client(
    run: Fixture, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "read.sqlite"
    code, out, _ = run_cli(capsys, "query", run.run_id, "--export", str(target), "--state", str(run.state))
    assert (code, out) == (0, f"wrote the read model of run {run.run_id} to {target}\n")
    plain = sqlite3.connect(target)
    try:
        assert plain.execute("SELECT text FROM messages WHERE seq = ?", (run.told,)).fetchone() == (TOLD,)
    finally:
        plain.close()
    code, _, err = run_cli(capsys, "query", run.run_id, "--export", str(target), "--state", str(run.state))
    assert code == 2 and "exists" in err


def test_query_costs_calls_from_a_prices_file(run: Fixture, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    prices = tmp_path / "prices.yaml"
    prices.write_text(yaml.safe_dump(PRICES.model_dump(mode="json")), encoding="utf-8")
    sql = "SELECT ROUND(SUM(cost), 5) AS cost FROM model_calls"
    _, out, _ = run_cli(
        capsys, "query", run.run_id, sql, "--format", "json", "--prices", str(prices), "--state", str(run.state)
    )
    assert json.loads(out) == [{"cost": 0.00532}]


def test_trace_lists_the_agents_acts_toward_a_person(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run_cli(capsys, "trace", run.run_id, "--person", "sofia", "--state", str(run.state))
    assert code == 0
    assert out.splitlines() == [
        f"run {run.run_id}: 2 action(s) of the agent's",
        f"#4    wake 1   2026-08-24 10:00  message    slack D0SOFIA -> sofia: {QUESTION}  (seq {run.asked})",
        f"#13   wake 2   2026-08-25 12:00  message    slack D0SOFIA -> sofia: {FOLLOW_UP}  (seq {run.follow_up})",
    ]


def test_trace_filters_and_answers_json(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run_cli(
        capsys, "trace", run.run_id, "--kind", "memory", "--wake", "3", "--json", "--state", str(run.state)
    )
    traced = json.loads(out)
    assert [a["seq"] for a in traced["actions"]] == [20, 22, run.late_write]
    _, out, _ = run_cli(
        capsys,
        "trace",
        run.run_id,
        "--from",
        "2026-08-25T12:00:00Z",
        "--to",
        "2026-08-25T13:00",
        "--json",
        "--state",
        str(run.state),
    )
    assert [a["kind"] for a in json.loads(out)["actions"]] == ["memory", "model_call", "message", "memory"]
    code, _, err = run_cli(capsys, "trace", run.run_id, "--person", "nobody", "--state", str(run.state))
    assert code == 2 and "its people are dania, owner, sofia" in err


def test_explain_gives_the_chain_before_and_after_an_ask(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run_cli(capsys, "explain", run.run_id, str(run.asked), "--json", "--state", str(run.state))
    assert code == 0
    found = json.loads(out)
    assert (found["wake"]["wake"], found["wake"]["reason"]) == (1, "start")
    assert [a["target"] for a in found["read_before"]] == ["default/asks/sofia", "D0SOFIA"]
    assert found["model_call"]["span_id"] == "a1a1a1a1a1a1a1a1"
    assert found["call"]["call_id"] == 2
    assert [r["text"] for r in found["replies"]] == [ANSWER]
    assert [m["seq"] for m in found["follow_ups"]] == [run.follow_up]
    assert [f["check_id"] for f in found["findings"]] == ["follows_up_when_due", "expectations"]


def test_explain_of_a_reply_names_what_woke_the_wake_and_the_ask_it_answers(
    run: Fixture, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run_cli(capsys, "explain", run.run_id, str(run.answered), "--state", str(run.state))
    assert code == 0
    assert out.splitlines() == [
        f"run {run.run_id}, seq {run.answered}: person create slack message D0SOFIA/1724601600.000100 at "
        "2026-08-25T16:00:00.000Z in wake 3",
        f"  said: {ANSWER}",
        "before",
        "  wake: 3 at 2026-08-25T16:00:00.000Z, reason person_replied, woken by reply",
        "  fell due: reply person_reply due 2026-08-25T16:00:00.000Z (dispatch 2)",
        f"  reply landed: sofia: {ANSWER} (reply 1, seq {run.answered})",
        f"  answers: seq {run.asked} 2026-08-24T10:00:00.000Z agent in slack D0SOFIA: {QUESTION}",
        "after",
        "  next: #15 memory default/asks/sofia: get asks/sofia",
        f"  next: #16 message D0OWNER: {TOLD}",
        '  next: #17 memory default/asks/sofia: put asks/sofia = {"status":"answered"}',
        '  next: #18 memory default/notes/last: put notes/last = "told the owner"',
    ]


def test_explain_of_a_follow_up_names_the_ask_it_chases(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run_cli(capsys, "explain", run.run_id, str(run.follow_up), "--json", "--state", str(run.state))
    found = json.loads(out)
    assert found["answers"]["seq"] == run.asked
    assert [d["source"] for d in found["woken_by"]] == ["reported"]
    assert found["message"]["joined_by"] == "content"


def test_explain_of_a_seq_beyond_the_log_is_refused(run: Fixture, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run_cli(capsys, "explain", run.run_id, "999", "--state", str(run.state))
    assert code == 2
    assert f"its seqs run from 1 to {run.late_write}" in err


@pytest.mark.parametrize(
    "order",
    [
        ["RUN", "SQL", "--format", "json", "--state", "STATE"],
        ["RUN", "--state", "STATE", "SQL", "--format", "json"],
        ["--state", "STATE", "RUN", "--format", "json", "SQL"],
        ["--format", "json", "--state", "STATE", "RUN", "SQL"],
    ],
)
def test_query_takes_its_run_and_sql_in_any_order_among_its_options(
    run: Fixture, capsys: pytest.CaptureFixture[str], order: list[str]
) -> None:
    sql = "SELECT seq FROM messages WHERE is_reply = 1"
    words = {"RUN": run.run_id, "SQL": sql, "STATE": str(run.state)}

    code, out, err = run_cli(capsys, "query", *(words.get(w, w) for w in order))

    assert code == 0, err
    assert json.loads(out) == [{"seq": run.answered}]
