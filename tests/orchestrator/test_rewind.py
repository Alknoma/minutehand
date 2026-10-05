"""Forks: a finished run restarted from one of its checkpoints with the scenario changed."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import timedelta

import pytest

from minutehand.adapters.agent.reach import reach_for
from minutehand.application.checkpoint import NotRestorable, Restorable, checkpoint_seqs, checkpoints
from minutehand.application.refusals import RunRefused
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.restore import Restored, RestoreFailed
from minutehand.application.rewind import RESTORE_RECORD, changed_scenario, fork_run
from minutehand.domain.agent import AgentUnderTest, Booked
from minutehand.domain.experiment import DeadlineShift, Fork, PersonChange, PromptPatch, TicketEdit
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Scenario, TicketState
from minutehand.domain.world import Actor, EntityKind, EntityRef, TicketSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from tests.orchestrator.rig import T0, Rig, scenario, scripted
from tests.orchestrator.world import CHAT, SECRET, RecordingClock


def two_replies() -> Scenario:
    base = scenario()
    people = [
        p if p.key != "sofia" else p.model_copy(update={"reply": scripted("Yes, 40k.", "Signing Friday.", hours=36)})
        for p in base.people
    ]
    return base.model_copy(update={"people": people})


async def fork(rig: Rig, parent: RunRecord, scn: Scenario, agent: AgentUnderTest, how: Fork) -> list[RunRecord]:
    def open_parent(clock: Clock) -> Store:
        return rig.open(parent.run_id, clock)

    return await fork_run(
        fork=how,
        parent=parent,
        open_parent=open_parent,
        run_id="child",
        scenario=scn,
        agent=agent,
        reach=reach_for(agent, env=rig.env()),
        services=rig.services(),
        replier_for=ScriptedReplier,
        state_dir=rig.tmp / "state",
        mounts=rig.board,
        traffic=rig.board,
        signing={CHAT: SECRET},
    )


def state(rig: Rig) -> dict[str, object]:
    return json.loads((rig.tmp / "agent" / "state.json").read_text())


def runs(rig: Rig) -> list[str]:
    """Every run the world file holds: a refused fork must leave none of its own."""
    db = sqlite3.connect(rig.tmp / "world.db")
    try:
        return [r[0] for r in db.execute("SELECT run_id FROM run ORDER BY rowid")]
    finally:
        db.close()


async def test_a_fork_replays_earlier_replies_applies_a_person_change_and_leaves_the_parent_alone(rig: Rig) -> None:
    scn = two_replies()
    agent = rig.agent("ask_and_file", hooks=True)
    parent, parent_store, _ = await rig.run(scn, agent)
    assert parent.stop is StopReason.AGENT_DONE
    assert [r.text for r in parent_store.replies()] == ["Yes, 40k.", "Signing Friday."]
    before = [e.model_dump() for e in parent_store.events()]
    after_start = checkpoint_seqs(parent_store)[1]
    rig.chat.pushed.clear()

    change = PersonChange(person="sofia", reply=scripted("CHANGED one", "CHANGED two", hours=12))
    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=after_start, overrides=[change]))

    assert child.stop is StopReason.AGENT_DONE
    assert child.parent_run == "root" and child.forked_at == after_start
    # The reply sofia had already decided before the fork lands unchanged at +36h; her next answer is the new one.
    landed = [json.loads(p)["text"] for p in rig.chat.pushed]
    assert landed == ["Yes, 40k.", "CHANGED two"]
    assert [w.sim_time for w in child.wakes] == [
        T0,
        T0 + timedelta(hours=36),
        T0 + timedelta(hours=48),
        T0 + timedelta(days=4),
    ]
    # The agent's own state was restored to the end of wake 1, not carried on from the parent's end.
    assert state(rig)["reasons"] == ["start", "person_replied", "person_replied", "due"]
    assert state(rig)["heard"] == ["Yes, 40k.", "CHANGED two"]
    reopened = rig.open("root", RecordingClock(T0))
    assert [e.model_dump() for e in reopened.events()] == before
    assert [r.text for r in reopened.replies()] == ["Yes, 40k.", "Signing Friday."]


async def test_a_ticket_edit_lands_in_the_fork_as_the_scenario(rig: Rig) -> None:
    scn = scenario(ticket_fates=[])
    agent = rig.agent("ask_and_file", hooks=True)
    parent, parent_store, _ = await rig.run(scn, agent)
    assert parent.stop is StopReason.NOTHING_PENDING
    ticket = next(e.entity for e in parent_store.events() if e.entity.kind is EntityKind.TICKET)
    at = checkpoint_seqs(parent_store)[1]

    edit = TicketEdit(entity=ticket, state=TicketState.DONE)
    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=at, overrides=[edit]))

    assert child.stop is StopReason.AGENT_DONE
    edited = rig.open("child", RecordingClock(T0)).events(since=at)
    landed = [e for e in edited if e.entity == ticket and e.actor is Actor.SCENARIO]
    assert len(landed) == 1 and isinstance(landed[0].after, TicketSnapshot)
    assert landed[0].after.state is TicketState.DONE and landed[0].sim_time == T0


async def test_a_fork_of_an_agent_without_state_hooks_is_refused(rig: Rig) -> None:
    agent = rig.agent("ask_silent")
    parent, store, _ = await rig.run(scenario(ticket_fates=[]), agent)
    with pytest.raises(RunRefused, match="no state hooks") as refused:
        await fork(
            rig, parent, scenario(ticket_fates=[]), agent, Fork(parent_run="root", at_seq=checkpoint_seqs(store)[0])
        )
    assert "cannot rewind what a real third-party service the run reached keeps" in str(refused.value)
    assert runs(rig) == ["root"]


async def test_a_fork_between_checkpoints_is_refused(rig: Rig) -> None:
    agent = rig.agent("ask_silent", hooks=True)
    parent, store, _ = await rig.run(scenario(ticket_fates=[]), agent)
    with pytest.raises(RunRefused, match="no checkpoint at seq"):
        await fork(
            rig, parent, scenario(ticket_fates=[]), agent, Fork(parent_run="root", at_seq=checkpoint_seqs(store)[0] + 1)
        )
    assert runs(rig) == ["root"]


async def test_a_prompt_patch_with_nothing_on_the_wire_is_refused(rig: Rig) -> None:
    agent = rig.agent("ask_silent", hooks=True)
    parent, store, _ = await rig.run(scenario(ticket_fates=[]), agent)
    with pytest.raises(RunRefused, match="prompt_patch"):
        await fork(
            rig,
            parent,
            scenario(ticket_fates=[]),
            agent,
            Fork(parent_run="root", at_seq=checkpoint_seqs(store)[0], overrides=[PromptPatch(text="Be brief.")]),
        )


async def test_a_fork_with_a_booking_pending_is_refused(rig: Rig) -> None:
    # Until this was refused, the fork shared the parent's schedule record and, on the AWS provider, raised
    # LookupError when it fired: the schedule's queue was in the parent's account, in the parent process's memory.
    agent = rig.agent("book", extra=[Booked()], hooks=True)
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent)
    after_start = checkpoint_seqs(store)[1]
    with pytest.raises(RunRefused, match=r"1 booked wake\(s\) pending at seq \d+ \(testsched kept\)"):
        await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=after_start))
    assert runs(rig) == ["root"]


async def test_a_fork_after_the_booking_fired_is_not_refused(rig: Rig) -> None:
    agent = rig.agent("book", extra=[Booked()], hooks=True)
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent)
    last = checkpoint_seqs(store)[-1]
    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=last))
    assert child.forked_at == last


def test_a_deadline_shift_moves_the_childs_deadline() -> None:
    scn = scenario()
    moved = changed_scenario(scn, Fork(parent_run="root", at_seq=1, overrides=[DeadlineShift(by=timedelta(days=-4))]))
    assert moved.deadline == T0 + timedelta(days=10)
    assert scn.deadline == T0 + timedelta(days=14)


async def test_a_ticket_edit_on_a_provider_that_cannot_edit_is_refused_and_leaves_no_run(rig: Rig) -> None:
    agent = rig.agent("ask_silent", hooks=True)
    parent, store, _ = await rig.run(scenario(ticket_fates=[]), agent)
    edit = TicketEdit(entity=EntityRef(provider="nowhere", kind=EntityKind.TICKET, external_id="t1"))
    with pytest.raises(RunRefused, match="nowhere, which cannot edit tickets"):
        await fork(
            rig,
            parent,
            scenario(ticket_fates=[]),
            agent,
            Fork(parent_run="root", at_seq=checkpoint_seqs(store)[0], overrides=[edit]),
        )
    assert runs(rig) == ["root"]


async def test_a_restore_step_that_fails_is_refused_and_the_child_it_made_is_discarded(rig: Rig) -> None:
    agent = rig.agent("ask_silent", hooks=True)
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent)
    assert agent.state is not None
    broken = agent.model_copy(
        update={
            "state": agent.state.model_copy(
                update={"restore": [sys.executable, "-c", "import sys; print('the snapshot is gone'); sys.exit(5)"]}
            )
        }
    )
    with pytest.raises(RestoreFailed, match=r"(?s)failed at step `restore`.*exited 5.*the snapshot is gone"):
        await fork(rig, parent, scn, broken, Fork(parent_run="root", at_seq=checkpoint_seqs(store)[1]))
    assert runs(rig) == ["root"]
    assert not (rig.tmp / "state" / "child").exists()


async def test_background_calls_past_the_settle_limit_make_the_checkpoint_not_restorable_and_a_fork_from_it_is_refused(
    rig: Rig,
) -> None:
    agent = rig.agent("ask_and_keep_calling", hooks=True)
    assert agent.state is not None
    agent = agent.model_copy(
        update={
            "state": agent.state.model_copy(
                update={"quiet": timedelta(seconds=0.2), "settle_limit": timedelta(seconds=0.5)}
            )
        }
    )
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent, env=rig.env(BACKGROUND_SECONDS="1.5"))

    found = checkpoints(store)
    setup, after_start = list(found)[:2]
    unsettled = found[after_start].agent
    assert isinstance(found[setup].agent, Restorable)
    assert isinstance(unsettled, NotRestorable)
    assert "still making outbound calls" in unsettled.reason and "GET /testchat/inbox" in unsettled.reason
    assert not (rig.tmp / "state" / "root" / "wake-1").exists()

    with pytest.raises(RunRefused, match=f"the checkpoint at seq {after_start} of run root is not restorable"):
        await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=after_start))
    assert runs(rig) == ["root"]


async def test_background_calls_inside_the_settle_limit_delay_the_checkpoint_until_they_stop(rig: Rig) -> None:
    agent = rig.agent("ask_and_keep_calling", hooks=True)
    assert agent.state is not None
    agent = agent.model_copy(
        update={
            "state": agent.state.model_copy(
                update={"quiet": timedelta(seconds=0.2), "settle_limit": timedelta(seconds=10)}
            )
        }
    )
    began = time.monotonic()
    _, store, _ = await rig.run(scenario(ticket_fates=[]), agent, env=rig.env(BACKGROUND_SECONDS="1.0"))

    assert time.monotonic() - began >= 1.2
    assert all(isinstance(c.agent, Restorable) for c in checkpoints(store).values())
    last = rig.board.last_call()
    assert last is not None and last.what == "GET /testchat/inbox"


async def test_a_fork_of_an_agent_that_cannot_be_asked_for_its_report_says_it_was_not_verified(rig: Rig) -> None:
    scn = scenario(ticket_fates=[])
    agent = rig.agent("ask_silent", hooks=True)
    parent, store, _ = await rig.run(scn, agent)
    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=checkpoint_seqs(store)[1]))
    restored = Restored.model_validate_json((rig.tmp / "state" / child.run_id / RESTORE_RECORD).read_text())
    assert not restored.verified and restored.unverified is not None
    assert "no report endpoint" in restored.unverified
