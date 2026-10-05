"""What a provider declares about itself. Data only: loading a manifest imports no provider code."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import Field, JsonValue, model_validator

from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import EntityKind


class Tier(StrEnum):
    """How much of the real service a provider reproduces."""

    OBSERVED = "observed"  # calls pass through to the real service and are recorded
    GENERATED = "generated"  # stateful create/read/update/delete built from the service's API description
    TAUGHT = "taught"  # generated, then corrected against recorded real traffic
    FINISHED = "finished"  # hand-finished: refusals, pushed events, sign-in, known quirks


class TicketField(StrEnum):
    """A `SeededTicket` field not every ticket provider can hold."""

    KEY = "key"  # the scenario's own name for the ticket, by which the provider's own seed names it
    LABELS = "labels"
    COMMENTS = "comments"


class DocumentChange(StrEnum):
    """What a person can do to a seeded document (`DocumentHappening.action`); not every document provider can show
    every one."""

    EDITED = "edited"
    RENAMED = "renamed"
    MOVED = "moved"
    SHARED = "shared"
    TRASHED = "trashed"
    COMMENTED = "commented"
    FIELD_SET = "field_set"


class PersonChange(StrEnum):
    """What can happen to a person's account while a world is open; not every provider can show every one."""

    REMOVED = "removed"  # gone from the workspace, team or tenant: the account answers as not found
    DEACTIVATED = "deactivated"  # still there, but can no longer sign in, be assigned or be messaged
    REACTIVATED = "reactivated"  # a deactivated account active again


class WorldKey(Model):
    """A part of a request's URL that names whose service it is, with no credential needed to say so: a site's
    own host label (`acme` of `acme.atlassian.net`), or a path segment (a Microsoft tenant in
    `/{tenant}/oauth2/v2.0/token`, an Atlassian cloud id in `/ex/jira/{cloudId}/`). `minutehand serve` routes a
    call carrying one to the world that claims that key, as it routes a token. Exactly one of the two."""

    host: str | None = Field(
        default=None, pattern=r"^[^/]*\{key\}[^/]*$", description="A host with `{key}` in place of one label"
    )
    path: str | None = Field(
        default=None,
        pattern=r"^/.*\{key\}",
        description="A path prefix with `{key}` in place of one segment; the query is not read",
    )

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.host is None) == (self.path is None):
            raise ValueError("a world key is read from the host or from the path; give exactly one")
        return self

    def found(self, host: str, path: str) -> str | None:
        """The key this request's host or path carries, or None when it does not have this shape."""
        if self.host is not None:
            pattern = re.escape(self.host.lower()).replace(re.escape("{key}"), "([^./]+)")
            matched = re.fullmatch(pattern, host.lower())
        else:
            assert self.path is not None
            pattern = re.escape(self.path).replace(re.escape("{key}"), "([^/?#]+)")
            matched = re.match(pattern, path.split("?", 1)[0])
        return matched.group(1) if matched else None


class Manifest(Model):
    key: ProviderKey
    tier: Tier
    hosts: list[str] = Field(min_length=1, description="Exact hosts or '*.' wildcards this provider answers for")
    path_prefix: str = Field(default="", description="Prefix the real API serves under, e.g. '/api/1.0'")
    kinds: list[EntityKind] = Field(default=[], description="Entity kinds it maps; empty means records only")
    pushes_events: bool = False
    books_wakes: bool = Field(
        default=False, description="True for a scheduler: what the agent books here becomes a wake"
    )
    ticket_fields: list[TicketField] = Field(
        default=[],
        description="The optional `SeededTicket` fields its tickets hold; a scenario that sets any other on one of "
        "its tickets is refused at load, since the provider would drop it in silence",
    )
    world_keys: list[WorldKey] = Field(
        default=[],
        description="Where a request to this provider names its world without a credential: per-customer hosts, "
        "tenant or site ids in the path",
    )
    document_changes: list[DocumentChange] = Field(
        default=[],
        description="What a person can do to its seeded documents; a scenario whose document happening does any "
        "other is refused at load, since the provider could not show it",
    )
    people_changes: list[PersonChange] = Field(
        default=[],
        description="What can happen to a person's account here while a world is open (`ChangesPeople`); any other "
        "is refused at the call, naming these",
    )


def world_keys(manifest: Manifest, host: str, path: str) -> list[str]:
    """Every world key a request to this provider carries in its host or path, in the manifest's order."""
    found = [k.found(host, path) for k in manifest.world_keys]
    return list(dict.fromkeys(k for k in found if k is not None))


def fault_fragment[M: Model](seed: type[M], text: str, fields: frozenset[str]) -> M:
    """A provider's own seed model read from a fragment that may set only `fields` (those declaring faults), for
    `ports.provider.DeclaresFaults.declare`; a fragment setting anything else is refused, naming it, since a world
    already open cannot be re-seeded."""
    fragment = seed.model_validate_json(text)
    others = sorted(set(fragment.model_fields_set) - fields)
    if others:
        raise ValueError(
            f"a fault declaration on an open world may set only {', '.join(sorted(fields))}; it sets {', '.join(others)}"
        )
    return fragment


def merged_seed[M: Model](seed: type[M], held: str | None, added: str) -> str:
    """A provider's own seed with a further fragment of it merged in, each read through the provider's model: a list
    the fragment sets grows by what it lists, a model it sets is merged field by field, and any other value it sets
    must be what the world's seed already says. The result is validated again before it is returned, as JSON text."""
    fragment = seed.model_validate_json(added).model_dump(mode="json", exclude_unset=True)
    whole = seed.model_validate_json(held).model_dump(mode="json", exclude_unset=True) if held is not None else {}
    return seed.model_validate(_merged(whole, fragment, seed.__name__)).model_dump_json(exclude_unset=True)


def _merged(held: JsonValue, added: JsonValue, where: str) -> JsonValue:
    if isinstance(held, dict) and isinstance(added, dict):
        out = dict(held)
        for name, value in added.items():
            out[name] = _merged(held[name], value, f"{where}.{name}") if name in held else value
        return out
    if isinstance(held, list) and isinstance(added, list):
        return [*held, *added]
    if held != added:
        raise ValueError(f"{where} is {held!r} in this world's seed, and the addition says {added!r}")
    return held
