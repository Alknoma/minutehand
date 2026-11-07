from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="ledger",
    title="Ledger",
    tier=Tier.FINISHED,
    hosts=["ledger.example", "*.ledger.test"],
    path_prefix="/api/v2",
    kinds=[EntityKind.RECORD],
)
