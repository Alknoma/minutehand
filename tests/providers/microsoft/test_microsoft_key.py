"""The provider's signing key is the one derived from its seed, and building it costs a sign-in nothing."""

from __future__ import annotations

import random
import time

from minutehand.adapters.providers.microsoft import keys

SEED = "minutehand microsoft provider signing key, version 1"
PRIME_BITS = 1024
ROUNDS = 40

KEY_ID = "Zb6tQpx-bfOlAIIG-f7r-ko5WHw"
"""The `kid` agents have cached from every Minutehand that published this key."""


def probably_prime(n: int, draw: random.Random) -> bool:
    if n < 4:
        return n in (2, 3)
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % small == 0:
            return n == small
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(ROUNDS):
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


def prime(draw: random.Random) -> int:
    while True:
        candidate = draw.getrandbits(PRIME_BITS) | (1 << (PRIME_BITS - 1)) | (1 << (PRIME_BITS - 2)) | 1
        if candidate % keys.PUBLIC_EXPONENT != 1 and probably_prime(candidate, draw):
            return candidate


def test_the_primes_are_the_ones_the_seed_derives() -> None:
    """The search the provider once ran on its first token, run here: the key every earlier version published."""
    draw = random.Random(SEED)
    p = prime(draw)
    q = prime(draw)
    while q == p:
        q = prime(draw)
    assert (p, q) == (keys.P, keys.Q)
    assert keys.key_id() == KEY_ID


def test_building_the_key_takes_no_search() -> None:
    """A first sign-in waited on the key; built from its primes it is ready in well under a second."""
    keys.private_key.cache_clear()
    started = time.monotonic()
    keys.private_key()
    assert time.monotonic() - started < 1.0
