"""The one key every token this provider issues is signed with, and the JSON Web Key Set that publishes it.

The key is the same in every world, every run, every fork and on every machine. That is deliberate: an agent caches
the signing keys it fetched from the OpenID metadata, and a token issued in a fork, or in another world of the same
server, must verify against the keys the agent already holds. A token is told apart from another world's by its
claims (tenant, app), never by its key. Nothing about this key is secret: it is a fake's, and publishing it is its
whole purpose.

Its two primes are written below. They were derived from a fixed seed by a search for primes in pure Python
(`tests/providers/microsoft/test_microsoft_key.py` derives them again and checks they are these), which took
seconds and ran on the provider's first token: an agent's first sign-in through Minutehand took about five seconds,
the default timeout of common HTTP clients.
"""

from __future__ import annotations

import base64
import hashlib
from functools import cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PUBLIC_EXPONENT = 65537

P = int(
    "f2c22d90355c22ef55c8e6192c9ad5b7043305e5a3745f1b63f2923c14f0da0b867d10d3234cc5960c9786e90c9a37a9"
    "72cc60c9adaeb7cae7abd27b73416c9b1c1b8fcf91cdd300d9b20199c3ffbcc88b6daafb01871706ef7e90356666618d"
    "99639af27dac02695012c7cafe43732a015d7ea9cacd509c7c0bae297a1aad4d",
    16,
)
"""The key's first prime, of 1024 bits."""

Q = int(
    "fbf1c1ce9cfef7dd9a2682287db3729df2f70c88dbe78cf88ae5f0d0da11222bce56bf0f52de50a1b4adc2944c51dab3"
    "09f211d12d55eea7f76b2bb4556a9497b9c1c3aac30d58c41c7febe6bc7dce71508ead91868593cd70bc684b5716750b"
    "bc9af9492d5a1ffedae4959db678fb036a22e26cdfefa6526dddef065abd6313",
    16,
)
"""The key's second prime, of 1024 bits."""


@cache
def private_key() -> rsa.RSAPrivateKey:
    """The provider's RSA-2048 key, from its two primes."""
    phi = (P - 1) * (Q - 1)
    d = pow(PUBLIC_EXPONENT, -1, phi)
    numbers = rsa.RSAPrivateNumbers(
        p=P,
        q=Q,
        d=d,
        dmp1=d % (P - 1),
        dmq1=d % (Q - 1),
        iqmp=pow(Q, -1, P),
        public_numbers=rsa.RSAPublicNumbers(PUBLIC_EXPONENT, P * Q),
    )
    return numbers.private_key()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _int_bytes(n: int) -> bytes:
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


@cache
def modulus() -> str:
    return b64url(_int_bytes(private_key().public_key().public_numbers().n))


@cache
def exponent() -> str:
    return b64url(_int_bytes(PUBLIC_EXPONENT))


@cache
def key_id() -> str:
    """The `kid` every token carries and the key set lists: a thumbprint of the public key."""
    return b64url(hashlib.sha256(f"{exponent()}.{modulus()}".encode()).digest()[:20])


def sign(signing_input: bytes) -> str:
    """RS256 over `signing_input`, base64url without padding."""
    signature = private_key().sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return b64url(signature)
