"""The examples of docs/approvals.md: every file under examples/approvals loads with every load-time check, and the
inbox, chat-button, email and approval-service patterns play end to end against the example agent (examples/approvals/agent.py), its people
written by the recipes' fake model, reaching the outcome the guide states. So do the two forks, the samples over
decision timing, and the guide's queries over the runs they read."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.files import load_agent, load_fork, load_scenario
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import VerdictKind
from minutehand.domain.world import Actor, InboxItemSnapshot, MessageSnapshot, StoredSnapshot
from minutehand.run_all import Batch
from tests.ports import free_port
from tests.support.people import people_environment, people_model

pytestmark = pytest.mark.timeout(240)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "approvals"
AGENT = EXAMPLES / "agent.py"
MINUTEHAND = Path(sys.executable).parent / "minutehand"
PORT = "127.0.0.1:8720"
FORKS = {"flip_to_approve.yaml", "believes_approved.yaml"}

OWEN, NADIA, MARTA = "owen@example.com", "nadia@example.com", "marta@example.com"


# -- the files and the guide --------------------------------------------------------------------------------------


def _example_files() -> list[Path]:
    return sorted(p for p in EXAMPLES.rglob("*.yaml") if p.name not in FORKS)


def _validate(*files: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    return subprocess.run(
        [str(MINUTEHAND), "validate", *map(str, files)], capture_output=True, text=True, env=env, timeout=120
    )


GROUPS = {
    # an agent file and the scenarios it plays: validated together, so each scenario's rules are read as a run
    # reads them (a person a rule names that the scenario lacks, an `assess_off` naming nothing, are refused)
    "inbox/agent.yaml": ["inbox/approved.yaml", "inbox/rejected.yaml", "inbox/never_decides.yaml",
                         "inbox/timing/decision_timing.yaml"],
    "chat/agent.yaml": ["chat/approved.yaml", "chat/rejected.yaml"],
    "chat/teams_agent.yaml": ["chat/approved.yaml", "chat/rejected.yaml"],
    "email/agent.yaml": ["email/rejected.yaml", "email/away.yaml", "email/budget_changes.yaml"],
    "service/agent.yaml": ["service/never_decides.yaml"],
    "service/slack_fallthrough.yaml": [],
    "": ["calendar/declined_invitation.yaml", "tracker/approved_by_ticket.yaml"],
}  # fmt: skip


def test_every_example_file_loads_with_every_load_time_check() -> None:
    grouped = {EXAMPLES / f for agent, scenarios in GROUPS.items() for f in [agent, *scenarios] if f}
    assert grouped == set(_example_files()), "every example file is in a group"
    for agent, scenarios in GROUPS.items():
        files = [EXAMPLES / f for f in [agent, *scenarios] if f]
        checked = _validate(*files)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert checked.stdout.count(": a valid ") == len(files), checked.stdout
    for name in FORKS:
        load_fork(EXAMPLES / "inbox" / name, parent_run="parent", at_seq=12)


def test_a_rule_naming_someone_the_scenario_lacks_is_refused_with_its_agent_file() -> None:
    """What the grouping above guards: the team's rules name the backup approver, whom the calendar scenario lacks."""
    checked = _validate(EXAMPLES / "inbox" / "agent.yaml", EXAMPLES / "calendar" / "declined_invitation.yaml")
    assert checked.returncode == 1
    assert "rule chases_at_most_twice names marta, who is not in the scenario" in checked.stdout + checked.stderr


# -- playing the examples -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Played:
    run_id: str
    verdict: VerdictKind
    failed: list[str]
    messages: list[tuple[str, str]]  # (recipient, text) of every message the agent sent
    orders: list[str]  # each order the agent placed, as stored
    requests: list[str]  # each request filed with the approval service, as stored
    items: list[tuple[str, str]]  # (person, status) of each item's last version in the agent's product


def _agent(folder: str, port: int) -> AgentUnderTest:
    written = load_agent(EXAMPLES / folder / "agent.yaml").model_dump_json()
    return AgentUnderTest.model_validate_json(written.replace(PORT, f"127.0.0.1:{port}"))


def _read(state: Path, run_id: str, verdict: VerdictKind, failed: list[str]) -> Played:
    with session.reading(state, run_id) as world:
        events = world.events()
    messages = [
        (", ".join(e.after.recipient_emails), e.after.text)
        for e in events
        if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
    ]
    stored = [e.after for e in events if isinstance(e.after, StoredSnapshot)]
    items: dict[str, tuple[str, str]] = {}
    for e in events:
        if isinstance(e.after, InboxItemSnapshot):
            items[e.after.item_id] = (e.after.person or "", e.after.status.value)
    return Played(
        run_id=run_id,
        verdict=verdict,
        failed=failed,
        messages=messages,
        orders=[s.item for s in stored if s.collection == "orders" and s.item is not None],
        requests=[s.item for s in stored if s.collection == "requests" and s.item is not None],
        items=list(items.values()),
    )


async def play(
    folder: str, scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, heedless: bool = False
) -> Played:
    port = free_port()
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.setenv("APPROVAL_VIA", {"chat": "slack"}.get(folder, folder))
    monkeypatch.setenv("AGENT_BEHAVIOUR", "heedless" if heedless else "careful")
    [outcome] = await session.play(
        load_scenario(EXAMPLES / folder / scenario),
        _agent(folder, port),
        state=tmp_path / "state",
        command=[sys.executable, str(AGENT)],
        model=people_model(),
        listen=session.Listen(receive_telemetry=False),
    )
    failed = [f.check for f in outcome.result.findings if f.kind is FindingKind.FAIL]
    return _read(tmp_path / "state", outcome.record.run_id, outcome.result.verdict.kind, failed)


def to(played: Played, email: str) -> list[str]:
    return [text for recipient, text in played.messages if recipient == email]


async def test_inbox_approved_orders_once_nadia_approves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    played = await play("inbox", "approved.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.PASSED, played.failed
    assert len(played.orders) == 1 and '"approval": "apr-1"' in played.orders[0]
    assert played.items == [("nadia", "decided")]
    assert any("Nadia approved PO-7731" in t for t in to(played, OWEN)), played.messages


async def test_inbox_rejected_orders_nothing_and_tells_owen_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    played = await play("inbox", "rejected.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.PASSED, played.failed
    assert played.orders == []
    assert any("the q3 hardware budget is spent" in t.lower() for t in to(played, OWEN)), played.messages


async def test_inbox_rejected_fails_the_heedless_agent_that_orders_anyway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    played = await play("inbox", "rejected.yaml", tmp_path, monkeypatch, heedless=True)

    assert played.verdict is VerdictKind.FAILED
    assert played.failed == ["acts_only_once_approved", "never_orders_after_a_rejection"]
    assert len(played.orders) == 1


async def test_inbox_never_decided_is_chased_once_escalated_and_left_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    played = await play("inbox", "never_decides.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.UNFINISHED, played.failed
    assert played.orders == []
    assert sorted(played.items) == [("marta", "pending"), ("nadia", "pending")]
    assert [t.split("\n")[0] for t in to(played, NADIA)] == ["Reminder: PO-7731"]


async def test_chat_approved_by_a_button_press(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    played = await play("chat", "approved.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.PASSED, played.failed
    assert len(played.orders) == 1
    assert len(to(played, NADIA)) == 1 and "Could you approve PO-7731" in to(played, NADIA)[0]


async def test_chat_rejected_through_the_modal_tells_owen_her_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    played = await play("chat", "rejected.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.PASSED, played.failed
    assert played.orders == []
    assert any("The Q3 hardware budget is spent." in t for t in to(played, OWEN)), played.messages


async def test_chat_rejected_fails_the_heedless_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    played = await play("chat", "rejected.yaml", tmp_path, monkeypatch, heedless=True)

    assert played.verdict is VerdictKind.FAILED
    assert played.failed == ["never_orders_after_a_rejection"]


async def test_email_rejected_is_read_from_her_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    played = await play("email", "rejected.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.PASSED, played.failed
    assert played.orders == []
    assert any("the q3 hardware budget is spent" in t.lower() for t in to(played, OWEN)), played.messages


async def test_email_rejected_fails_the_heedless_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    played = await play("email", "rejected.yaml", tmp_path, monkeypatch, heedless=True)

    assert played.verdict is VerdictKind.FAILED
    assert played.failed == ["never_orders_after_a_rejection"]


async def test_email_approver_away_is_covered_by_her_delegate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    played = await play("email", "away.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.PASSED, played.failed
    assert len(to(played, NADIA)) == 1, "nothing more to Nadia once her automatic reply named Marta"
    assert len(to(played, MARTA)) == 1
    assert len(played.orders) == 1 and '"approval": "apr-2"' in played.orders[0]


async def test_service_request_stays_as_filed_and_nothing_is_ordered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    played = await play("service", "never_decides.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.PASSED, played.failed
    assert played.orders == []
    filed = [json.loads(r) for r in played.requests]
    assert [(r["approver"], r["status"]) for r in filed] == [(NADIA, "pending"), (MARTA, "pending")]
    assert [t.split("\n")[0] for t in to(played, NADIA)] == ["Reminder: PO-7731"]


# -- forks, samples and queries -----------------------------------------------------------------------------------


async def test_the_two_forks_of_a_rejected_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = await play("inbox", "rejected.yaml", tmp_path, monkeypatch)
    assert parent.verdict is VerdictKind.PASSED and parent.orders == []
    state = tmp_path / "state"
    before_the_decision = 13  # the checkpoint after wake 1: the request is up, Nadia has not decided

    outcomes: dict[str, Played] = {}
    for name in sorted(FORKS):
        changes = load_fork(EXAMPLES / "inbox" / name, parent_run=parent.run_id, at_seq=before_the_decision)
        [forked] = await session.fork(
            parent.run_id,
            changes,
            state=state,
            command=[sys.executable, str(AGENT)],
            model=people_model(),
            listen=session.Listen(receive_telemetry=False),
        )
        failed = [f.check for f in forked.result.findings if f.kind is FindingKind.FAIL]
        outcomes[name] = _read(state, forked.record.run_id, forked.result.verdict.kind, failed)

    flipped = outcomes["flip_to_approve.yaml"]
    assert flipped.verdict is VerdictKind.PASSED, flipped.failed
    assert len(flipped.orders) == 1 and any("Nadia approved" in t for t in to(flipped, OWEN))
    believed = outcomes["believes_approved.yaml"]
    assert believed.verdict is VerdictKind.FAILED
    assert "acts_only_once_approved" in believed.failed and len(believed.orders) == 1


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")} | people_environment()
    return subprocess.run([str(MINUTEHAND), *args], capture_output=True, text=True, env=env, timeout=200)


def test_samples_over_decision_timing_meet_their_expected_rate(tmp_path: Path) -> None:
    folder = tmp_path / "timing"
    folder.mkdir()
    (folder / "decision_timing.yaml").write_text((EXAMPLES / "inbox" / "timing" / "decision_timing.yaml").read_text())
    port = free_port()  # one port, runs one at a time: an inbox's URLs may not name {run.port} (the guide's gaps)
    agent = tmp_path / "agent.yaml"
    agent.write_text((EXAMPLES / "inbox" / "agent.yaml").read_text().replace(PORT, f"127.0.0.1:{port}"))
    command = ["env", f"PORT={port}", "APPROVAL_VIA=inbox", sys.executable, str(AGENT)]

    ran = _cli("run-all", str(folder), "--agent", str(agent), "--jobs", "1", "--samples", "4", "--seed", "7",
               "--state", str(tmp_path / "state"), "--json", "--", *command)  # fmt: skip

    assert ran.returncode == 0, ran.stdout + ran.stderr
    [timing] = Batch.model_validate_json(ran.stdout).played
    assert timing.matched and len(timing.samples) == 4
    assert {s.verdict for s in timing.samples} == {VerdictKind.PASSED}


def query_of(name: str) -> str:
    return (EXAMPLES / "queries" / name).read_text()


async def test_the_guides_queries_show_what_the_agent_did_around_each_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    heedless = await play("inbox", "rejected.yaml", tmp_path, monkeypatch, heedless=True)
    silent = await play("inbox", "never_decides.yaml", tmp_path, monkeypatch)

    after = _cli("query", heedless.run_id, query_of("after_a_rejection.sql"), "--state", str(state), "--format", "json")
    assert after.returncode == 0, after.stderr
    rows = json.loads(after.stdout)
    assert [r["kind"] for r in rows] == ["stored", "message"], rows
    assert '"approval": "apr-1"' in rows[0]["summary"]

    waiting = _cli("query", silent.run_id, query_of("while_undecided.sql"), "--state", str(state), "--format", "json")
    assert waiting.returncode == 0, waiting.stderr
    rows = json.loads(waiting.stdout)
    nadias = [r for r in rows if r["approver"] == "nadia"]
    assert [r["kind"] for r in nadias] == ["message", "message", "write", "message"], nadias
    assert nadias[0]["person"] == "nadia" and nadias[2]["summary"].startswith("create inbox_item apr-2")


async def test_a_budget_cut_while_her_answer_is_owed_does_not_change_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap the guide states: a fact change reaches what a person is asked after it, never an answer they owe."""
    played = await play("email", "budget_changes.yaml", tmp_path, monkeypatch)

    assert played.verdict is VerdictKind.PASSED, played.failed
    assert len(played.orders) == 1, "she approved the full order, from what she knew when asked"


def test_run_port_in_an_inbox_url_is_refused_by_run_all(tmp_path: Path) -> None:
    """A gap the guide states: `run-all` reads the agent file before it fills `{run.port}`, and an inbox's URLs may
    name only their own placeholders, so an agent with an inbox cannot take a port per run."""
    folder = tmp_path / "timing"
    folder.mkdir()
    (folder / "decision_timing.yaml").write_text((EXAMPLES / "inbox" / "timing" / "decision_timing.yaml").read_text())
    agent = tmp_path / "agent.yaml"
    agent.write_text((EXAMPLES / "inbox" / "agent.yaml").read_text().replace(PORT, "127.0.0.1:{run.port}"))

    ran = _cli("run-all", str(folder), "--agent", str(agent), "--state", str(tmp_path / "state"), "--", "true")

    assert ran.returncode == 2, ran.stdout + ran.stderr
    assert "the list's request's url names {run.port}" in ran.stderr
