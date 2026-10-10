"""When the agent could first know each change someone else made, and when it acted (`domain.reactions`): a reply is
seen by the wake it brings, a decision by the agent's first read of the item answered 2xx after it; a read that failed
is no read; the act is the agent's first move in the world after seeing."""

from __future__ import annotations

from datetime import timedelta

from minutehand.domain.reactions import reactions
from minutehand.domain.world import Actor, EntityKind, Exchange, RecordedCall
from tests.checks.world import Log, at, person

NADIA = person("nadia")


def _read(path: str, status: int, hours: float) -> RecordedCall:
    exchange = Exchange(method="GET", host="api.approvals.example", path=path, status=status)
    return RecordedCall(exchange=exchange, provider=None, first_seq=1, last_seq=0, wake=1, sim_time=at(hours))


def _decided() -> Log:
    log = Log()
    log.transition("approvals", "req_1", 1, "pending", name="create", kind=EntityKind.SERVICE_ITEM)
    log.transition(
        "approvals", "req_1", 2, "approved", from_state="pending", name="approve", actor=Actor.PERSON, who=NADIA
    )
    return log


def test_a_decision_is_seen_by_the_first_read_answered_after_it_and_acted_on_by_the_next_move() -> None:
    log = _decided()
    log.transition("orders", "o1", 6, "exists", name="create", kind=EntityKind.STORED)
    reads = [_read("/v1/requests/req_1", 200, 1.5), _read("/v1/requests/req_1", 200, 6)]

    [decision] = reactions(log.events, reads, [])

    assert (decision.change.name, decision.seen_by, decision.seen_call) == ("approve", "read", 2)
    assert decision.unseen == timedelta(hours=4) and decision.to_act == timedelta(0)
    assert decision.acted is not None and decision.acted.provider == "orders"


def test_a_read_that_failed_is_no_read() -> None:
    reads = [_read("/v1/requests/req_1", 502, 3), _read("/v1/requests/req_1", 200, 7)]

    [decision] = reactions(_decided().events, reads, [])

    assert decision.seen_call == 2 and decision.unseen == timedelta(hours=5), "the 502 at hour 3 showed nothing"


def test_a_reply_is_seen_by_the_wake_it_brings() -> None:
    log = Log()
    log.message([NADIA], 1, "Could you approve PO-7731?")
    log.transition(
        "slack",
        "conv1",
        2,
        "replied",
        from_state="awaiting",
        name="reply",
        actor=Actor.PERSON,
        who=NADIA,
        kind=EntityKind.MESSAGE,
    )
    log.message([NADIA], 2, "Thank you.")

    [reply] = reactions(log.events, [], [(at(1), "start"), (at(2), "person_replied")])

    assert (reply.seen_by, reply.unseen, reply.to_act) == ("wake", timedelta(0), timedelta(0))


def test_a_change_the_agent_never_read_was_never_seen_and_never_acted_on() -> None:
    [decision] = reactions(_decided().events, [_read("/v1/requests/other", 200, 5)], [])

    assert (decision.seen_at, decision.acted_at, decision.unseen) == (None, None, None)
