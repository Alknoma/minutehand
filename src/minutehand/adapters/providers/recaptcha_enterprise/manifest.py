"""What the reCAPTCHA Enterprise provider claims. Data only: nothing else in the provider is imported to read it.

`FINISHED` covers what its tests exercise through Google's own discovery client (`google-api-python-client`) and
stock `google-auth`: `projects.assessments.create`, answered from the tokens the scenario seeds as a browser's
grecaptcha would have minted them. Every other method of the v1 API answers 501, naming it (see `CLAIMS.md`).
"""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="recaptcha_enterprise",
    title="reCAPTCHA Enterprise",
    tier=Tier.FINISHED,
    hosts=["recaptchaenterprise.googleapis.com"],
    kinds=[EntityKind.RECORD],
    # An event's `token` is the response token a browser's grecaptcha minted: the service's data, not a credential.
    data_keys=["token"],
)
