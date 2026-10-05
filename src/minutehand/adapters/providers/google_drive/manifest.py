"""What the Google Drive provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import (
    AccountFact,
    DocumentChange,
    DocumentField,
    Manifest,
    PersonFact,
    SpaceField,
    Tier,
)
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="google_drive",
    tier=Tier.FINISHED,
    hosts=[
        "www.googleapis.com",
        "oauth2.googleapis.com",
        "docs.googleapis.com",
        "slides.googleapis.com",
        "iamcredentials.googleapis.com",
    ],
    kinds=[EntityKind.DOCUMENT, EntityKind.COMMENT],
    document_changes=[
        DocumentChange.EDITED,
        DocumentChange.RENAMED,
        DocumentChange.MOVED,
        DocumentChange.SHARED,
        DocumentChange.TRASHED,
        DocumentChange.COMMENTED,
    ],
    document_fields=[
        DocumentField.SPREADSHEET,
        DocumentField.PRESENTATION,
        DocumentField.FILE,
        DocumentField.FOLDER,
        DocumentField.OWNER,
        DocumentField.SPACE,
        DocumentField.SHARED_WITH,
        DocumentField.MODIFIED_BEFORE_START,
        DocumentField.MODIFIED_BY,
        DocumentField.ID,
    ],
    space_fields=[SpaceField.SPACES, SpaceField.ID],
    sign_ins=True,
    account_facts=[AccountFact.ID, AccountFact.NAME, AccountFact.EMAIL_HIDDEN],
    person_facts=[PersonFact.WITHOUT_EMAIL],
)
