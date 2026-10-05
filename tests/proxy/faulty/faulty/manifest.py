from minutehand.domain.provider import Holds, Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    holds=Holds.nothing(),
    key="faulty",
    tier=Tier.FINISHED,
    hosts=["faulty.example"],
    path_prefix="",
    kinds=[EntityKind.RECORD],
)
