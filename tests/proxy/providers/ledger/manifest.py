from minutehand.domain.provider import Holds, Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    holds=Holds.nothing(),
    key="ledger",
    tier=Tier.FINISHED,
    hosts=["ledger.example", "*.ledger.test"],
    path_prefix="/api/v2",
    kinds=[EntityKind.RECORD],
)
