"""What the Jira provider claims. Data only: nothing else in the provider is imported to read it.

A Jira Cloud site answers at `https://<site>.atlassian.net/rest/...`. An OAuth 2.0 (3LO) app reaches the
same site at `https://api.atlassian.com/ex/jira/<cloudId>/rest/...`, lists the sites its token reaches at
`api.atlassian.com/oauth/token/accessible-resources`, and refreshes its token at `auth.atlassian.com`. The
paths differ by host, so the manifest strips nothing and the app reads both shapes.
"""

from __future__ import annotations

from minutehand.domain.provider import Manifest, TicketField, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="jira",
    tier=Tier.FINISHED,
    hosts=["*.atlassian.net", "api.atlassian.com", "auth.atlassian.com"],
    kinds=[EntityKind.TICKET, EntityKind.COMMENT],
    ticket_fields=[TicketField.KEY, TicketField.LABELS, TicketField.COMMENTS],
)
