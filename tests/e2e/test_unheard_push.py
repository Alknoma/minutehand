"""A Slack event the agent never acknowledges (`adapters/providers/slack/inbound`): every send and retry is in the
record, the person's reply stands as said though its push failed, the run says why it stopped, and the run's health
names the push (`checks.health`)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.query.reader import open_model, query
from minutehand.domain.checks import HealthKind
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.world import Actor, PendingSnapshot, PendingStatus, PushSnapshot
from tests.e2e.support import ANSWER, SOFIA, THANKS, agent_under_test, answers, messages, scenario, texts, world


async def test_an_event_never_acknowledged_is_recorded_send_by_send_and_the_reply_stands_as_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "unacknowledging")
    [outcome] = await session.play(
        scenario(answers(after=timedelta(hours=3))), launched.agent, state=tmp_path / "state", command=launched.command
    )

    record, result = outcome.record, outcome.result
    assert record.stop is StopReason.AGENT_FAILED
    assert record.failure is not None and "sent 4 times" in record.failure
    assert "send 4 answered 500" in record.failure and "Slack retries no more" in record.failure
    store = world(tmp_path / "state", record.run_id)
    events = store.events()
    sends = [e.after for e in events if isinstance(e.after, PushSnapshot)]
    answer = [s for s in sends if ANSWER in s.body]
    assert [(s.attempt, s.retry_reason, s.status) for s in answer] == [
        (0, None, 500),
        (1, "http_error", 500),
        (2, "http_error", 500),
        (3, "http_error", 500),
    ]
    assert len({s.item for s in answer}) == 1, "one event, sent four times"
    assert texts(messages(events, Actor.AGENT, to=SOFIA)).count(THANKS) == 4, "each retry was acted on again"
    [said] = [r for r in store.replies() if r.person == "sofia"]
    assert said.text == ANSWER, "the person said it, though the push failed"
    owed = [e.after for e in events if isinstance(e.after, PendingSnapshot) and e.after.person == "sofia"]
    assert owed[-1].status is PendingStatus.ACTED and owed[-1].failure is not None
    [unheard] = [h for h in result.simulation if h.kind is HealthKind.PUSH_FAILED and h.incomplete]
    assert "4 time(s)" in unheard.words and len(unheard.evidence) == 4
    assert result.verdict.kind is VerdictKind.SIMULATION_INCOMPLETE
    rows = query(
        open_model(tmp_path / "state", record.run_id),
        "SELECT item, attempt, status, delivered FROM pushes WHERE provider = 'slack' ORDER BY seq",
    ).rows
    assert [list(r)[1:] for r in rows if r[0] == answer[0].item] == [[0, 500, 0], [1, 500, 0], [2, 500, 0], [3, 500, 0]]
    replies = query(open_model(tmp_path / "state", record.run_id), "SELECT person FROM replies").rows
    assert ["sofia"] in [list(r) for r in replies]
