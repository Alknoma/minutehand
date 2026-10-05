"""The ledger fake claims one host. Data only."""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier

MANIFEST = Manifest(key="ledger", tier=Tier.FINISHED, hosts=["ledger.example"])
