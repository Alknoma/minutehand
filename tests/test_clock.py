from datetime import UTC, datetime, timedelta

from minutehand.domain.clock import Due, DueKind, next_jump

NOW = datetime(2026, 8, 24, 10, 51, tzinfo=UTC)


def test_the_clock_jumps_to_the_earliest_pending_moment_and_fires_only_what_is_due() -> None:
    jump = next_jump(
        NOW,
        [
            Due(at=NOW + timedelta(days=3), kind=DueKind.AGENT_WAKE, ref="next_wake"),
            Due(at=NOW + timedelta(hours=40), kind=DueKind.PERSON_REPLY, ref="owner"),
        ],
    )
    assert jump is not None and jump.now == NOW + timedelta(hours=40)
    assert [d.ref for d in jump.firing] == ["owner"]


def test_something_already_overdue_fires_without_moving_the_clock() -> None:
    jump = next_jump(NOW, [Due(at=NOW - timedelta(hours=1), kind=DueKind.TICKET_FATE, ref="late")])
    assert jump is not None and jump.now == NOW and [d.ref for d in jump.firing] == ["late"]


def test_nothing_pending_means_the_run_is_over() -> None:
    assert next_jump(NOW, []) is None
