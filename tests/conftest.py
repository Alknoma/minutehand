"""What every test shares: one proxy CA per worker.

A proxy started in this process makes its CA in its `confdir` when there is none (`adapters.proxy.trust`), and most
tests give it a fresh directory, so each made a 2048-bit RSA key: about 60 ms, on a thousand proxy starts. Here the
first CA a worker makes is kept and every later one is a copy of its files, so a directory still starts with no CA
and ends up with a CA in mitmproxy's own files. A test about the CA being made at all is marked `own_ca` and makes
its own. Processes a test starts (`minutehand run`, `minutehand serve`) are not touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mitmproxy import certs

_MAKE = certs.CertStore.create_store
_made: dict[tuple[str, int, str | None, str | None], Path] = {}


@pytest.fixture(autouse=True)
def _one_proxy_ca_per_worker(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    if request.node.get_closest_marker("own_ca") is not None:
        return

    def copy_of_the_workers(
        path: Path, basename: str, key_size: int, organization: str | None = None, cn: str | None = None
    ) -> None:
        key = (basename, key_size, organization, cn)
        if key not in _made:
            made = tmp_path_factory.mktemp("proxy-ca")
            _MAKE(made, basename, key_size, organization, cn)
            _made[key] = made
        path.mkdir(parents=True, exist_ok=True)
        for kept in _made[key].iterdir():
            shutil.copy2(kept, path / kept.name)

    monkeypatch.setattr(certs.CertStore, "create_store", staticmethod(copy_of_the_workers))
