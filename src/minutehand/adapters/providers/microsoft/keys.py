"""The one key every token this provider issues is signed with, and the JSON Web Key Set that publishes it.

The key is derived from a fixed seed (`adapters.derived_key`), so it is the same in every world, every run and
every fork, and no private key is ever written down: an agent caches the signing keys it fetched from the OpenID
metadata, and a token issued in a fork, or in another world of the same server, must verify against the keys the
agent already holds. A token is told apart from another world's by its claims (tenant, app), never by its key.
"""

from __future__ import annotations

from minutehand.adapters.derived_key import DerivedKey

KEY = DerivedKey("minutehand microsoft provider signing key, version 1")
