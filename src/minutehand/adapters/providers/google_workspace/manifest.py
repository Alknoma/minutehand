"""What the Google Workspace provider claims. Data only: nothing else in the provider is imported to read it.

One provider answers every Google Workspace API it serves, because they share hosts and sign-in: Calendar is
served at `www.googleapis.com/calendar/v3`, beside Drive, and every API takes the tokens `oauth2.googleapis.com`
issues. The proxy routes by host alone, so no second provider could claim Calendar. Google's identity
infrastructure beside them is here too (`identity.py`): IAM Credentials' signing, the OAuth2 certificates and the
metadata server's runtime service account.
"""

from __future__ import annotations

from minutehand.domain.items import CALENDAR_EVENT, COMMENT, DOCUMENT, EMAIL
from minutehand.domain.provider import ClaimedPath, DocumentChange, Manifest, Tier
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
        "metadata.google.internal",
    ],
    paths=[
        # Google's OAuth2 ID-token certificates: the same in every world, fetched with no credential.
        ClaimedPath(host="www.googleapis.com", prefix="/oauth2/v1/certs", shared=True),
        ClaimedPath(host="www.googleapis.com", prefix="/oauth2/v3/certs", shared=True),
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
    item_types=[DOCUMENT, COMMENT, EMAIL, CALENDAR_EVENT],
)
