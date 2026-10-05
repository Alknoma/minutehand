"""The certificates an agent must trust: the proxy's own CA, and every public root besides.

An agent's HTTP library reads ONE file of trusted CAs from `SSL_CERT_FILE` (or its own variable), and that
file replaces the roots it would otherwise trust. A file holding only the proxy's CA verifies the hosts the
proxy answers, and breaks every call it tunnels to a real host, such as a model API, whose certificate is
signed by a public CA. So the file handed to the agent is a bundle: the public roots `certifi` carries, then
the proxy's CA.

The CA is made in the state directory the first time it is needed, by the proxy or by `minutehand env`, and
reused after, so an agent configured once keeps trusting every later run.
"""

from __future__ import annotations

from pathlib import Path

import certifi
from mitmproxy import certs
from mitmproxy.options import CONF_BASENAME

CA_CERT = f"{CONF_BASENAME}-ca-cert.pem"
BUNDLE = "minutehand-ca-bundle.pem"
KEY_SIZE = 2048
"""mitmproxy's own default, so a CA made here is the one the proxy would have made."""


def authority(confdir: Path) -> Path:
    """The proxy's CA certificate in `confdir`, made when it is not there yet."""
    confdir.mkdir(parents=True, exist_ok=True)
    certs.CertStore.from_store(confdir, CONF_BASENAME, KEY_SIZE)
    return confdir / CA_CERT


def write_bundle(confdir: Path, *, public_roots: str | None = None) -> Path:
    """The one file every CA variable names: `public_roots` (certifi's by default), then the proxy's CA.

    Rewritten on every call, so a newer certifi or a remade CA reaches the next run."""
    roots = certifi.contents() if public_roots is None else public_roots
    ours = authority(confdir).read_text(encoding="utf-8")
    bundle = confdir / BUNDLE
    bundle.write_text(roots.rstrip("\n") + "\n\n# Minutehand's proxy CA\n" + ours, encoding="utf-8")
    return bundle
