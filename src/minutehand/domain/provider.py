"""What a provider declares about itself. Data only: loading a manifest imports no provider code."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from types import UnionType
from typing import Annotated, ClassVar, Self, Union, get_args, get_origin

from pydantic import BaseModel, Field, JsonValue, model_validator

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
    shared_hosts: list[str] = Field(
        default=[],
        description="Hosts whose answers are the same in every world and need no world's state (published signing "
        "keys and their metadata): under `minutehand serve`, a call to one that no world claims is answered "
        "rather than refused",
    )
    people_changes: list[PersonChange] = Field(
        default=[],
        description="What can happen to a person's account here while a world is open (`ChangesPeople`); any other "
        "is refused at the call, naming these",
    )
    state_outside_log: str | None = Field(
        default=None,
        description="What of the state this provider answers from it keeps outside the run's log, in words (e.g. "
        "queues and their messages, in a library's process memory); None when everything is in the log. A fork "
        "of a run that called it before the fork's seq is refused, naming this, since the child could not see it",
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


class Keyed:
    """A seed model whose items of a list are told apart by `IDENTITY`, the names of the fields that say which thing
    an item is (a workspace's `key`, a repository's `owner` and `name`). A fragment's item naming the same identity
    as one the seed holds is merged into it, rather than added beside it as a second of the same thing."""

    IDENTITY: ClassVar[tuple[str, ...]]


def merged_seed[M: Model](seed: type[M], held: str | None, added: str) -> str:
    """A provider's own seed with a further fragment of it merged in: a list the fragment sets grows by what it lists,
    except that an item of a `Keyed` model naming the identity of an item held is merged into that item; an object
    it sets is merged field by field, and any other value it sets must be what the world's seed already says. The
    fragment may name what only the whole seed holds (a repository's owner), so it is read as the merge's input and
    the whole is validated through the provider's model, which refuses what it cannot read."""
    fragment: JsonValue = json.loads(added)
    whole: JsonValue = json.loads(held) if held is not None else {}
    return seed.model_validate(_merged(whole, fragment, seed.__name__, seed)).model_dump_json(exclude_unset=True)


def _model_in(annotation: object) -> type[BaseModel] | None:
    """The one model a field holds, through `list[...]`, `X | None` and `Annotated`; None for anything else."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) is Annotated:
        return _model_in(get_args(annotation)[0])
    inner = [a for a in get_args(annotation) if a is not type(None)]
    if get_origin(annotation) is list or (len(inner) == 1 and get_origin(annotation) in (Union, UnionType)):
        return _model_in(inner[0]) if len(inner) == 1 else None
    return None


def _field_model(model: type[BaseModel] | None, name: str) -> type[BaseModel] | None:
    if model is None:
        return None
    for field_name, info in model.model_fields.items():
        if name in (field_name, info.alias):
            return _model_in(info.annotation)
    return None


def _identity(item: JsonValue, keyed: type[Keyed]) -> tuple[JsonValue, ...] | None:
    if not isinstance(item, dict) or any(k not in item for k in keyed.IDENTITY):
        return None
    return tuple(item[k] for k in keyed.IDENTITY)


def _merged(held: JsonValue, added: JsonValue, where: str, model: type[BaseModel] | None) -> JsonValue:
    if isinstance(held, dict) and isinstance(added, dict):
        out = dict(held)
        for name, value in added.items():
            inner = _field_model(model, name)
            out[name] = _merged(held[name], value, f"{where}.{name}", inner) if name in held else value
        return out
    if isinstance(held, list) and isinstance(added, list):
        if model is None or not issubclass(model, Keyed):
            return [*held, *added]
        out_list = list(held)
        for item in added:
            named = _identity(item, model)
            at = next((n for n, h in enumerate(out_list) if named is not None and _identity(h, model) == named), None)
            if at is None:
                out_list.append(item)
            else:
                label = ", ".join(f"{k}={v!r}" for k, v in zip(model.IDENTITY, named or (), strict=True))
                out_list[at] = _merged(out_list[at], item, f"{where}[{label}]", model)
        return out_list
    if held != added:
        raise ValueError(f"{where} is {held!r} in this world's seed, and the addition says {added!r}")
    return held
