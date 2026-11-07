"""What the Firebase Cloud Messaging provider claims. Data only: nothing else in the provider is imported to read it.

`FINISHED` covers what its tests exercise through Google's own `firebase-admin` and its discovery client
(`google-api-python-client`): `projects.messages.send` of the FCM HTTP v1 API to a registration token, answered
from the registrations the scenario seeds. Every other method of the API answers 501, naming it (see `CLAIMS.md`).
"""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="firebase_cloud_messaging",
    title="Firebase Cloud Messaging",
    tier=Tier.FINISHED,
    hosts=["fcm.googleapis.com"],
    kinds=[EntityKind.RECORD],
    # A message's `token` is the device's registration token, the address it is sent to: not a credential.
    data_keys=["token"],
)
