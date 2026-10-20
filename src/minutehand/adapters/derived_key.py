"""An RSA-2048 signing key derived from a fixed seed: the same in every world, every run, every fork and every
machine, and never written down.

A fake that publishes a signing key needs one its verifiers can cache: an agent keeps the keys it fetched (from
OpenID metadata, an x509 certificate list) for hours, and a token issued in a fork, or in another world of the same
server, must verify against the keys it already holds. So a token is told apart from another world's by its claims
(an audience, a tenant, a project), never by its key. Nothing about such a key is secret: it is a fake's, and
publishing it is its whole purpose.

Each provider names its own seed; one seed is one key.
"""

from __future__ import annotations

import base64
import hashlib
import random
from datetime import UTC, datetime
from functools import cached_property

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

PUBLIC_EXPONENT = 65537
_PRIME_BITS = 1024
_ROUNDS = 40
CERTIFICATE_FROM = datetime(2026, 1, 1, tzinfo=UTC)
"""The validity of a certificate made for a derived key: fixed, so the certificate's bytes are the same wherever it is
made. Verifiers of a JWT against an x509 certificate read only its key."""
CERTIFICATE_UNTIL = datetime(2036, 1, 1, tzinfo=UTC)


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


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _int_bytes(n: int) -> bytes:
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


class DerivedKey:
    """The RSA-2048 key `seed` names, derived on first use and kept for the process."""

    def __init__(self, seed: str) -> None:
        self.seed = seed

    @cached_property
    def private_key(self) -> rsa.RSAPrivateKey:
        draw = random.Random(self.seed)
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

    @cached_property
    def modulus(self) -> str:
        """The public modulus, base64url, as a JSON Web Key's `n`."""
        return b64url(_int_bytes(self.private_key.public_key().public_numbers().n))

    @cached_property
    def exponent(self) -> str:
        """The public exponent, base64url, as a JSON Web Key's `e`."""
        return b64url(_int_bytes(PUBLIC_EXPONENT))

    @cached_property
    def thumbprint(self) -> str:
        """A thumbprint of the public key, base64url: a `kid` a JSON Web Key Set can list it by."""
        return b64url(hashlib.sha256(f"{self.exponent}.{self.modulus}".encode()).digest()[:20])

    def sign(self, signing_input: bytes) -> bytes:
        """RS256 (RSASSA-PKCS1-v1_5 with SHA-256) over `signing_input`."""
        return self.private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())

    def certificate(self, common_name: str) -> str:
        """A self-signed x509 certificate of the public key, PEM, issued to and by `common_name`, valid from
        `CERTIFICATE_FROM` to `CERTIFICATE_UNTIL`; the same bytes wherever it is made."""
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        made = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.private_key.public_key())
            .serial_number(int(hashlib.sha256(self.seed.encode()).hexdigest()[:16], 16))
            .not_valid_before(CERTIFICATE_FROM)
            .not_valid_after(CERTIFICATE_UNTIL)
            .sign(self.private_key, hashes.SHA256())
        )
        return made.public_bytes(serialization.Encoding.PEM).decode("ascii")
