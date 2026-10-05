"""What the run loop raises. Adapters raise `AgentFailed`; the loop turns it into `StopReason.AGENT_FAILED`."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from minutehand.domain.provider import (
    AccountFact,
    ChannelField,
    DocumentChange,
    DocumentField,
    Manifest,
    MessagingKind,
    SpaceField,
    TicketActionKind,
    TicketField,
)
from minutehand.domain.scenario import (
    DocumentHappening,
    DocumentKind,
    ProviderKey,
    Scenario,
    Seed,
    SeededPost,
    TicketHappening,
)
from minutehand.domain.world import EntityKind


class AgentFailed(Exception):
    """The agent could not be reached, exited with an error, or answered with something that is not a report."""


class RunRefused(Exception):
    """The run cannot start or continue as configured: a missing provider, an unsupported person, no state hooks."""


def refuse_unheld(scenario: Scenario | Seed, manifests: Mapping[ProviderKey, Manifest]) -> None:
    """Whatever the seed declares that its provider cannot hold is refused, naming it: a ticket, document, space,
    sign-in or channel on a provider without them, a fact of one its provider does not keep, a person's account entry
    saying what its service cannot show, a happening its provider cannot land. The provider would drop each in
    silence, and the scenario would claim a world that never existed. A provider not installed is refused elsewhere,
    by name, except one only a person's account entry names, refused here."""
    for message in unheld(scenario, manifests):
        raise RunRefused(message)


def unheld(scenario: Scenario | Seed, manifests: Mapping[ProviderKey, Manifest]) -> list[str]:
    """Each thing `refuse_unheld` refuses, in words, in the order the seed declares them."""
    found: list[str] = []
    for n, happening in enumerate(scenario.happenings, start=1):
        provider = scenario.happening_provider(happening)
        manifest = manifests.get(provider)
        if manifest is None:
            continue
        if isinstance(happening, DocumentHappening):
            if not manifest.holds.document_change(DocumentChange(happening.action.kind)):
                found.append(
                    f"happening {n} ({happening.person} {happening.action.kind} the seeded document "
                    f"{happening.document!r}) lands on {provider}, which has no way to show that"
                )
        elif isinstance(happening, TicketHappening):
            if not manifest.holds.ticket_action(TicketActionKind(happening.action.kind)):
                found.append(
                    f"happening {n} ({happening.person} {happening.action.kind} the seeded ticket "
                    f"{happening.ticket!r}) lands on {provider}, which has no way to show that"
                )
        elif not manifest.holds.messaging(MessagingKind(happening.kind)):
            found.append(
                f"happening {n} ({happening.person} {happening.kind}) lands on {provider}, which has no way to show that"
            )
    for ticket in scenario.tickets:
        manifest = manifests.get(ticket.provider)
        if manifest is None:
            continue
        if EntityKind.TICKET not in manifest.kinds:
            found.append(f"the seeded ticket {ticket.title!r} is on {ticket.provider}, which holds no tickets")
            continue
        given = {
            TicketField.KEY: ticket.key is not None,
            TicketField.LABELS: bool(ticket.labels),
            TicketField.COMMENTS: bool(ticket.comments),
            TicketField.NUMBER: ticket.number is not None,
            TicketField.ID: ticket.id is not None,
        }
        unheld_fields = [f.value for f, set_ in given.items() if set_ and not manifest.holds.ticket_field(f)]
        if unheld_fields:
            found.append(
                f"the seeded ticket {ticket.title!r} sets {', '.join(unheld_fields)}, which {ticket.provider} "
                "tickets cannot hold"
            )
    for document in scenario.documents:
        manifest = manifests.get(document.provider)
        if manifest is None:
            continue
        if EntityKind.DOCUMENT not in manifest.kinds:
            found.append(f"the seeded document {document.title!r} is on {document.provider}, which holds no documents")
            continue
        facts = {
            DocumentField.SPREADSHEET: document.kind is DocumentKind.SPREADSHEET,
            DocumentField.PRESENTATION: document.kind is DocumentKind.PRESENTATION,
            DocumentField.FILE: document.kind is DocumentKind.FILE,
            DocumentField.FOLDER: document.folder is not None,
            DocumentField.OWNER: document.owner is not None,
            DocumentField.SPACE: document.space is not None,
            DocumentField.SHARED_WITH: bool(document.shared_with),
            DocumentField.MODIFIED_BEFORE_START: document.modified_before_start > timedelta(0),
            DocumentField.MODIFIED_BY: document.modified_by is not None,
            DocumentField.ID: document.id is not None,
        }
        missing = [f.value for f, set_ in facts.items() if set_ and not manifest.holds.document_field(f)]
        if missing:
            found.append(
                f"the seeded document {document.title!r} sets {', '.join(missing)}, which {document.provider} "
                "documents cannot hold"
            )
    for space in scenario.spaces:
        manifest = manifests.get(space.provider)
        if manifest is None:
            continue
        if not manifest.holds.space_field(SpaceField.SPACES):
            found.append(f"the shared space {space.name!r} is on {space.provider}, which holds no shared spaces")
        elif space.id is not None and not manifest.holds.space_field(SpaceField.ID):
            found.append(f"the shared space {space.name!r} sets id, which {space.provider} spaces cannot hold")
    for sign_in in scenario.sign_ins:
        manifest = manifests.get(sign_in.provider)
        if manifest is not None and not manifest.holds.sign_ins:
            found.append(f"a sign-in is declared for {sign_in.provider}, which takes no seeded sign-ins")
    for channel in scenario.channels:
        manifest = manifests.get(channel.provider)
        if manifest is None:
            continue
        where = f"#{channel.name}" if channel.name is not None else "the direct conversation"
        if EntityKind.CHANNEL not in manifest.kinds:
            found.append(f"the seeded channel {where} is on {channel.provider}, which holds no channels")
            continue
        posts = _every(channel.history)
        set_ = {
            ChannelField.NAMED: channel.name is not None,
            ChannelField.DIRECT: channel.name is None,
            ChannelField.PRIVATE: channel.private,
            ChannelField.ARCHIVED: channel.archived,
            ChannelField.TOPIC: bool(channel.topic),
            ChannelField.PURPOSE: bool(channel.purpose),
            ChannelField.WITHOUT_AGENT: not channel.agent_member,
            ChannelField.HISTORY: bool(channel.history),
            ChannelField.THREADS: any(p.replies for p in posts),
            ChannelField.FILES: any(p.files for p in posts),
            ChannelField.ID: channel.id is not None,
            ChannelField.POST_ID: any(p.id is not None for p in posts),
        }
        missing = [f.value for f, given in set_.items() if given and not manifest.holds.channel_field(f)]
        if missing:
            found.append(
                f"the seeded channel {where} sets {', '.join(missing)}, which {channel.provider} channels cannot hold"
            )
    for person in scenario.people:
        for account in person.accounts:
            manifest = manifests.get(account.provider)
            if manifest is None:
                found.append(
                    f"{person.key} has an account in {account.provider}, and no installed provider is named that"
                )
                continue
            facts_given = {
                AccountFact.LOGIN: account.login is not None,
                AccountFact.ID: account.id is not None,
                AccountFact.NAME: account.name is not None,
                AccountFact.EMAIL_HIDDEN: not account.email_visible,
            }
            missing = [f.value for f, given in facts_given.items() if given and not manifest.holds.account_fact(f)]
            if missing:
                found.append(
                    f"{person.key}'s account in {account.provider} sets {', '.join(missing)}, which "
                    f"{account.provider} accounts cannot hold"
                )
    return found


def _every(posts: list[SeededPost]) -> list[SeededPost]:
    return [found for post in posts for found in (post, *_every(post.replies))]
