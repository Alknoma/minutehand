"""What the Notion provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="notion",
    tier=Tier.FINISHED,
    hosts=["api.notion.com"],
    kinds=[EntityKind.DOCUMENT, EntityKind.RECORD, EntityKind.COMMENT],
)
