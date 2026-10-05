"""What the run loop raises. Adapters raise `AgentFailed`; the loop turns it into `StopReason.AGENT_FAILED`."""

from __future__ import annotations

from collections.abc import Mapping

from minutehand.domain.provider import Manifest, TicketField
from minutehand.domain.scenario import ProviderKey, SeededTicket


class AgentFailed(Exception):
    """The agent could not be reached, exited with an error, or answered with something that is not a report."""


class RunRefused(Exception):
    """The run cannot start or continue as configured: a missing provider, an unsupported person, no state hooks."""


def refuse_unheld_ticket_fields(tickets: list[SeededTicket], manifests: Mapping[ProviderKey, Manifest]) -> None:
    """A seeded ticket that sets a field its provider cannot hold is refused, naming the ticket: the provider would
    drop it in silence, and the scenario would claim a world that never existed. A provider not installed is
    refused elsewhere, by name."""
    for ticket in tickets:
        manifest = manifests.get(ticket.provider)
        if manifest is None:
            continue
        given = {TicketField.KEY: ticket.key is not None, TicketField.LABELS: bool(ticket.labels)}
        given[TicketField.COMMENTS] = bool(ticket.comments)
        unheld = [f.value for f, set_ in given.items() if set_ and f not in manifest.ticket_fields]
        if unheld:
            raise RunRefused(
                f"the seeded ticket {ticket.title!r} sets {', '.join(unheld)}, which {ticket.provider} tickets "
                "cannot hold"
            )
