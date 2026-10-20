"""A Google account's numeric id, as every Google API that names an account answers it: OAuth2's `userinfo` `id`,
an OpenID ID token's `sub`, Drive's `permissionId` (zero-padded), Firebase Authentication's `google.com` provider
`rawId`. Google derives none of these from anything a caller can see; here each is derived from the account's
address, so every provider that names a world's Google account names it alike, and none reads another's state.
"""

from __future__ import annotations

import hashlib


def account_id(email: str) -> str:
    """The account's id: decimal digits, the same for an address in any letter case."""
    return str(int(hashlib.sha256(f"permission\x1f{email.lower()}".encode()).hexdigest()[:15], 16))
