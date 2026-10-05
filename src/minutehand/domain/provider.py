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
    NUMBER = "number"  # its declared number within its project (`BACKEND-142`)
    ID = "id"  # its declared id in the service


class DocumentField(StrEnum):
    """A `SeededDocument` fact not every document provider can hold: a kind other than a plain document, or a field
    other than its title and text."""

    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    FILE = "file"
    FOLDER = "folder"
    OWNER = "owner"
    SPACE = "space"
    SHARED_WITH = "shared_with"
    MODIFIED_BEFORE_START = "modified_before_start"
    MODIFIED_BY = "modified_by"
    ID = "id"


class ChannelField(StrEnum):
    """A `SeededChannel` (or `SeededPost`) fact not every messaging provider can hold."""

    NAMED = "named"  # a channel with a name
    DIRECT = "direct"  # with no name: the agent's direct conversation with its members
    PRIVATE = "private"
    ARCHIVED = "archived"
    TOPIC = "topic"
    PURPOSE = "purpose"
    WITHOUT_AGENT = "without_agent"  # `agent_member: false`
    HISTORY = "history"
    THREADS = "threads"  # a post's replies
    FILES = "files"  # a post's files
    ID = "id"
    POST_ID = "post_id"


class SpaceField(StrEnum):
    """What a provider holds of `Scenario.spaces`."""

    SPACES = "spaces"  # any shared space at all
    ID = "id"


class AccountFact(StrEnum):
    """A `PersonAccount` field: what a person's entry may say of their account in one service."""

    LOGIN = "login"
    ID = "id"
    NAME = "name"
    EMAIL_HIDDEN = "email_hidden"  # `email_visible: false`


class PersonFact(StrEnum):
    """A fact of a `Person` the services they have accounts in present (or have no place for).

    A person is one across every service, so a fact a service has no place for is not refused: the manifest lists
    what the service shows, and the real service shows nothing of the rest (Asana has no job title)."""

    WITHOUT_EMAIL = "without_email"
    TITLE = "title"
    GUEST = "guest"
    DEACTIVATED = "deactivated"
    BOT = "bot"
    WORKING_HOURS = "working_hours"
    ABSENCES = "absences"


class TicketActionKind(StrEnum):
    """What a `TicketHappening` has its person do (`TicketAction.kind`)."""

    MOVES = "moves"
    REASSIGNS = "reassigns"
    COMMENTS = "comments"
    DELETES = "deletes"


class MessagingKind(StrEnum):
    """What a `MessagingHappening` has its person do (its `kind`)."""

    POSTS = "posts"
    EDITS = "edits"
    DELETES = "deletes"
    REACTS = "reacts"
    JOINS = "joins"
    ADDS_AGENT = "adds_agent"
    OPENS_AGENT = "opens_agent"
    COMMANDS = "commands"


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


class Holds(Model):
    """What a provider holds of everything the shared seed can say, one answer per fact, none defaulted: a fact
    added to a shared seed model, a happening kind or a people change is a field here, and every manifest fails the
    type checker until it answers it. `False` is a promise: `application.refusals.unheld` refuses the fact at load,
    naming it, rather than let the provider drop it in silence. `tests/providers/test_held_or_refused.py` walks
    every field of the shared seed models against these and seeds each fact into each provider: a fact held must
    change what the provider stores."""

    # a `Person` fact presented on their account
    person_without_email: bool
    person_title: bool
    person_guest: bool
    person_deactivated: bool
    person_bot: bool
    person_working_hours: bool
    person_absences: bool
    # a `PersonAccount` fact
    account_login: bool
    account_id: bool
    account_name: bool
    account_email_hidden: bool
    # an optional `SeededTicket` fact
    ticket_key: bool
    ticket_labels: bool
    ticket_comments: bool
    ticket_number: bool
    ticket_id: bool
    # an optional `SeededDocument` fact
    document_spreadsheet: bool
    document_presentation: bool
    document_file: bool
    document_folder: bool
    document_owner: bool
    document_space: bool
    document_shared_with: bool
    document_modified_before_start: bool
    document_modified_by: bool
    document_id: bool
    # a `SeededChannel`/`SeededPost` fact
    channel_named: bool
    channel_direct: bool
    channel_private: bool
    channel_archived: bool
    channel_topic: bool
    channel_purpose: bool
    channel_without_agent: bool
    channel_history: bool
    channel_threads: bool
    channel_files: bool
    channel_id: bool
    channel_post_id: bool
    # what it holds of `Scenario.spaces`
    space_spaces: bool
    space_id: bool
    # a `TicketHappening` action
    ticket_action_moves: bool
    ticket_action_reassigns: bool
    ticket_action_comments: bool
    ticket_action_deletes: bool
    # a `MessagingHappening` kind
    messaging_posts: bool
    messaging_edits: bool
    messaging_deletes: bool
    messaging_reacts: bool
    messaging_joins: bool
    messaging_adds_agent: bool
    messaging_opens_agent: bool
    messaging_commands: bool
    # a `DocumentHappening` action
    document_change_edited: bool
    document_change_renamed: bool
    document_change_moved: bool
    document_change_shared: bool
    document_change_trashed: bool
    document_change_commented: bool
    document_change_field_set: bool
    # a change to an account while a world is open
    people_change_removed: bool
    people_change_deactivated: bool
    people_change_reactivated: bool
    sign_ins: bool
    """Whether it holds `Scenario.sign_ins`."""
    faults: bool
    """Whether it declares deliberate faults of its own kinds (`ports.provider.DeclaresFaults`)."""

    def person_fact(self, fact: PersonFact) -> bool:
        """Whether it holds a `Person` fact presented on their account (`PersonFact`)."""
        match fact:
            case PersonFact.WITHOUT_EMAIL:
                return self.person_without_email
            case PersonFact.TITLE:
                return self.person_title
            case PersonFact.GUEST:
                return self.person_guest
            case PersonFact.DEACTIVATED:
                return self.person_deactivated
            case PersonFact.BOT:
                return self.person_bot
            case PersonFact.WORKING_HOURS:
                return self.person_working_hours
            case PersonFact.ABSENCES:
                return self.person_absences

    def account_fact(self, fact: AccountFact) -> bool:
        """Whether it holds a `PersonAccount` fact (`AccountFact`)."""
        match fact:
            case AccountFact.LOGIN:
                return self.account_login
            case AccountFact.ID:
                return self.account_id
            case AccountFact.NAME:
                return self.account_name
            case AccountFact.EMAIL_HIDDEN:
                return self.account_email_hidden

    def ticket_field(self, fact: TicketField) -> bool:
        """Whether it holds an optional `SeededTicket` fact (`TicketField`)."""
        match fact:
            case TicketField.KEY:
                return self.ticket_key
            case TicketField.LABELS:
                return self.ticket_labels
            case TicketField.COMMENTS:
                return self.ticket_comments
            case TicketField.NUMBER:
                return self.ticket_number
            case TicketField.ID:
                return self.ticket_id

    def document_field(self, fact: DocumentField) -> bool:
        """Whether it holds an optional `SeededDocument` fact (`DocumentField`)."""
        match fact:
            case DocumentField.SPREADSHEET:
                return self.document_spreadsheet
            case DocumentField.PRESENTATION:
                return self.document_presentation
            case DocumentField.FILE:
                return self.document_file
            case DocumentField.FOLDER:
                return self.document_folder
            case DocumentField.OWNER:
                return self.document_owner
            case DocumentField.SPACE:
                return self.document_space
            case DocumentField.SHARED_WITH:
                return self.document_shared_with
            case DocumentField.MODIFIED_BEFORE_START:
                return self.document_modified_before_start
            case DocumentField.MODIFIED_BY:
                return self.document_modified_by
            case DocumentField.ID:
                return self.document_id

    def channel_field(self, fact: ChannelField) -> bool:
        """Whether it holds a `SeededChannel`/`SeededPost` fact (`ChannelField`)."""
        match fact:
            case ChannelField.NAMED:
                return self.channel_named
            case ChannelField.DIRECT:
                return self.channel_direct
            case ChannelField.PRIVATE:
                return self.channel_private
            case ChannelField.ARCHIVED:
                return self.channel_archived
            case ChannelField.TOPIC:
                return self.channel_topic
            case ChannelField.PURPOSE:
                return self.channel_purpose
            case ChannelField.WITHOUT_AGENT:
                return self.channel_without_agent
            case ChannelField.HISTORY:
                return self.channel_history
            case ChannelField.THREADS:
                return self.channel_threads
            case ChannelField.FILES:
                return self.channel_files
            case ChannelField.ID:
                return self.channel_id
            case ChannelField.POST_ID:
                return self.channel_post_id

    def space_field(self, fact: SpaceField) -> bool:
        """Whether it holds what it holds of `Scenario.spaces` (`SpaceField`)."""
        match fact:
            case SpaceField.SPACES:
                return self.space_spaces
            case SpaceField.ID:
                return self.space_id

    def ticket_action(self, fact: TicketActionKind) -> bool:
        """Whether it holds a `TicketHappening` action (`TicketActionKind`)."""
        match fact:
            case TicketActionKind.MOVES:
                return self.ticket_action_moves
            case TicketActionKind.REASSIGNS:
                return self.ticket_action_reassigns
            case TicketActionKind.COMMENTS:
                return self.ticket_action_comments
            case TicketActionKind.DELETES:
                return self.ticket_action_deletes

    def messaging(self, fact: MessagingKind) -> bool:
        """Whether it holds a `MessagingHappening` kind (`MessagingKind`)."""
        match fact:
            case MessagingKind.POSTS:
                return self.messaging_posts
            case MessagingKind.EDITS:
                return self.messaging_edits
            case MessagingKind.DELETES:
                return self.messaging_deletes
            case MessagingKind.REACTS:
                return self.messaging_reacts
            case MessagingKind.JOINS:
                return self.messaging_joins
            case MessagingKind.ADDS_AGENT:
                return self.messaging_adds_agent
            case MessagingKind.OPENS_AGENT:
                return self.messaging_opens_agent
            case MessagingKind.COMMANDS:
                return self.messaging_commands

    def document_change(self, fact: DocumentChange) -> bool:
        """Whether it holds a `DocumentHappening` action (`DocumentChange`)."""
        match fact:
            case DocumentChange.EDITED:
                return self.document_change_edited
            case DocumentChange.RENAMED:
                return self.document_change_renamed
            case DocumentChange.MOVED:
                return self.document_change_moved
            case DocumentChange.SHARED:
                return self.document_change_shared
            case DocumentChange.TRASHED:
                return self.document_change_trashed
            case DocumentChange.COMMENTED:
                return self.document_change_commented
            case DocumentChange.FIELD_SET:
                return self.document_change_field_set

    def people_change(self, fact: PersonChange) -> bool:
        """Whether it holds a change to an account while a world is open (`PersonChange`)."""
        match fact:
            case PersonChange.REMOVED:
                return self.people_change_removed
            case PersonChange.DEACTIVATED:
                return self.people_change_deactivated
            case PersonChange.REACTIVATED:
                return self.people_change_reactivated

    @classmethod
    def nothing(cls) -> Holds:
        """A provider that holds none of it: everything the shared seed says for it is refused at load. For a
        provider that holds no people, tickets, documents or channels at all; any other answers field by field."""
        return cls.model_validate(dict.fromkeys(cls.model_fields, False))

    def people_changes(self) -> list[PersonChange]:
        """The changes to an account it can show while a world is open."""
        return [c for c in PersonChange if self.people_change(c)]


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
    world_keys: list[WorldKey] = Field(
        default=[],
        description="Where a request to this provider names its world without a credential: per-customer hosts, "
        "tenant or site ids in the path",
    )
    shared_hosts: list[str] = Field(
        default=[],
        description="Hosts whose answers are the same in every world and need no world's state (published signing "
        "keys and their metadata): under `minutehand serve`, a call to one that no world claims is answered "
        "rather than refused",
    )
    holds: Holds = Field(
        description="What it holds of everything the shared seed can say; whatever it does not hold is refused at load"
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
