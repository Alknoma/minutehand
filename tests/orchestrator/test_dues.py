"""The run loop's table of what is due next, as the log records it: every entry when it entered and how it left."""

from __future__ import annotations

from datetime import timedelta

from minutehand.application.checkpoint import checkpoint_seqs
from minutehand.application.dues import due_entries, due_events
from minutehand.checks.no_follow_up import NoFollowUp
from minutehand.checks.runner import view_of
from minutehand.domain.agent import Booked
from minutehand.domain.clock import DueClosed, DueEntry, DueSource
from minutehand.domain.experiment import Fork, PersonChange
from minutehand.domain.world import Actor
from tests.orchestrator.rig import T0, Rig, scenario, scripted
from tests.orchestrator.test_rewind import fork, two_replies
from tests.orchestrator.world import RecordingClock


def _of(entries: list[DueEntry], source: DueSource) -> list[tuple[object, DueClosed | None, object]]:
    return [(e.due.at, e.closed, e.closed_at) for e in entries if e.source is source]


async def test_a_next_wake_reported_again_after_every_wake_is_one_entry_that_fires_at_its_moment(rig: Rig) -> None:
    # START and the reply wake both report a next wake four days from the start: the second names the same moment
    _, store, _ = await rig.run(scenario(), rig.agent("ask_and_file"))

    entries = due_entries(store)
    four_days = T0 + timedelta(days=4)
    assert _of(entries, DueSource.REPORTED) == [(four_days, DueClosed.FIRED, four_days)]
    assert _of(entries, DueSource.REPLY) == [(T0 + timedelta(hours=36), DueClosed.FIRED, T0 + timedelta(hours=36))]
    assert _of(entries, DueSource.FATE) == [(T0 + timedelta(days=3), DueClosed.FIRED, T0 + timedelta(days=3))]
    reported = next(e for e in entries if e.source is DueSource.REPORTED)
    assert reported.entered_at == T0 and reported.entered_wake == 1


async def test_a_booking_deleted_before_it_fires_leaves_the_table_cancelled_and_the_kept_one_fires(rig: Rig) -> None:
    booked = Booked(take_limit=timedelta(seconds=0.1))
    _, store, _ = await rig.run(scenario(ticket_fates=[]), rig.agent("book", extra=[booked]))

    assert _of(due_entries(store), DueSource.BOOKED) == [
        (T0 + timedelta(hours=5), DueClosed.FIRED, T0 + timedelta(hours=5)),
        (T0 + timedelta(hours=2), DueClosed.CANCELLED, T0),
    ]


async def test_a_next_wake_moved_by_the_agent_replaces_the_one_it_named_before(rig: Rig) -> None:
    # keep_waking names a new moment an hour on after every wake: each fires, none is left open
    _, store, _ = await rig.run(scenario(max_wakes=3, ticket_fates=[]), rig.agent("keep_waking"))

    reported = _of(due_entries(store), DueSource.REPORTED)
    assert [at for at, _, _ in reported] == [T0 + timedelta(hours=h) for h in (1, 2, 3)]
    assert [closed for _, closed, _ in reported] == [DueClosed.FIRED, DueClosed.FIRED, None]


async def test_the_table_is_written_by_the_run_loop_and_never_counted_as_the_agents_work(rig: Rig) -> None:
    record, store, _ = await rig.run(scenario(), rig.agent("ask_and_file"))

    rows = due_events(store.events())
    assert rows and {e.actor for e in rows} == {Actor.SCENARIO}
    assert [w.world_changes for w in record.wakes] == [2, 1, 0]


async def test_no_follow_up_says_the_agent_had_planned_its_next_wake_past_the_wait(rig: Rig) -> None:
    # Dania is silent: asking her opens a wait due 66 hours on; the agent plans its next wake 100 hours on
    record, store, _ = await rig.run(
        scenario(ticket_fates=[], deadline_after=timedelta(days=5)),
        rig.agent("ask_silent"),
        env=rig.env(NEXT_WAKE_AFTER_HOURS="100"),
    )
    view = view_of(scenario(ticket_fates=[], deadline_after=timedelta(days=5)), store.events(), record.wakes, [])

    planned = view.model_copy(update={"dues": due_entries(store)})
    [finding] = NoFollowUp().run(planned).findings
    assert finding.message.endswith(
        "; when it fell due, the agent's own next wake was 1 day 10 hours later (reported in wake 1)"
    )
    [unplanned] = NoFollowUp().run(view).findings
    assert "when it fell due" not in unplanned.message


async def test_a_fork_cancels_the_reply_its_person_change_withdrew_and_enters_the_one_asked_again(rig: Rig) -> None:
    scn = two_replies()
    agent = rig.agent("ask_and_file", hooks=True)
    parent, parent_store, _ = await rig.run(scn, agent)
    after_start = checkpoint_seqs(parent_store)[1]
    change = PersonChange(person="sofia", reply=scripted("CHANGED one", "CHANGED two", hours=12))

    await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=after_start, overrides=[change]))

    child = due_entries(rig.open("child", RecordingClock(T0)))
    replies = [(e.due.at, e.closed, e.closed_at) for e in child if e.source is DueSource.REPLY]
    assert replies[0] == (T0 + timedelta(hours=36), DueClosed.CANCELLED, T0)
    assert replies[1] == (T0 + timedelta(hours=12), DueClosed.FIRED, T0 + timedelta(hours=12))
    assert [e.closed for e in due_entries(parent_store) if e.source is DueSource.REPLY] == [DueClosed.FIRED] * 2
