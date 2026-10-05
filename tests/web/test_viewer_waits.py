"""The viewer draws a wait as overdue exactly where the scorecard counts one as due, on a finished run scored by the
real scorer: an agent that reports to a silent owner, and reports again before the first report's expected date.

The second report is a follow-up on the same wait, so the owner's patience starts again from it and the wait is
not due when the run ends, though its first expected date has long passed. The page once drew it overdue from
that date while the scorecard said no wait passed the time an answer was due."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand import session
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import ObligationsResponse, ScorecardResponse
from minutehand.application.checkpoint import Checkpoint, NoHooks, write_checkpoint
from minutehand.application.run_clock import RunClock
from minutehand.domain.checks import ObligationKind, WakeRecord
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Silent
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation
from tests.e2e.support import OWNER, T0, scenario

RUN = "reporting"


def _report(n: int, text: str) -> Change:
    return Change(
        entity=EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=f"1.00000{n}"),
        operation=Operation.CREATE,
        actor=Actor.AGENT,
        body="{}",
        after=MessageSnapshot(text=text, channel="D-owner", recipient_emails=[OWNER]),
    )


async def _finished_run(state: Path, *, report_again: bool) -> None:
    """Wake 1 reports to the owner; wake 2, 50 hours on, reports again or does nothing; the run is cut off at its
    wake limit 100 hours in. Scored and kept the way `session` keeps a run."""
    played = scenario(Silent()).model_copy(update={"expect": [], "max_wakes": 3})
    directory = session.run_dir(state, RUN)
    directory.mkdir(parents=True)
    (directory / session.SCENARIO).write_text(played.model_dump_json())
    clock = RunClock(T0)
    store = SqliteStore(directory / session.WORLD, RUN, clock)
    checkpoint = Checkpoint(wake=0, now=T0, replies=0, pending=[], agent=NoHooks())
    write_checkpoint(store, checkpoint)
    wakes: list[WakeRecord] = []
    for hours, change in (
        (0, _report(1, "I have asked Sofia and will tell you when she answers.")),
        (50, _report(2, "Still waiting on Sofia.") if report_again else None),
        (100, None),
    ):
        clock.jump(T0 + timedelta(hours=hours))
        wake = clock.begin_wake()
        if change is not None:
            store.apply(change)
        write_checkpoint(store, checkpoint.model_copy(update={"wake": wake, "now": clock.now()}))
        wakes.append(
            WakeRecord(
                index=wake, sim_time=clock.now(), world_changes=int(change is not None), commitments_changed=False
            )
        )
    record = RunRecord(
        run_id=RUN,
        scenario=played.name,
        seed=played.seed,
        started_at=T0,
        ended_at=clock.now(),
        wall_seconds=0,
        stop=StopReason.WAKE_LIMIT,
        wakes=wakes,
    )
    judge = session._Judge(played, None, judging=False)  # pyright: ignore[reportPrivateUsage]
    await judge.score(record, store)
    session._keep(directory, record, judge)  # pyright: ignore[reportPrivateUsage]
    store._db.close()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(("report_again", "due"), [(True, 0), (False, 1)])
async def test_the_viewer_draws_a_wait_overdue_only_where_the_scorecard_counts_it_due(
    tmp_path: Path, report_again: bool, due: int
) -> None:
    state = tmp_path / "state"
    await _finished_run(state, report_again=report_again)

    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        card = ScorecardResponse.model_validate_json((await c.get(f"/api/runs/{RUN}/scorecard")).content).scorecard
        drawn = ObligationsResponse.model_validate_json((await c.get(f"/api/runs/{RUN}/obligations")).content)

    assert card is not None
    [owner] = [w for w in drawn.obligations if w.obligation.kind is ObligationKind.ANSWER_FROM_PERSON]
    assert owner.obligation.person == "owner" and owner.obligation.settled_at is None
    assert owner.obligation.expected_by is not None and owner.obligation.expected_by < T0 + timedelta(hours=100)
    assert card.follow_ups_due == due
    assert sum(len(w.fell_due) for w in drawn.obligations) == card.follow_ups_due
    assert sum(1 for w in drawn.obligations for d in w.fell_due if d.late) == card.follow_ups_late
