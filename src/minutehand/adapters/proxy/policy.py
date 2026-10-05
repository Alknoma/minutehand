"""What the proxy does with a call, decided by its host alone."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from minutehand.adapters.proxy.hosts import HostPattern
from minutehand.adapters.proxy.registry import ProviderConflict, Registry
from minutehand.domain.experiment import ModelSwap, Override, PromptPatch
from minutehand.domain.provider import Manifest

DEFAULT_MODEL_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
)

ModelEdit = PromptPatch | ModelSwap


class HostPolicy(StrEnum):
    ANSWER = "answer"  # a provider claims the host and its fake answers
    TUNNEL = "tunnel"  # a model API with nothing to change: bytes pass through, never decrypted
    EDIT = "edit"  # a model API the run changes: decrypted, edited, sent on upstream
    RECORD = "record"  # a model API the run records: decrypted, sent on unchanged, kept as a span
    REFUSE = "refuse"  # nobody claims it: 502, recorded


class Routing:
    def __init__(
        self,
        registry: Registry,
        *,
        model_hosts: Sequence[str] = DEFAULT_MODEL_HOSTS,
        overrides: Sequence[Override] = (),
    ) -> None:
        self.registry = registry
        self._model_hosts = [HostPattern(host) for host in model_hosts]
        for pattern in self._model_hosts:
            for manifest in registry.manifests:
                for claimed in manifest.hosts:
                    if pattern.overlaps(HostPattern(claimed)):
                        raise ProviderConflict(
                            f"{pattern.text!r} is a model host and {manifest.key!r} claims {claimed!r}"
                        )
        self.edits: list[ModelEdit] = [o for o in overrides if isinstance(o, PromptPatch | ModelSwap)]

    def apply(self, run_id: str, overrides: list[ModelEdit]) -> None:
        """`application.rewind.OnTheWire`: the model edits for the run about to play. One run plays at a time
        through one proxy, so the edits are the proxy's until the next call; `[]` sends model calls on untouched."""
        self.edits = list(overrides)

    def is_model_host(self, host: str) -> bool:
        return any(pattern.matches(host) for pattern in self._model_hosts)

    def edits_for(self, host: str) -> list[ModelEdit]:
        if not self.is_model_host(host):
            return []
        return [e for e in self.edits if e.where.host is None or e.where.host.lower() == host.lower()]

    def policy(self, host: str) -> HostPolicy:
        if self.claimant(host) is not None:
            return HostPolicy.ANSWER
        if self.is_model_host(host):
            return HostPolicy.EDIT if self.edits_for(host) else HostPolicy.TUNNEL
        return HostPolicy.REFUSE

    def claimant(self, host: str) -> Manifest | None:
        return self.registry.claimant(host)
