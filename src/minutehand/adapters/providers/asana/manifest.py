"""What the Asana provider claims. Data only: nothing else in the provider is imported to read it.

`FINISHED` covers the surface an agent that hands work to people touches, and nothing
else of Asana: users, the workspace, projects and their sections, tasks (create, read,
update, delete, list, search, typeahead) and comment stories. A field or parameter
Asana has and this provider does not is answered 501 by name, never ignored.
"""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="asana",
    tier=Tier.FINISHED,
    hosts=["app.asana.com"],
    path_prefix="/api/1.0",
    kinds=[EntityKind.TICKET, EntityKind.COMMENT],
)
