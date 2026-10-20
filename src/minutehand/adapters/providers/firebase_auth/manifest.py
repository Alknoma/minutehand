"""What the Firebase Authentication provider claims. Data only: nothing else in the provider is imported to read it.

Identity Toolkit v1 (`identitytoolkit.googleapis.com`: the Admin SDK's account management and the REST API's
sign-up and sign-in) and the Secure Token API (`securetoken.googleapis.com`: refreshing an ID token). The
certificates its ID tokens are signed with are published on `www.googleapis.com`, which Google Workspace claims, so
that one path is claimed apart (`Manifest.paths`), answered the same in every world.

A world is one Firebase project: an admin call names it in its path (`/v1/projects/{projectId}/`), an end user's
call by the web API key in its query (`?key=`), and a refresh by its refresh token or the same key.
"""

from __future__ import annotations

from minutehand.domain.provider import ClaimedPath, Manifest, Tier, WorldKey
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="firebase_auth",
    tier=Tier.FINISHED,
    hosts=["identitytoolkit.googleapis.com", "securetoken.googleapis.com"],
    kinds=[EntityKind.RECORD],
    world_keys=[WorldKey(path="/v1/projects/{key}/"), WorldKey(query="key")],
    paths=[
        ClaimedPath(
            host="www.googleapis.com",
            prefix="/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com",
            shared=True,
        )
    ],
)
