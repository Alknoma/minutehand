"""What the YouTrack provider claims. Data only: nothing else in the provider is imported to read it.

YouTrack Cloud serves its REST API at `/api/...` on `*.youtrack.cloud` and at
`/youtrack/api/...` on the older `*.myjetbrains.com` instances. One `path_prefix`
cannot say both, so the manifest strips nothing and the app answers both path
shapes itself, on either host family.
"""

from __future__ import annotations

from minutehand.domain.provider import (
    AccountFact,
    Manifest,
    PersonChange,
    PersonFact,
    TicketActionKind,
    TicketField,
    Tier,
    WorldKey,
)
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="youtrack",
    tier=Tier.FINISHED,
    hosts=["*.youtrack.cloud", "*.myjetbrains.com"],
    kinds=[EntityKind.TICKET, EntityKind.COMMENT],
    ticket_fields=[TicketField.KEY, TicketField.LABELS, TicketField.COMMENTS, TicketField.NUMBER, TicketField.ID],
    world_keys=[WorldKey(host="{key}.youtrack.cloud"), WorldKey(host="{key}.myjetbrains.com")],
    people_changes=[PersonChange.DEACTIVATED, PersonChange.REACTIVATED],
    account_facts=[AccountFact.LOGIN, AccountFact.ID, AccountFact.NAME, AccountFact.EMAIL_HIDDEN],
    person_facts=[PersonFact.WITHOUT_EMAIL, PersonFact.DEACTIVATED],
    ticket_actions=[
        TicketActionKind.MOVES,
        TicketActionKind.REASSIGNS,
        TicketActionKind.COMMENTS,
        TicketActionKind.DELETES,
    ],
)
