"""What the Slack provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import Manifest, PersonChange, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="slack",
    tier=Tier.FINISHED,
    hosts=["slack.com", "*.slack.com"],
    kinds=[EntityKind.MESSAGE, EntityKind.CHANNEL],
    pushes_events=True,
    books_work=True,
    people_changes=[PersonChange.DEACTIVATED, PersonChange.REACTIVATED],
)
