"""What the Google Workspace provider claims. Data only: nothing else in the provider is imported to read it.

One provider answers every Google Workspace API it serves, because they share hosts and sign-in: Calendar is
served at `www.googleapis.com/calendar/v3`, beside Drive, and every API takes the tokens `oauth2.googleapis.com`
issues. The proxy routes by host alone, so no second provider could claim Calendar.
"""

from __future__ import annotations

from minutehand.domain.provider import DocumentChange, Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="google_workspace",
    tier=Tier.FINISHED,
    hosts=[
        "www.googleapis.com",
        "oauth2.googleapis.com",
        "gmail.googleapis.com",
        "docs.googleapis.com",
        "slides.googleapis.com",
        "iamcredentials.googleapis.com",
    ],
    kinds=[EntityKind.DOCUMENT, EntityKind.COMMENT, EntityKind.MESSAGE],
    holds_spaces=True,
    document_changes=[
        DocumentChange.EDITED,
        DocumentChange.RENAMED,
        DocumentChange.MOVED,
        DocumentChange.SHARED,
        DocumentChange.TRASHED,
        DocumentChange.COMMENTED,
    ],
)
