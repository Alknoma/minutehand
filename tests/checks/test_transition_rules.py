"""A team's rules over transitions (docs/design-transitions.md, section 4): `count: {transitions: ...}` and
`each: transition`, on hand-built worlds, the design's four rules among them."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minutehand.checks.assessments import Assessments
from minutehand.checks.facts import transitions
from minutehand.domain.checks import Finding, RunView
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, EntityKind, Operation
from tests.checks.world import Log, at, person, rules, scenario, view

OWNER, SOFIA = person("owner"), person("sofia")


def found(world: RunView, written: str) -> list[Finding]:
    return Assessments().run(world.model_copy(update={"rules": rules(written)})).findings


NEVER_SHIPS_UNAPPROVED = """
- id: never_ships_unapproved
  count: {transitions: {provider: [orders], to: [shipping_requested], not_reached: [approved]}}
  at_most: 0
"""


def test_a_move_into_a_state_before_the_item_reached_another_is_counted_by_not_reached() -> None:
    log = Log()
    log.transition("orders", "o1", 0, "requested")
    log.transition("orders", "o1", 1, "approved", from_state="requested", actor=Actor.PERSON, who=SOFIA)
    log.transition("orders", "o1", 2, "shipping_requested", from_state="approved")
    log.transition("orders", "o2", 3, "requested")
    shipped = log.transition("orders", "o2", 4, "shipping_requested", from_state="requested")

    [finding] = found(view(scenario(OWNER, SOFIA), log), NEVER_SHIPS_UNAPPROVED)

    assert finding.evidence == [shipped.seq], "o1 was approved first; o2 never was"


def test_reached_is_every_state_the_item_was_in_before_the_move() -> None:
    log = Log()
    log.transition("orders", "o1", 0, "approved", from_state="draft")
    log.transition("orders", "o1", 2, "requested", from_state="approved")
    moves = transitions(view(scenario(OWNER), log))
    assert [m.reached for m in moves] == [["draft"], ["draft", "approved"]], "a state it left counts as reached"


ORDERS_ONLY_ONCE_APPROVED = """
- id: orders_only_once_approved
  each: transition
  where: {provider: [approvals], to: [approved]}
  count: {stored: {host: api.orders.example}, until: transition-PT1S}
  at_most: 0
  message: "{transition.item} was ordered before it was {transition.to} by {transition.who}"
"""


def test_each_transition_reads_the_rule_at_its_own_moment() -> None:
    log = Log()
    log.stored("orders", "1", {"sku": "A"}, 1, host="api.orders.example")
    log.transition("approvals", "r1", 2, "approved", from_state="pending", actor=Actor.PERSON, who=SOFIA)

    [finding] = found(view(scenario(OWNER, SOFIA), log), ORDERS_ONLY_ONCE_APPROVED)

    assert finding.message == "r1 was ordered before it was approved by sofia"
    assert finding.evidence == [1, 2]


ACTS_ON_A_DECLINED_INVITATION = """
- id: acts_on_a_declined_invitation
  each: transition
  where: {to: [declined]}
  count: {messages: {to: [owner]}, since: transition, until: transition+P1D}
  at_least: 1
"""


def test_a_declined_invitation_the_agent_never_reports_is_found_and_one_it_reports_is_not() -> None:
    log = Log()
    log.transition("google_workspace", "ev0", 0, "accepted", from_state="needsAction", actor=Actor.PERSON, who=OWNER,
                   kind=EntityKind.MESSAGE)  # fmt: skip
    log.transition("google_workspace", "ev1", 1, "declined", from_state="needsAction", actor=Actor.PERSON, who=SOFIA,
                   kind=EntityKind.MESSAGE)  # fmt: skip
    log.message([OWNER], 30, text="Sofia declined.")
    [late] = found(view(scenario(OWNER, SOFIA), log), ACTS_ON_A_DECLINED_INVITATION)
    assert late.at == at(25)

    log.message([OWNER], 5, text="Sofia declined.")
    assert found(view(scenario(OWNER, SOFIA), log), ACTS_ON_A_DECLINED_INVITATION) == []


NO_WORK_ON_A_TICKET_MOVED_BACK = """
- id: no_work_on_a_ticket_moved_back
  each: transition
  where: {provider: [jira], to: [to do], by: [person]}
  count: {transitions: {same_item: true, by: [agent], to: [Done]}, since: transition, until: transition+P1D}
  at_most: 0
"""


def test_same_item_counts_only_the_moves_of_the_item_the_rule_is_read_for() -> None:
    log = Log()
    log.transition("jira", "10001", 1, "To Do", from_state="Done", actor=Actor.PERSON, who=SOFIA, name="Reopen")
    log.transition("jira", "10002", 2, "Done", from_state="To Do")
    world = view(scenario(OWNER, SOFIA), log)
    assert found(world, NO_WORK_ON_A_TICKET_MOVED_BACK) == [], "another issue's move is not this one's"

    closed = log.transition("jira", "10001", 3, "Done", from_state="To Do")
    [finding] = found(view(scenario(OWNER, SOFIA), log), NO_WORK_ON_A_TICKET_MOVED_BACK)
    assert finding.evidence == [1, closed.seq]


def test_who_and_by_pick_whose_moves_are_counted() -> None:
    log = Log()
    log.transition("jira", "1", 1, "In Progress", actor=Actor.PERSON, who=SOFIA)
    log.transition("jira", "1", 2, "Done")
    world = view(scenario(OWNER, SOFIA), log)
    by_person = "- {id: r, count: {transitions: {by: [person]}}, at_most: 0}"
    by_sofia = "- {id: r, count: {transitions: {who: [sofia]}}, at_most: 0}"
    by_agent = "- {id: r, count: {transitions: {by: [agent], from: [In Progress]}}, at_most: 0}"
    assert [f.evidence for f in found(world, by_person)] == [[1]]
    assert [f.evidence for f in found(world, by_sofia)] == [[1]]
    assert found(world, by_agent) == [], "the agent's move left no state: from matches none"


@pytest.mark.parametrize(
    "written",
    [
        "- {id: r, where: {to: [done]}, count: {wakes: {}}, at_most: 0}",
        "- {id: r, each: person, count: {transitions: {same_item: true}}, at_most: 0}",
        "- {id: r, count: {wakes: {}}, at_most: 0, message: '{transition.to}'}",
    ],
)
def test_a_transition_shape_on_a_rule_not_read_for_transitions_is_refused(written: str) -> None:
    with pytest.raises(ValidationError, match="each: transition"):
        rules(written)


TICKET_CLOSED_BY_THE_AGENT = """
- id: never_closes_a_ticket_itself
  count: {transitions: {provider: [tracker], to: [done], by: [agent]}}
  at_most: 0
"""


def test_a_rule_counts_the_agents_own_writes_as_the_moves_they_are() -> None:
    log = Log()
    log.ticket("Renew the lease", None, 1, external_id="t1")
    closed = log.ticket(
        "Renew the lease", None, 2, external_id="t1", operation=Operation.UPDATE, state=TicketState.DONE
    )

    [finding] = found(view(scenario(OWNER), log), TICKET_CLOSED_BY_THE_AGENT)

    assert finding.evidence == [closed.seq], "the agent closing the ticket through its API is a move to done"
