"""The one key every token this provider issues is signed with, and the JSON Web Key Set that publishes it.

The key is derived from a fixed seed, so it is the same in every world, every run and every fork, and no
private key is ever written down. That is deliberate: an agent caches the signing keys it fetched from the
OpenID metadata, and a token issued in a fork, or in another world of the same server, must verify against the
keys the agent already holds. A token is told apart from another world's by its claims (tenant, app), never by
its key. Nothing about this key is secret: it is a fake's, and publishing it is its whole purpose.
"""

from __future__ import annotations

import base64
import hashlib
import random
from functools import cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

SEED = "minutehand microsoft provider signing key, version 1"
PUBLIC_EXPONENT = 65537
_PRIME_BITS = 1024
_ROUNDS = 40


def _probably_prime(n: int, draw: random.Random) -> bool:
    if n < 4:
        return n in (2, 3)
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % small == 0:
            return n == small
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(_ROUNDS):
        x = pow(draw.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(draw: random.Random) -> int:
    while True:
        candidate = draw.getrandbits(_PRIME_BITS) | (1 << (_PRIME_BITS - 1)) | (1 << (_PRIME_BITS - 2)) | 1
        if candidate % PUBLIC_EXPONENT != 1 and _probably_prime(candidate, draw):
            return candidate


@cache
def private_key() -> rsa.RSAPrivateKey:
    """The provider's RSA-2048 key, the same on every machine: derived, never stored."""
    draw = random.Random(SEED)
    p = _prime(draw)
    q = _prime(draw)
    while q == p:
        q = _prime(draw)
    phi = (p - 1) * (q - 1)
    d = pow(PUBLIC_EXPONENT, -1, phi)
    public = rsa.RSAPublicNumbers(PUBLIC_EXPONENT, p * q)
    numbers = rsa.RSAPrivateNumbers(
        p=p,
        q=q,
        d=d,
        dmp1=d % (p - 1),
        dmq1=d % (q - 1),
        iqmp=pow(q, -1, p),
        public_numbers=public,
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
