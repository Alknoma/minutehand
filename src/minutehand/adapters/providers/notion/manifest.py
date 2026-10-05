"""What the Notion provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import (
    AccountFact,
    DocumentChange,
    DocumentField,
    Manifest,
    PersonChange,
    PersonFact,
    Tier,
)
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
    document_fields=[
        DocumentField.FOLDER,
        DocumentField.OWNER,
        DocumentField.MODIFIED_BEFORE_START,
        DocumentField.MODIFIED_BY,
        DocumentField.ID,
    ],
    account_facts=[AccountFact.ID, AccountFact.NAME, AccountFact.EMAIL_HIDDEN],
    person_facts=[PersonFact.WITHOUT_EMAIL],
)
