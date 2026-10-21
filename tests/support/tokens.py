"""A JWT checked at a given moment: what a bot inside the sandbox does, whose clock is the run's. PyJWT reads this
machine's clock for `exp` and `nbf`; a token Minutehand issues at the run's clock is checked here against the run's
clock instead, with the Bot Framework SDK's 300 s of leeway, raising PyJWT's own errors."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import jwt

LEEWAY_SECONDS = 300


def decode_at(
    token: str, key: Any, *, audience: str, issuer: str | list[str], now: datetime, require: list[str]
) -> dict[str, Any]:
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=audience,
        issuer=issuer,
        options={"require": require, "verify_exp": False, "verify_nbf": False, "verify_iat": False},
    )
    at = now.timestamp()
    if "nbf" in claims and claims["nbf"] > at + LEEWAY_SECONDS:
        raise jwt.ImmatureSignatureError("The token is not yet valid (nbf)")
    if "exp" in claims and claims["exp"] <= at - LEEWAY_SECONDS:
        raise jwt.ExpiredSignatureError("Signature has expired")
    return claims
