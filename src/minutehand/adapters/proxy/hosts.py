"""Host patterns: an exact host, or `*.` followed by a domain meaning any host under it."""

from __future__ import annotations

from dataclasses import dataclass

WILDCARD = "*."


@dataclass(frozen=True)
class HostPattern:
    text: str

    def __post_init__(self) -> None:
        body = self.text[len(WILDCARD):] if self.is_wildcard else self.text
        if not body or "*" in body or body != body.lower() or body.startswith(".") or body.endswith("."):
            raise ValueError(f"not a host pattern: {self.text!r} (an exact lower-case host, or '*.' and a domain)")

    @property
    def is_wildcard(self) -> bool:
        return self.text.startswith(WILDCARD)

    @property
    def domain(self) -> str:
        """The exact host, or the domain a wildcard covers the subdomains of."""
        return self.text[len(WILDCARD):] if self.is_wildcard else self.text

    def matches(self, host: str) -> bool:
        host = host.lower()
        if self.is_wildcard:
            return host.endswith("." + self.domain)
        return host == self.domain

    def overlaps(self, other: HostPattern) -> bool:
        """True when some host would match both."""
        if not self.is_wildcard and not other.is_wildcard:
            return self.domain == other.domain
        if self.is_wildcard and other.is_wildcard:
            return self.matches(other.domain) or other.matches(self.domain) or self.domain == other.domain
        exact, wild = (other, self) if self.is_wildcard else (self, other)
        return wild.matches(exact.domain)
