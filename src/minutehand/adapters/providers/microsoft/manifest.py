"""What the Microsoft provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier
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
)
