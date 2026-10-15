"""A certificate for an agent's HTTPS receiver that no CA anyone trusts signed: what an agent on a self-signed
certificate serves. Minutehand checks no receiver's certificate (`docs/design.md`), so a push to it is delivered."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from mitmproxy import certs
from mitmproxy.options import CONF_BASENAME

from minutehand.adapters.proxy.trust import KEY_SIZE


def untrusted_certificate(directory: Path) -> tuple[Path, Path]:
    """A certificate for 127.0.0.1 signed by a CA made in `directory` for it alone, and its key.
    (certificate.pem, key.pem)"""
    directory.mkdir(parents=True, exist_ok=True)
    store = certs.CertStore.from_store(directory, CONF_BASENAME, KEY_SIZE)
    entry = store.get_cert("127.0.0.1", [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    cert, key = directory / "receiver.pem", directory / "receiver-key.pem"
    cert.write_bytes(entry.cert.to_pem())
    key.write_bytes(
        entry.privatekey.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return cert, key
