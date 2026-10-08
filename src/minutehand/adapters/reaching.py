"""What a service in the run trusts when it reaches the agent over HTTPS, as a webhook push does: the bundle the agent
itself is handed, the public roots and the proxy's CA (`adapters.proxy.trust`). A receiver of the agent's serves a
certificate the run's CA signed, as a real one serves a certificate a public CA signed.

The running proxy names the bundle while it runs; with no proxy running, the public roots alone are trusted."""

from __future__ import annotations

import ssl
from pathlib import Path

_bundle: list[Path] = []


def trust(bundle: Path | None) -> None:
    """Name the bundle the running proxy hands agents; None when it stops."""
    _bundle.clear()
    if bundle is not None:
        _bundle.append(bundle)


def verify() -> ssl.SSLContext | bool:
    """What an HTTPS client of a service's verifies the agent's certificate against."""
    return ssl.create_default_context(cafile=str(_bundle[0])) if _bundle else True
