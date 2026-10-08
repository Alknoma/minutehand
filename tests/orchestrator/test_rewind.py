"""Forks: a finished run restarted from one of its checkpoints with the scenario changed."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta

import pytest

from minutehand import session
from minutehand.adapters.agent.reach import reach_for
from minutehand.application.checkpoint import NotRestorable, Remembered, checkpoint_seqs, checkpoints
from minutehand.application.memory import digest, memory_of
from minutehand.application.refusals import RunRefused
from minutehand.application.replier import PeopleReplier
from minutehand.application.restore import Restored, Verification
from minutehand.application.rewind import RESTORE_RECORD, changed_scenario, fork_run
from minutehand.domain.agent import AgentUnderTest, Booked
from minutehand.domain.experiment import DeadlineShift, Fork, PersonChange, PromptPatch, TicketEdit
from minutehand.domain.provider import Manifest
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import AfterScript, Scenario, TicketState
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


async def fork(
    rig: Rig,
    parent: RunRecord,
    scn: Scenario,
    agent: AgentUnderTest,
    how: Fork,
    *,
    manifests: list[Manifest] | None = None,
) -> list[RunRecord]:
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
        replier_for=lambda s, pins: PeopleReplier(s, None, pins=pins),
        state_dir=rig.tmp / "state",
        mounts=rig.mounts,
        traffic=rig.board,
        signing={CHAT: SECRET},
        manifests=manifests or [],
    )


def remembered(rig: Rig, run_id: str, *, until: int | None = None) -> dict[str, object]:
    """What the agent remembered in a run (its key `state`), as the run's log holds it, as of `until`."""
    held = memory_of(rig.open(run_id, RecordingClock(T0)).events(), until=until)
    loaded = json.loads(held[("default", "state")])
    assert isinstance(loaded, dict)
    return loaded


def runs(rig: Rig) -> list[str]:
    """Every run the world file holds: a refused fork must leave none of its own."""
    db = sqlite3.connect(rig.tmp / "world.db")
    try:
        return [r[0] for r in db.execute("SELECT run_id FROM run ORDER BY rowid")]
    finally:
        db.close()


async def test_a_fork_applies_a_person_change_to_a_reply_decided_but_not_yet_landed_and_leaves_the_parent_alone(
    rig: Rig,
) -> None:
    scn = two_replies()
    agent = rig.agent("ask_and_file")
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
    # The reply sofia had decided before the fork, due at +36h, had not been said by it: it is withdrawn and she
    # is asked again as she now is, answering 12 hours on.
    landed = [json.loads(p)["text"] for p in rig.chat.pushed]
    assert landed == ["CHANGED one", "CHANGED two"]
    assert [w.sim_time for w in child.wakes] == [
        T0,
        T0 + timedelta(hours=12),
        T0 + timedelta(hours=24),
        T0 + timedelta(days=4),
    ]
    # The agent's memory was its parent's at the end of wake 1, not carried on from the parent's end, and nothing
    # restored it: the fork reads its parent's log up to the checkpoint.
    assert remembered(rig, "child")["reasons"] == ["start", "person_replied", "person_replied", "due"]
    assert remembered(rig, "child")["heard"] == ["CHANGED one", "CHANGED two"]
    assert remembered(rig, "root")["heard"] == ["Yes, 40k.", "Signing Friday."]
    reopened = rig.open("root", RecordingClock(T0))
    assert [e.model_dump() for e in reopened.events()] == before
    assert [r.text for r in reopened.replies()] == ["Yes, 40k.", "Signing Friday."]


async def test_a_ticket_edit_lands_in_the_fork_as_the_scenario(rig: Rig) -> None:
    scn = scenario(ticket_fates=[])
    agent = rig.agent("ask_and_file")
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


async def test_a_fork_starts_from_its_parents_memory_at_the_checkpoint_with_no_hooks(rig: Rig) -> None:
    """The memory a fork starts from is the parent's log up to the checkpoint: equal, key by key and as a digest, to
    what the checkpoint kept, and not what the parent remembered by its end."""
    scn = two_replies()
    agent = rig.agent("ask_and_file")
    parent, parent_store, _ = await rig.run(scn, agent)
    at = checkpoint_seqs(parent_store)[2]  # after the first reply: the middle of the run
    held = checkpoints(parent_store)[at].agent
    at_checkpoint = memory_of(parent_store.events(), until=at)
    assert at_checkpoint != memory_of(parent_store.events()), "the parent remembered more after the checkpoint"
    assert held.memory == digest(at_checkpoint)

    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=at))

    child_store = rig.open("child", RecordingClock(T0))
    assert memory_of(child_store.events(), until=at) == at_checkpoint
    assert remembered(rig, "child", until=at)["reasons"] == ["start", "person_replied"]
    restored = Restored.model_validate_json((rig.tmp / "state" / child.run_id / RESTORE_RECORD).read_text())
    assert restored.memory == held.memory and restored.verified_by == [Verification.MEMORY]


async def test_a_fork_between_checkpoints_is_refused(rig: Rig) -> None:
    agent = rig.agent("ask_silent")
    parent, store, _ = await rig.run(scenario(ticket_fates=[]), agent)
    with pytest.raises(RunRefused, match="no checkpoint at seq"):
        await fork(
            rig, parent, scenario(ticket_fates=[]), agent, Fork(parent_run="root", at_seq=checkpoint_seqs(store)[0] + 1)
        )
    assert runs(rig) == ["root"]


async def test_a_prompt_patch_with_nothing_on_the_wire_is_refused(rig: Rig) -> None:
    agent = rig.agent("ask_silent")
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
    agent = rig.agent("book", extra=[Booked(take_limit=timedelta(seconds=0.1))])
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent)
    after_start = checkpoint_seqs(store)[1]
    with pytest.raises(RunRefused, match=r"1 booked wake\(s\) pending at seq \d+ \(testsched kept\)"):
        await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=after_start))
    assert runs(rig) == ["root"]


async def test_a_fork_after_the_booking_fired_is_not_refused(rig: Rig) -> None:
    agent = rig.agent("book", extra=[Booked(take_limit=timedelta(seconds=0.1))])
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent)
    last = checkpoint_seqs(store)[-1]
    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=last))
    assert child.forked_at == last


def _outside(rig: Rig) -> list[Manifest]:
    """The test scheduler, as a provider that says it keeps its deliveries outside the log, as AWS does."""
    return [rig.sched.manifest.model_copy(update={"state_outside_log": "its deliveries, in this test's memory"})]


async def test_a_fork_after_a_provider_with_state_outside_the_log_was_used_is_refused(rig: Rig) -> None:
    """The fork after the booking fired shares nothing pending, and before this was refused it ran: the child was
    answered by a scheduler holding none of what the parent had booked or delivered (on AWS, a fresh account
    with none of the agent's queues), and nothing said so."""
    agent = rig.agent("book", extra=[Booked(take_limit=timedelta(seconds=0.1))])
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent)
    last = checkpoint_seqs(store)[-1]
    refusal = (
        rf"the fork at seq {last} of run root cannot rewind what the run had built up in testsched \(its changes "
        r"in the log\), which keeps its deliveries, in this test's memory"
    )
    with pytest.raises(RunRefused, match=refusal):
        await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=last), manifests=_outside(rig))
    assert runs(rig) == ["root"]


async def test_a_fork_before_the_agent_first_used_such_a_provider_is_not_refused(rig: Rig) -> None:
    agent = rig.agent("book", extra=[Booked(take_limit=timedelta(seconds=0.1))])
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent)
    setup = checkpoint_seqs(store)[0]
    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=setup), manifests=_outside(rig))
    assert child.forked_at == setup


def test_a_deadline_shift_moves_the_childs_deadline() -> None:
    scn = scenario()
    moved = changed_scenario(scn, Fork(parent_run="root", at_seq=1, overrides=[DeadlineShift(by=timedelta(days=-4))]))
    assert moved.deadline == T0 + timedelta(days=10)
    assert scn.deadline == T0 + timedelta(days=14)


async def test_a_ticket_edit_on_a_provider_that_cannot_edit_is_refused_and_leaves_no_run(rig: Rig) -> None:
    agent = rig.agent("ask_silent")
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


async def test_a_checkpoint_the_agent_went_on_writing_its_memory_after_is_not_restorable_and_is_refused(
    rig: Rig,
) -> None:
    """The agent reports idle and a process of its own writes its memory a moment later, in the same wake: the
    checkpoints of that wake hold memory it had not finished writing."""
    agent = rig.agent("remember_late")
    scn = scenario(ticket_fates=[])
    parent, store, _ = await rig.run(scn, agent, env=rig.env(BACKGROUND_SECONDS="0.2"))
    for _ in range(100):
        if ("default", "late") in memory_of(store.events()):
            break
        await asyncio.sleep(0.05)
    late = next(e for e in store.events() if e.entity.external_id == "default/late")
    setup, after_start = checkpoint_seqs(store)[:2]
    assert late.wake == 1 and late.seq > after_start
    points = {p.seq: p.agent for p in session.points_in(store)}
    assert isinstance(points[setup], Remembered)
    unsettled = points[after_start]
    assert isinstance(unsettled, NotRestorable)
    assert f"the first to default/late at seq {late.seq}" in unsettled.reason

    with pytest.raises(RunRefused, match=f"the checkpoint at seq {after_start} of run root is not restorable"):
        await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=after_start))
    assert runs(rig) == ["root"]


async def test_a_fork_of_an_agent_that_cannot_be_asked_for_its_report_says_it_was_not_verified(rig: Rig) -> None:
    scn = scenario(ticket_fates=[])
    agent = rig.agent("ask_silent")
    parent, store, _ = await rig.run(scn, agent)
    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=checkpoint_seqs(store)[1]))
    restored = Restored.model_validate_json((rig.tmp / "state" / child.run_id / RESTORE_RECORD).read_text())
    assert not restored.verified and restored.unverified is not None
    assert "no report endpoint" in restored.unverified


def test_a_fork_whose_change_leaves_a_relayed_tell_unsayable_is_refused_saying_why() -> None:
    """Rosa's new answer no longer holds the tell the scenario expects her to say. Before, the fork crashed with a
    validation traceback and exited 1, which every surface reads as 'a check failed'."""
    from minutehand.application.refusals import RunRefused
    from minutehand.application.rewind import changed_scenario
    from minutehand.domain.experiment import Fork, PersonChange
    from minutehand.domain.scenario import Scenario, Scripted, ScriptedReply

    scenario = Scenario.model_validate(
        {
            "name": "relay",
            "goal": "Tell Owen the reference.",
            "owner": "owen",
            "starts_at": "2026-08-24T09:00:00Z",
            "people": [
                {"key": "owen", "name": "Owen", "email": "owen@example.com", "reply": {"kind": "silent"}},
                {
                    "key": "rosa",
                    "name": "Rosa",
                    "email": "rosa@example.com",
                    "reply": {
                        "kind": "scripted",
                        "then": "silent",
                        "replies": [{"to_ask": 1, "verbatim": "Reference LH-2291."}],
                    },
                },
            ],
            "expect": [{"kind": "relayed", "said_by": "rosa", "to": "owen", "holding": ["LH-2291"]}],
        }
    )
    change = PersonChange(
        person="rosa",
        reply=Scripted(then=AfterScript.SILENT, replies=[ScriptedReply(to_ask=1, verbatim="Friday is taken.")]),
    )

    with pytest.raises(
        RunRefused, match=r"self-contradictory: .*no step of rosa's script and none of their facts holds 'LH-2291'"
    ):
        changed_scenario(scenario, Fork(parent_run="p", at_seq=1, overrides=[change]))
