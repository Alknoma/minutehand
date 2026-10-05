"""What the GitHub provider claims. Data only: nothing else in the provider is imported to read it.

GitHub serves its REST API and its GraphQL endpoint (`/graphql`) at the root of `api.github.com`, so nothing is
stripped. Only the reads a code-reading client makes are answered (see `README.md`), so the provider maps no
entity kind: everything it holds is a record.
"""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier

MANIFEST = Manifest(
    key="github",
    tier=Tier.FINISHED,
    hosts=["api.github.com"],
)
