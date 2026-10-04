"""What the Google Drive provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="google_drive",
    tier=Tier.FINISHED,
    hosts=["www.googleapis.com", "oauth2.googleapis.com", "docs.googleapis.com"],
    kinds=[EntityKind.DOCUMENT, EntityKind.COMMENT],
)
