"""Forks: a finished run restarted from one of its checkpoints with the scenario changed."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from minutehand.adapters.agent.reach import reach_for
from minutehand.application.checkpoint import checkpoint_seqs
from minutehand.application.refusals import RunRefused
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.rewind import changed_scenario, fork_run
from minutehand.domain.agent import AgentUnderTest, Booked
from minutehand.domain.experiment import DeadlineShift, Fork, PersonChange, PromptPatch, TicketEdit
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Scenario, TicketState
from minutehand.domain.world import Actor, EntityKind, TicketSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from tests.orchestrator.rig import T0, Rig, scenario, scripted
from tests.orchestrator.world import RecordingClock


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
        poll_interval=0.001,
    )


def state(rig: Rig) -> dict[str, object]:
    return json.loads((rig.tmp / "agent" / "state.json").read_text())


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
    with pytest.raises(RunRefused, match="no state hooks"):
        await fork(
            rig, parent, scenario(ticket_fates=[]), agent, Fork(parent_run="root", at_seq=checkpoint_seqs(store)[0])
        )


async def test_a_fork_between_checkpoints_is_refused(rig: Rig) -> None:
    agent = rig.agent("ask_silent", hooks=True)
    parent, store, _ = await rig.run(scenario(ticket_fates=[]), agent)
    with pytest.raises(RunRefused, match="no checkpoint at seq"):
        await fork(
            rig, parent, scenario(ticket_fates=[]), agent, Fork(parent_run="root", at_seq=checkpoint_seqs(store)[0] + 1)
        )


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
