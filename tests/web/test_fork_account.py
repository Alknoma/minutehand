"""A fork says what it is: where it split from its parent, what it changed, whether its restore was verified, and how
its outcome differs from its parent's, on every surface that shows it: the viewer's API, `minutehand findings` and
`runs`, and the MCP run listing.

Two forks are taken from one checkpoint of a forgetful agent's run in which Sofia never answers: in one she answers
after a day and a half, in the other the deadline moves three days later. Before, both read "Rerun from after wake 1"
and neither said what it had changed."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import cli, session
from minutehand.adapters.mcp.results import RunListing
from minutehand.adapters.web.responses import RunResponse, RunsResponse
from minutehand.application.forks import described
from minutehand.application.restore import Verification
from minutehand.domain.experiment import DeadlineShift, Fork, PersonChange
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import Silent
from minutehand.domain.world import Actor
from tests.e2e.support import ANSWER, T0, agent_under_test, answers, scenario
from tests.mcp.test_mcp_tools import call, connected
from tests.web.test_viewer_api import client, read


async def two_forks(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str, int]:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", hooks=True)
    [parent] = await session.play(scenario(Silent()), launched.agent, state=state, command=launched.command)
    point = next(p for p in session.fork_points(state, parent.record.run_id) if p.wake == 1)
    answering = Fork(
        parent_run=parent.record.run_id,
        at_seq=point.seq,
        overrides=[PersonChange(person="sofia", reply=answers(after=timedelta(hours=36)))],
    )
    later = Fork(parent_run=parent.record.run_id, at_seq=point.seq, overrides=[DeadlineShift(by=timedelta(days=3))])
    [one] = await session.fork(parent.record.run_id, answering, state=state, command=launched.command)
    [two] = await session.fork(parent.record.run_id, later, state=state, command=launched.command)
    return parent.record.run_id, one.record.run_id, two.record.run_id, point.seq


async def test_a_fork_says_where_it_split_what_it_changed_how_it_was_restored_and_how_it_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    parent, answered, later, at_seq = await two_forks(state, tmp_path, monkeypatch)

    async with client(state) as c:
        listed = await read(c, "/api/runs", RunsResponse)
        one = await read(c, f"/api/runs/{answered}", RunResponse)
        two = await read(c, f"/api/runs/{later}", RunResponse)
        root = await read(c, f"/api/runs/{parent}", RunResponse)

    by_id = {r.run_id: r for r in listed.runs}
    assert by_id[parent].changed is None and root.fork is None
    assert by_id[answered].changed == "Sofia Romano answers with 1 scripted reply after 36 hours"
    assert by_id[later].changed == "deadline +3.0 days"

    fork = one.fork
    assert fork is not None
    assert (fork.parent_run, fork.at_seq, fork.after_wake) == (parent, at_seq, 1)
    assert fork.at == next(w.sim_time for w in root.record.wakes if w.index == 1)  # type: ignore[union-attr]
    [changed] = fork.changes
    assert "Sofia" in changed and "after 36 hours" in changed and "before, never answers" in changed
    assert fork.restore is not None and fork.restore.verified and Verification.REPORT in fork.restore.by
    outcome = fork.outcome
    assert outcome is not None
    assert outcome.parent_verdict.kind is VerdictKind.FAILED and outcome.verdict_changed
    assert any(d.label == "expectations met" for d in outcome.scorecard)
    assert outcome.findings_lost and not any(f.check == "no_follow_up" for f in outcome.findings_gained)
    split = outcome.first_divergence
    assert split is not None and split.fork is not None
    assert ANSWER in split.fork.words and "Sofia" not in (split.parent.words if split.parent else "")

    assert two.fork is not None and two.fork.outcome is not None
    deadline = T0 + timedelta(days=14)
    [moved] = two.fork.changes
    assert f"{deadline:%Y-%m-%d %H:%M}" in moved and f"{deadline + timedelta(days=3):%Y-%m-%d %H:%M}" in moved

    assert cli.main(["findings", answered, "--state", str(state)]) == 0
    said = capsys.readouterr().out
    assert f"forked from {parent} at seq {at_seq}: after wake 1" in said
    assert "Sofia Romano (sofia) answers with 1 scripted reply" in said
    assert "its restore was verified: the agent's report" in said
    assert "verdict: failed -> passed" in said
    assert "the records part at the first change in the world after the split" in said and ANSWER in said

    cli.main(["runs", "--state", str(state)])
    listing = capsys.readouterr().out
    assert "after wake 1" in listing and "deadline +3.0 days" in listing and "Sofia Romano answers" in listing

    async with connected(state) as mcp:
        runs = await call(mcp, "list_runs", RunListing)
    accounts = {r.run_id: r.fork for r in runs.runs}
    assert accounts[parent] is None
    assert accounts[answered] == fork


def test_a_persons_reply_from_a_fork_is_the_first_change_in_the_world_that_differs() -> None:
    """The reply lands in the fork and not in its parent: it is named, with who made it."""
    from minutehand.application.forks import first_divergence
    from minutehand.domain.world import EntityKind, EntityRef, MessageSnapshot, Operation, WorldEvent

    def message(seq: int, text: str, actor: Actor, run: str) -> WorldEvent:
        return WorldEvent(
            seq=seq,
            run_id=run,
            wake=2,
            sim_time=T0 + timedelta(hours=seq),
            wall_time=T0,
            actor=actor,
            operation=Operation.CREATE,
            entity=EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=f"m{seq}-{text[:3]}"),
            after=MessageSnapshot(text=text, channel="D1"),
        )

    shared = message(5, "Hello", Actor.AGENT, "p")
    parent = [message(1, "before", Actor.AGENT, "p"), shared, message(6, "Nudge", Actor.AGENT, "p")]
    fork = [
        message(1, "before", Actor.AGENT, "c"),
        shared.model_copy(update={"run_id": "c"}),
        message(6, ANSWER, Actor.PERSON, "c"),
    ]
    split = first_divergence(parent, fork, 1, scenario(Silent()), scenario(Silent()))
    assert split is not None and split.shared == 1
    assert (
        split.parent is not None and "the agent sent a message" in split.parent.words and "Nudge" in split.parent.words
    )
    assert split.fork is not None and "a person wrote" in split.fork.words and ANSWER in split.fork.words
    assert first_divergence(parent, parent, 1, scenario(Silent()), scenario(Silent())) is None


def test_findings_are_paired_by_check_and_kind_so_a_changed_message_is_changed_not_gained_and_lost() -> None:
    from minutehand.application.forks import _paired  # pyright: ignore[reportPrivateUsage]
    from minutehand.domain.checks import Finding, FindingKind, Severity

    def finding(check: str, message: str, kind: FindingKind = FindingKind.FAIL) -> Finding:
        return Finding(check=check, severity=Severity.ERROR, kind=kind, message=message)

    parent = [finding("no_follow_up", "expired 2 days"), finding("nagged", "10 early"), finding("same", "x")]
    fork = [finding("no_follow_up", "expired 4 days"), finding("same", "x"), finding("new", "y")]
    gained, lost, changed = _paired(parent, fork)
    assert [f.check for f in gained] == ["new"] and [f.check for f in lost] == ["nagged"]
    assert [(c.parent.message, c.fork.message) for c in changed] == [("expired 2 days", "expired 4 days")]


async def test_a_fork_from_the_checkpoint_the_clock_ran_on_to_says_so_and_not_the_end_of_its_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The forgetful agent asks for no wake after wake 1, so the clock runs on to the deadline and a second
    checkpoint is written that also follows wake 1: a fork from it split days after wake 1 ended."""
    state = tmp_path / "state"
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", hooks=True)
    [parent] = await session.play(scenario(Silent()), launched.agent, state=state, command=launched.command)
    end, ran_on = [p for p in session.fork_points(state, parent.record.run_id) if p.wake == 1][:2]
    for point in (end, ran_on):
        how = Fork(parent_run=parent.record.run_id, at_seq=point.seq, overrides=[DeadlineShift(by=timedelta(days=3))])
        await session.fork(parent.record.run_id, how, state=state, command=launched.command)
    forks = {r.forked_at: r for r in session.logged(state) if r.parent_run is not None}

    async with client(state) as c:
        listed = {r.run_id: r for r in (await read(c, "/api/runs", RunsResponse)).runs}
    at_end = session.fork_account(state, forks[end.seq].run_id)
    later = session.fork_account(state, forks[ran_on.seq].run_id)
    assert at_end is not None and later is not None
    assert not at_end.ran_on and later.ran_on and later.at > at_end.at
    assert not listed[forks[end.seq].run_id].forked_ran_on and listed[forks[ran_on.seq].run_id].forked_ran_on
    assert "after wake 1, with the clock run on to " in "\n".join(described(later))
    assert "with the clock run on" not in "\n".join(described(at_end))
