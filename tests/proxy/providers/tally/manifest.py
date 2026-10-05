from minutehand.domain.provider import Holds, Manifest, Tier

MANIFEST = Manifest(holds=Holds.nothing(), key="tally", tier=Tier.FINISHED, hosts=["tally.test"])
