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

MODEL_INFRASTRUCTURE_HOSTS: tuple[str, ...] = (
    "mcp-proxy.anthropic.com",
    "*.mcp.claude.com",
    "platform.claude.com",
)
"""Hosts a model vendor's own client calls beside its model API, whatever the run's model hosts: Claude Code's relay
to the account's remote MCP connectors, Anthropic's own MCP servers, and the sign-in's token refresh. Each is
tunnelled, or recorded under `--record-model-calls`, as a model host is, and never edited: what crosses is not a
model call."""

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
        self._infrastructure = [HostPattern(host) for host in MODEL_INFRASTRUCTURE_HOSTS]
        for pattern in [*self._model_hosts, *self._infrastructure]:
            self._refuse_claimed(pattern)
        # Model hosts a world of `minutehand serve` declared while open, each with whether its calls are recorded.
        self._declared: dict[str, tuple[HostPattern, bool]] = {}
        self.edits: list[ModelEdit] = [o for o in overrides if isinstance(o, PromptPatch | ModelSwap)]

    def _refuse_claimed(self, pattern: HostPattern) -> None:
        for manifest in self.registry.manifests:
            for claimed in manifest.hosts:
                if pattern.overlaps(HostPattern(claimed)):
                    raise ProviderConflict(f"{pattern.text!r} is a model host and {manifest.key!r} claims {claimed!r}")

    @property
    def model_hosts(self) -> list[str]:
        """Every host that is a model API now: those the routing was built with, the vendors' own infrastructure
        (`MODEL_INFRASTRUCTURE_HOSTS`), and those declared since."""
        return [p.text for p in [*self._model_hosts, *self._infrastructure]] + list(self._declared)

    def declare(self, host: str, *, record: bool) -> None:
        """A model host declared while the proxy runs (a world of `minutehand serve`), tunnelled or, with `record`,
        opened and kept as spans. Refused (`ProviderConflict`) when a provider claims it or it overlaps a host
        already declared; `ValueError` when it is no host pattern."""
        pattern = HostPattern(host)
        self._refuse_claimed(pattern)
        taken = [text for text, (other, _) in self._declared.items() if other.overlaps(pattern)]
        if taken:
            raise ProviderConflict(f"{host!r} overlaps the model host {taken[0]!r} another world declared")
        self._declared[host] = (pattern, record)

    def withdraw(self, host: str) -> None:
        """A declared model host no longer one: the world that declared it closed."""
        del self._declared[host]

    def declared(self, host: str) -> str | None:
        """The declared model host pattern `host` falls under, if one does."""
        return next((text for text, (pattern, _) in self._declared.items() if pattern.matches(host)), None)

    def records(self, host: str) -> bool:
        """Whether the declaration `host` falls under asks for its calls to be kept as spans."""
        found = self.declared(host)
        return found is not None and self._declared[found][1]

    def apply(self, run_id: str, overrides: list[ModelEdit]) -> None:
        """`application.rewind.OnTheWire`: the model edits for the run about to play. One run plays at a time
        through one proxy, so the edits are the proxy's until the next call; `[]` sends model calls on untouched."""
        self.edits = list(overrides)

    def is_model_host(self, host: str) -> bool:
        return self._edited(host) or any(pattern.matches(host) for pattern in self._infrastructure)

    def _edited(self, host: str) -> bool:
        """A model API proper, whose calls a run's edits apply to; a vendor's infrastructure host is not one."""
        return any(pattern.matches(host) for pattern in self._model_hosts) or self.declared(host) is not None

    def edits_for(self, host: str) -> list[ModelEdit]:
        if not self._edited(host):
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
