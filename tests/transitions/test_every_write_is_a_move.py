"""Every write to an item in the world is a move of its state (`domain.transitions.moves`): the one record an item
check, the reviewer and the read model's `transitions` view read, whoever made it and whatever the item is."""

from __future__ import annotations

import json

from minutehand.domain.scenario import TicketState
from minutehand.domain.transitions import DELETED, EXISTS, moves
from minutehand.domain.world import Actor, EntityKind, Operation
from tests.checks.world import Log, person


def test_a_message_posted_and_edited_moves_from_nothing_to_existing_and_carries_its_words() -> None:
    log = Log()
    tom = person("tom")
    sent = log.message([tom], 1, text="Could you send the contract?")
    log.edit(sent, 2, "Could you send the signed contract?")

    posted, edited = moves(log.events)

    assert (posted.name, posted.from_state, posted.to_state, posted.by) == ("create", None, EXISTS, Actor.AGENT)
    assert json.loads(posted.content)["text"] == "Could you send the contract?"
    assert (edited.name, edited.from_state, edited.to_state) == ("update", EXISTS, EXISTS)
    assert (posted.seq, edited.seq, posted.item) == (sent.seq, sent.seq + 1, sent.entity)


def test_a_ticket_moves_through_its_own_states_and_a_delete_leaves_it_deleted() -> None:
    log = Log()
    filed = log.ticket("Renew the lease", None, 1, external_id="t1")
    log.ticket("Renew the lease", None, 2, external_id="t1", operation=Operation.UPDATE, state=TicketState.DONE)
    log.ticket("Renew the lease", None, 3, external_id="t1", operation=Operation.DELETE)

    states = [(m.from_state, m.to_state) for m in moves(log.events)]

    assert states == [(None, "open"), ("open", "done"), ("done", DELETED)]
    assert all(m.item == filed.entity for m in moves(log.events))


def test_a_recorded_transition_is_read_as_recorded() -> None:
    log = Log()
    nadia = person("nadia")
    log.transition("approvals", "req_1", 1, "pending", name="create", kind=EntityKind.SERVICE_ITEM)
    log.transition(
        "approvals", "req_1", 2, "needs_info", from_state="pending", name="ask_back", actor=Actor.PERSON, who=nadia
    )

    created, asked = moves(log.events)

    assert (created.name, created.from_state, created.to_state) == ("create", None, "pending")
    assert (asked.name, asked.from_state, asked.to_state, asked.who) == ("ask_back", "pending", "needs_info", "nadia")


def test_the_agents_memory_and_reads_are_no_moves() -> None:
    log = Log()
    tom = person("tom")
    sent = log.message([tom], 1)
    log.memory("plan", {"asked": "tom"}, 1)
    log.read(sent.entity, 2)

    assert [m.seq for m in moves(log.events)] == [sent.seq]


def test_the_world_as_the_scenario_set_it_up_is_no_move_but_the_state_it_left_is_kept() -> None:
    log = Log()
    log.ticket("Renew the lease", None, 0, external_id="t1", actor=Actor.SCENARIO, wake=0)
    closed = log.ticket(
        "Renew the lease", None, 2, external_id="t1", operation=Operation.UPDATE, state=TicketState.DONE
    )

    [move] = moves(log.events)

    assert (move.seq, move.from_state, move.to_state) == (closed.seq, "open", "done")


def test_a_write_its_provider_also_recorded_as_a_transition_is_one_move_in_the_providers_words() -> None:
    log = Log()
    tomas = person("tomas")
    log.ticket("Write the notes", tomas, 3, external_id="t1", actor=Actor.PERSON, operation=Operation.UPDATE)
    started = log.transition(
        "tracker", "t1", 3, "In Progress", from_state="To Do", name="Start work", actor=Actor.PERSON, who=tomas
    )

    [move] = moves(log.events)

    assert (move.seq, move.name, move.from_state, move.to_state) == (started.seq, "Start work", "To Do", "In Progress")
