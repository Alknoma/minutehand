"""Which provider answers which host. There is no registration list.

A provider is a package with two modules: `manifest.py` exposing `MANIFEST: Manifest`
(data only) and `provider.py` exposing `build() -> Provider`. Every such package under
`minutehand.adapters.providers` is found by walking the directory, and an installed
package names itself under the entry-point group `minutehand.providers` with its
package as the value (`ledger = "minutehand_provider_ledger"`).

Manifests load when the registry is built. `provider.py` is imported on the first
request to one of the provider's hosts, and not before.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points

from minutehand.adapters.proxy.hosts import HostPattern
from minutehand.domain.provider import Manifest
from minutehand.ports.provider import Provider

BUILTIN_PACKAGE = "minutehand.adapters.providers"
ENTRY_POINT_GROUP = "minutehand.providers"


class ProviderConflict(ValueError):
    """Two providers claim the same key, an overlapping host or an overlapping path."""


@dataclass
class _Entry:
    manifest: Manifest
    patterns: list[HostPattern]
    build: Callable[[], Provider]
    provider: Provider | None = field(default=None)


class Registry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    @classmethod
    def installed(cls) -> Registry:
        """Every provider shipped in this package and every one installed under the entry-point group."""
        registry = cls()
        registry.discover(BUILTIN_PACKAGE)
        registry.load_entry_points(entry_points(group=ENTRY_POINT_GROUP))
        return registry

    @property
    def manifests(self) -> list[Manifest]:
        return [entry.manifest for entry in self._entries.values()]

    def register(self, manifest: Manifest, build: Callable[[], Provider]) -> None:
        """Add one provider. `build` is called on the first request to one of its hosts."""
        if manifest.key in self._entries:
            raise ProviderConflict(f"two providers are named {manifest.key!r}")
        patterns = [HostPattern(host) for host in manifest.hosts]
        for entry in self._entries.values():
            for mine in patterns:
                for theirs in entry.patterns:
                    if mine.overlaps(theirs) and mine.is_wildcard == theirs.is_wildcard:
                        raise ProviderConflict(
                            f"{manifest.key!r} claims {mine.text!r}, which {entry.manifest.key!r} already claims"
                            f" as {theirs.text!r}"
                        )
        for entry in self._entries.values():
            for mine_path in manifest.paths:
                for theirs_path in entry.manifest.paths:
                    if mine_path.host == theirs_path.host and (
                        mine_path.prefix.startswith(theirs_path.prefix)
                        or theirs_path.prefix.startswith(mine_path.prefix)
                    ):
                        raise ProviderConflict(
                            f"{manifest.key!r} claims {mine_path.host}{mine_path.prefix}, which {entry.manifest.key!r}"
                            f" already claims as {theirs_path.host}{theirs_path.prefix}"
                        )
        self._entries[manifest.key] = _Entry(manifest=manifest, patterns=patterns, build=build)

    def discover(self, package: str) -> None:
        """Register every subpackage of `package` that has a `manifest.py`."""
        parent = importlib.import_module(package)
        for found in pkgutil.iter_modules(parent.__path__):
            if found.ispkg and importlib.util.find_spec(f"{package}.{found.name}.manifest") is not None:
                self._load_package(f"{package}.{found.name}")

    def load_entry_points(self, points: Iterable[EntryPoint]) -> None:
        for point in points:
            manifest = self._load_package(point.module)
            if manifest.key != point.name:
                raise ProviderConflict(f"entry point {point.name!r} names a package whose manifest is {manifest.key!r}")

    def _load_package(self, package: str) -> Manifest:
        manifest = importlib.import_module(f"{package}.manifest").MANIFEST
        if not isinstance(manifest, Manifest):
            raise TypeError(f"{package}.manifest.MANIFEST is a {type(manifest).__name__}, not a Manifest")
        if importlib.util.find_spec(f"{package}.provider") is None:
            raise ImportError(f"{package} has a manifest but no provider.py")

        def build() -> Provider:
            provider: Provider = importlib.import_module(f"{package}.provider").build()
            return provider

        self.register(manifest, build)
        return manifest

    def claimant(self, host: str) -> Manifest | None:
        """The manifest of the provider that answers `host`, if any does: one claiming it exactly before one claiming it
        under a wildcard (a vendor's own bucket inside a cloud's domain is the vendor's)."""
        found = [
            (pattern.is_wildcard, entry.manifest)
            for entry in self._entries.values()
            for pattern in entry.patterns
            if pattern.matches(host)
        ]
        return min(found, key=lambda claim: claim[0])[1] if found else None

    def answering(self, host: str, path: str) -> Manifest | None:
        """The manifest of the provider that answers a request to `host` at `path`: one claiming the path
        (`Manifest.paths`) before one claiming the host, since a path under another provider's host is claimed only
        to be answered apart from it."""
        found = [
            (len(claimed.prefix), entry.manifest)
            for entry in self._entries.values()
            for claimed in entry.manifest.paths
            if claimed.matches(host, path)
        ]
        return max(found, key=lambda claim: claim[0])[1] if found else self.claimant(host)

    def claims_paths_under(self, host: str) -> bool:
        """Whether any provider claims a path under `host` (`Manifest.paths`)."""
        return any(claimed.host == host.lower() for entry in self._entries.values() for claimed in entry.manifest.paths)

    def provider(self, manifest: Manifest) -> Provider:
        """The provider for a manifest this registry holds, built on first use."""
        entry = self._entries[manifest.key]
        if entry.provider is None:
            provider = entry.build()
            if provider.manifest != entry.manifest:
                raise ProviderConflict(f"{manifest.key!r} was registered with one manifest and built with another")
            entry.provider = provider
        return entry.provider
