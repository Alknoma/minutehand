"""What the Notion provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.items import COMMENT, DOCUMENT
from minutehand.domain.provider import DocumentChange, Manifest, PersonChange, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="notion",
    tier=Tier.FINISHED,
    hosts=["api.notion.com"],
    kinds=[EntityKind.DOCUMENT, EntityKind.RECORD, EntityKind.COMMENT],
    document_changes=[
        DocumentChange.EDITED,
        DocumentChange.RENAMED,
        DocumentChange.TRASHED,
        DocumentChange.COMMENTED,
        DocumentChange.FIELD_SET,
    ],
    people_changes=[PersonChange.REMOVED],
    item_types=[DOCUMENT, COMMENT],
)
