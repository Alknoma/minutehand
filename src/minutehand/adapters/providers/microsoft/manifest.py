"""What the Microsoft provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import DocumentChange, Manifest, Tier, WorldKey
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="microsoft",
    tier=Tier.FINISHED,
    hosts=[
        "login.microsoftonline.com",
        "login.botframework.com",
        "smba.trafficmanager.net",
        "graph.microsoft.com",
        "*.sharepoint.com",
    ],
    kinds=[EntityKind.MESSAGE, EntityKind.CHANNEL, EntityKind.DOCUMENT],
    pushes_events=True,
    world_keys=[
        WorldKey(path="/{key}/oauth2/v2.0/"),
        WorldKey(path="/{key}/v2.0/.well-known/"),
        WorldKey(path="/{key}/discovery/"),
        WorldKey(host="{key}.sharepoint.com"),
        WorldKey(host="{key}-my.sharepoint.com"),
    ],
    document_changes=[
        DocumentChange.EDITED,
        DocumentChange.RENAMED,
        DocumentChange.MOVED,
        DocumentChange.SHARED,
        DocumentChange.TRASHED,
    ],
)
