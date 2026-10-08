"""Where a reply's moment comes from across runs and forks: the run's seed, the person and the ask. The table of what
is due records each draw; a fork keeps every draw its parent made before the checkpoint, draws afresh after it with
its own seed, and a pinned reply time wins over any draw."""

from __future__ import annotations

from datetime import timedelta

from minutehand.application.checkpoint import checkpoint_seqs
from minutehand.application.dues import due_entries
from minutehand.domain.clock import DrawnFrom, DueSource
from minutehand.domain.experiment import Fork, Override, ReplyAt
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import AfterScript, Scenario, Scripted, ScriptedReply, Window
from tests.orchestrator.rig import T0, Rig, scenario
from tests.orchestrator.test_rewind import fork
from tests.orchestrator.world import RecordingClock

WIDE = Window(min=timedelta(hours=6), max=timedelta(hours=60))


def windowed(seed: int) -> Scenario:
    base = scenario(seed=seed)
    script = Scripted(
        then=AfterScript.SILENT,
        replies=[ScriptedReply(to_ask=1, verbatim="Yes, 40k."), ScriptedReply(to_ask=2, verbatim="Signing Friday.")],
    )
    people = [
        p if p.key != "sofia" else p.model_copy(update={"reply": script, "reply_within": WIDE}) for p in base.people
    ]
    return base.model_copy(update={"people": people})


def _moments(replies: list[PersonReply]) -> list[timedelta]:
    return [r.at - r.drawn.asked_at for r in replies if r.drawn is not None]


async def test_the_same_seed_draws_the_same_moments_and_the_table_records_each_draw(rig: Rig) -> None:
    agent = rig.agent("ask_and_file")
    _, first, _ = await rig.run(windowed(7), agent, run_id="a")
    _, again, _ = await rig.run(windowed(7), agent, run_id="b")
    _, other, _ = await rig.run(windowed(8), agent, run_id="c")
    assert _moments(first.replies()) == _moments(again.replies()) != _moments(other.replies())
    assert all(WIDE.min <= m <= WIDE.max for m in _moments(first.replies()) + _moments(other.replies()))
    drawn = [e.drawn for e in due_entries(first) if e.source is DueSource.REPLY]
    assert drawn == [r.drawn for r in first.replies()]
    assert all(d is not None and d.source is DrawnFrom.WINDOW and d.window == WIDE and d.seed == 7 for d in drawn)


async def test_a_fork_keeps_the_draws_before_its_checkpoint_and_draws_afresh_after_with_its_own_seed(rig: Rig) -> None:
    scn, agent = windowed(7), rig.agent("ask_and_file")
    parent, store, _ = await rig.run(scn, agent)
    after_start = checkpoint_seqs(store)[1]  # sofia's first answer is drawn, and has not landed
    [parents_first, parents_second] = store.replies()

    [child] = await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=after_start, seed=8))
    [kept, fresh] = rig.open("child", rig_clock()).replies()
    assert kept == parents_first, "drawn before the checkpoint: kept as the parent drew it"
    assert fresh.drawn is not None and fresh.drawn.seed == 8
    assert parents_second.drawn is not None
    assert fresh.at - fresh.drawn.asked_at != parents_second.at - parents_second.drawn.asked_at
    assert child.seed == 8


async def test_a_pinned_reply_time_wins_over_every_draw(rig: Rig) -> None:
    scn, agent = windowed(7), rig.agent("ask_and_file")
    parent, store, _ = await rig.run(scn, agent)
    after_start = checkpoint_seqs(store)[1]
    pins: list[Override] = [
        ReplyAt(person="sofia", to_ask=1, after=timedelta(hours=1)),
        ReplyAt(person="sofia", to_ask=2, after=timedelta(minutes=30)),
    ]
    await fork(rig, parent, scn, agent, Fork(parent_run="root", at_seq=after_start, overrides=pins))
    answered = [r for r in rig.open("child", rig_clock()).replies() if r.drawn is not None]
    pinned = [r for r in answered if r.drawn is not None and r.drawn.source is DrawnFrom.PINNED]
    assert [r.at - r.drawn.asked_at for r in pinned if r.drawn is not None] == [
        timedelta(hours=1),
        timedelta(minutes=30),
    ]
    assert pinned[0].at == T0 + timedelta(hours=1)


def rig_clock() -> RecordingClock:
    return RecordingClock(T0)
