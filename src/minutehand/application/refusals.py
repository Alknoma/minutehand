"""What the run loop raises. Adapters raise `AgentFailed`; the loop turns it into `StopReason.AGENT_FAILED`."""

from __future__ import annotations

from collections.abc import Mapping

from minutehand.domain.provider import DocumentChange, Manifest, TicketField
from minutehand.domain.scenario import DocumentHappening, ProviderKey, Scenario, Seed


class AgentFailed(Exception):
    """The agent could not be reached, exited with an error, or answered with something that is not a report."""


class RunRefused(Exception):
    """The run cannot start or continue as configured: a missing provider, an unsupported person, no state hooks."""


def refuse_unheld(scenario: Scenario | Seed, manifests: Mapping[ProviderKey, Manifest]) -> None:
    """A seeded ticket that sets a field its provider cannot hold is refused, naming the ticket, and a document
    happening its provider cannot show is refused, naming the happening: the provider would drop the one in silence
    and could not do the other, and the scenario would claim a world that never existed. A provider not installed
    is refused elsewhere, by name."""
    for n, happening in enumerate(scenario.happenings, start=1):
        if not isinstance(happening, DocumentHappening):
            continue
        provider = scenario.happening_document(happening).provider
        manifest = manifests.get(provider)
        if manifest is not None and DocumentChange(happening.action.kind) not in manifest.document_changes:
            raise RunRefused(
                f"happening {n} ({happening.person} {happening.action.kind} the seeded document "
                f"{happening.document!r}) lands on {provider}, which has no way to show that"
            )
    for space in scenario.spaces:
        manifest = manifests.get(space.provider)
        if manifest is not None and not manifest.holds_spaces:
            raise RunRefused(f"the shared space {space.name!r} is on {space.provider}, which holds no shared spaces")
    for ticket in scenario.tickets:
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
