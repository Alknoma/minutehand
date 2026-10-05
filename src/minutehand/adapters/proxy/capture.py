"""Hosts no provider claims, captured as declared (`domain.outbound`): what each call is answered with, what of
it is kept, how a send is read as a message to a person, and how a replay finds the recording of a call.

The request and answer bodies are the other service's own format, parsed only here: a JSON body as JSON, a form
as its fields, anything else as text, and a body whose content type is neither text nor JSON is kept as its
length and type only. Every kept body has its credential fields (`redact.CREDENTIAL_KEYS`) and the fields the
declaration names replaced by `[redacted]` before it is kept, hashed or matched.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import JsonValue

from minutehand.adapters.proxy import redact
from minutehand.adapters.proxy.hosts import HostPattern
from minutehand.adapters.proxy.registry import ProviderConflict, Registry
from minutehand.domain.outbound import (
    MESSAGE_ID,
    Acknowledge,
    Answer,
    HtmlAt,
    InForks,
    MessageReading,
    OnMiss,
    PassThrough,
    RecordedRun,
    Replay,
)
from minutehand.domain.scenario import Person
from minutehand.domain.world import AnsweredBy, Body, BodyKept, Recipient, RecordedCall

Declaration = Acknowledge | PassThrough | Replay

RECORDINGS = "captured.jsonl"
"""In a run's directory: every call the run captured, one `RecordedCall` a line, redacted as stored. A replay
reads it by run id, or from any directory a copy of it was put in."""

JSON = "application/json"
TEXT = "text/plain"

_TEXT_TYPES = ("application/x-www-form-urlencoded", "application/xml", "application/javascript")


def media(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def is_text(content_type: str | None, raw: bytes) -> bool:
    """Text or JSON by its own content type; with none, whatever decodes as UTF-8."""
    kind = media(content_type)
    if not kind:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    return (
        kind.startswith("text/")
        or kind == JSON
        or kind.endswith("+json")
        or kind.endswith("+xml")
        or kind in _TEXT_TYPES
    )


# -- paths into a body -------------------------------------------------------------------------------------------

_STEP = re.compile(r"([^.\[\]]+)|\[(\*|\d+)\]")


def _steps(path: str) -> list[str | int | None]:
    """`a.b[0].c[*]` as ["a", "b", 0, "c", None]: a key, an index, or None for every item."""
    steps: list[str | int | None] = []
    for key, index in _STEP.findall(path):
        if key:
            steps.append(key)
        else:
            steps.append(None if index == "*" else int(index))
    return steps


def values_at(holder: object, path: str) -> list[object]:
    """Every value at `path`, in order; none when any step is missing."""
    found: list[object] = [holder]
    for step in _steps(path):
        following: list[object] = []
        for value in found:
            if isinstance(step, str) and isinstance(value, dict) and step in value:
                following.append(value[step])
            elif step is None and isinstance(value, list):
                following.extend(value)
            elif isinstance(step, int) and isinstance(value, list) and -len(value) <= step < len(value):
                following.append(value[step])
        found = following
    return found


def _replace_at(holder: object, steps: list[str | int | None], replace: bool) -> None:
    """Every value at `steps` replaced by `[redacted]`, or, with `replace` False, removed."""
    if not steps:
        return
    step, rest = steps[0], steps[1:]
    if isinstance(holder, dict):
        if not isinstance(step, str) or step not in holder:
            return
        if rest:
            _replace_at(holder[step], rest, replace)
        elif replace:
            holder[step] = redact.REDACTED
        else:
            del holder[step]
        return
    if isinstance(holder, list):
        indices = range(len(holder)) if step is None else [step] if isinstance(step, int) else []
        for i in [i for i in indices if -len(holder) <= i < len(holder)]:
            if rest:
                _replace_at(holder[i], rest, replace)
            elif replace:
                holder[i] = redact.REDACTED
        if not rest and not replace:
            for i in sorted({i % len(holder) for i in indices if -len(holder) <= i < len(holder)}, reverse=True):
                del holder[i]


class _Form:
    """A form body as its fields: a field given once is a string, given several times a list."""

    @staticmethod
    def read(text: str) -> dict[str, object]:
        fields: dict[str, object] = {}
        for name, value in parse_qsl(text, keep_blank_values=True):
            if name not in fields:
                fields[name] = value
                continue
            held = fields[name]
            fields[name] = [*held, value] if isinstance(held, list) else [held, value]
        return fields

    @staticmethod
    def write(fields: Mapping[str, object]) -> str:
        pairs: list[tuple[str, str]] = []
        for name, value in fields.items():
            for one in value if isinstance(value, list) else [value]:
                pairs.append((name, one if isinstance(one, str) else json.dumps(one)))
        return urlencode(pairs)


def structured(text: str, content_type: str | None) -> object | None:
    """A body as the other service wrote it: JSON as JSON, a form as its fields; None for anything else."""
    kind = media(content_type)
    if kind == "application/x-www-form-urlencoded":
        return _Form.read(text)
    if kind == JSON or kind.endswith("+json") or not kind:
        try:
            parsed: object = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed
    return None


def _rewritten(text: str, content_type: str | None, paths: Sequence[str], *, replace: bool) -> str:
    """The body with every value at `paths` redacted (`replace`) or removed, in its own format."""
    if not paths:
        return text
    parsed = structured(text, content_type)
    if parsed is None:
        return text
    for path in paths:
        _replace_at(parsed, _steps(path), replace)
    if media(content_type) == "application/x-www-form-urlencoded" and isinstance(parsed, dict):
        return _Form.write(parsed)
    return json.dumps(parsed, ensure_ascii=False)


def redacted(text: str, content_type: str | None, paths: Sequence[str]) -> str:
    """Credential fields and the declared `paths` replaced by `[redacted]`."""
    once = redact.body(text, content_type or "") or ""
    return _rewritten(once, content_type, paths, replace=True)


def canonical(text: str, content_type: str | None, ignore: Sequence[str]) -> str:
    """The body as a replay matches it: `ignore`d fields removed, JSON keys sorted, form fields sorted."""
    without = _rewritten(text, content_type, ignore, replace=False)
    parsed = structured(without, content_type)
    if parsed is None:
        return without
    if media(content_type) == "application/x-www-form-urlencoded" and isinstance(parsed, dict):
        return urlencode(sorted(parse_qsl(without, keep_blank_values=True)))
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- what is kept of a body --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KeptBody:
    text: str | None
    body: Body


def keep(raw: bytes, content_type: str | None, *, limit: int, paths: Sequence[str]) -> KeptBody:
    """What the record holds of a body: text or JSON redacted and cut at `limit` bytes; anything else its length,
    type and hash only."""
    if not raw:
        return KeptBody(None, Body(content_type=content_type, size=0, kept=BodyKept.EMPTY, sha256=digest("")))
    if not is_text(content_type, raw):
        return KeptBody(
            None,
            Body(
                content_type=content_type,
                size=len(raw),
                kept=BodyKept.BINARY,
                sha256=hashlib.sha256(raw).hexdigest(),
            ),
        )
    clean = redacted(raw.decode("utf-8", errors="replace"), content_type, paths)
    encoded = clean.encode("utf-8")
    whole = len(encoded) <= limit
    text = clean if whole else encoded[:limit].decode("utf-8", errors="ignore")
    return KeptBody(
        text,
        Body(
            content_type=content_type,
            size=len(raw),
            kept=BodyKept.WHOLE if whole else BodyKept.TRUNCATED,
            sha256=digest(canonical(clean, content_type, [])),
        ),
    )


def query_of(path: str, ignore: Sequence[str]) -> str:
    """A path's query as a replay matches it: `ignore`d parameters removed, the rest sorted."""
    dropped = {name.lower() for name in ignore}
    pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True)
    return urlencode(sorted((k, v) for k, v in pairs if k.lower() not in dropped))


def query_keys(declaration: Declaration) -> frozenset[str]:
    """The query parameters redacted on this host: every credential name, and each declared field that is a
    plain name."""
    plain = {p.lower().replace("-", "_") for p in declaration.redact if "." not in p and "[" not in p}
    return redact.CAPTURED_QUERY_KEYS | plain


# -- answering ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Canned:
    status: int
    headers: dict[str, str]
    body: bytes


def canned(declaration: Acknowledge, method: str, path: str, *, message_id: str) -> Canned:
    """The answer the first matching route declares, else the host's own, with each `{message_id}` in its strings
    replaced by `message_id`."""
    route_path = urlsplit(path).path
    answer: Answer = next(
        (
            r.answer
            for r in declaration.routes
            if (r.method is None or r.method.upper() == method.upper()) and fnmatchcase(route_path, r.path)
        ),
        declaration.answer,
    )
    if answer.text is not None:
        body, kind = answer.text.replace(MESSAGE_ID, message_id).encode("utf-8"), TEXT
    else:
        filled = fill({} if answer.json_body is None else answer.json_body, {MESSAGE_ID: message_id})
        body, kind = json.dumps(filled).encode("utf-8"), JSON
    named = {k.lower() for k in answer.headers}
    headers = dict(answer.headers) if "content-type" in named else {**answer.headers, "content-type": kind}
    return Canned(status=answer.status, headers=headers, body=body)


def fill(template: JsonValue, values: Mapping[str, str]) -> JsonValue:
    """`template` with every placeholder in `values` replaced inside its strings; the structure is kept, so a
    value is never parsed as JSON and needs no escaping."""
    if isinstance(template, str):
        for placeholder, value in values.items():
            template = template.replace(placeholder, value)
        return template
    if isinstance(template, list):
        return [fill(v, values) for v in template]
    if isinstance(template, dict):
        return {k: fill(v, values) for k, v in template.items()}
    return template


# -- a send read as a message -----------------------------------------------------------------------------------


class _Text(HTMLParser):
    """HTML reduced to the text a person reads: tags dropped, scripts and styles skipped, a block a new line."""

    _BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"})
    _SKIPPED = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED:
            self._skipping += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_text(html: str) -> str:
    reader = _Text()
    reader.feed(html)
    reader.close()
    lines = [" ".join(line.split()) for line in "".join(reader.parts).split("\n")]
    return "\n".join(line for line in lines if line)


_ADDRESS = re.compile(r"<([^<>]+)>\s*$")


def _addresses(value: object) -> list[str]:
    """A recipient value as addresses: a string split on commas, `Name <address>` read as the address; a list
    item by item; an object by its `email`, else its `address`, else its `to`."""
    if isinstance(value, list):
        return [a for item in value for a in _addresses(item)]
    if isinstance(value, dict):
        for key in ("email", "address", "to"):
            if key in value:
                return _addresses(value[key])
        return []
    if not isinstance(value, str):
        return []
    found: list[str] = []
    for part in value.split(","):
        part = part.strip()
        named = _ADDRESS.search(part)
        if named is not None:
            part = named.group(1).strip()
        if part:
            found.append(part)
    return found


@dataclass(frozen=True)
class Read:
    """A send as a message: who it reached and what it said, or why it could not be read."""

    recipients: list[Recipient] = field(default_factory=lambda: list[Recipient]())
    text: str = ""
    subject: str | None = None
    unread: str | None = None


def _first_text(parsed: object, candidates: Sequence[str | HtmlAt]) -> str | None:
    for candidate in candidates:
        path = candidate.html if isinstance(candidate, HtmlAt) else candidate
        for value in values_at(parsed, path):
            if isinstance(value, str) and value.strip():
                return html_text(value) if isinstance(candidate, HtmlAt) else value
    return None


def read_message(reading: MessageReading, text: str | None, content_type: str | None, people: Sequence[Person]) -> Read:
    """The message a send carries, read by the declaration's paths from the request as the agent sent it."""
    parsed = structured(text, content_type) if text is not None else None
    if parsed is None:
        return Read(unread="the request body is not JSON or a form, so no message could be read from it")
    addresses = list(
        dict.fromkeys(a for path in reading.recipients for v in values_at(parsed, path) for a in _addresses(v))
    )
    if not addresses:
        return Read(unread=f"no recipient at {', '.join(reading.recipients)}")
    said = _first_text(parsed, reading.text)
    if said is None:
        return Read(unread="no text at any of the declared paths")
    by_email = {p.email.lower(): p for p in people}
    by_key = {p.key: p for p in people}
    recipients: list[Recipient] = []
    for address in addresses:
        person = by_email[address.lower()] if address.lower() in by_email else None
        if person is None and address in reading.handles and reading.handles[address] in by_key:
            person = by_key[reading.handles[address]]
        recipients.append(Recipient(address=address, person=person.key if person is not None else None))
    subject = _first_text(parsed, list(reading.subject))
    return Read(recipients=recipients, text=said, subject=subject)


# -- replay ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Asked:
    """A live call as a replay matches it, before anything of it is kept."""

    method: str
    host: str
    path: str
    body: str | None
    content_type: str | None
    raw_digest: str


@dataclass
class Recordings:
    """The captured calls of one earlier run that a replay may answer from, and how far through each identical
    call it has got."""

    source: str
    calls: list[RecordedCall]
    _used: dict[tuple[str, ...], int] = field(default_factory=lambda: dict[tuple[str, ...], int]())

    @classmethod
    def read(cls, directory: Path, *, source: str) -> Recordings:
        """`captured.jsonl` in `directory`; refused when there is none."""
        path = directory / RECORDINGS
        if not path.is_file():
            raise FileNotFoundError(f"no recordings for {source}: {path} does not exist")
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls(source=source, calls=[RecordedCall.model_validate_json(line) for line in lines if line.strip()])

    def answer(
        self, asked: Asked, *, ignore_query: Sequence[str], ignore_body: Sequence[str]
    ) -> tuple[RecordedCall | None, str]:
        """The recorded answer to the same call, or None with the reason none would do."""
        key = self._key(asked.method, asked.host, asked.path, ignore_query)
        same_route = [c for c in self.calls if self._replayable(c) and self._key_of(c, ignore_query) == key]
        if not same_route:
            return (
                None,
                f"{self.source} holds no {asked.method} {asked.host}{urlsplit(asked.path).path} with this query",
            )
        wanted = self._body(asked.body, asked.content_type, ignore_body, asked.raw_digest)
        matching: list[RecordedCall] = []
        unreadable = 0
        for call in same_route:
            recorded = self._recorded_body(call, ignore_body)
            if recorded is None:
                unreadable += 1
            elif recorded == wanted:
                matching.append(call)
        if not matching:
            why = f"{self.source} holds {len(same_route)} call(s) to this route, none with this body"
            if unreadable:
                why += f" ({unreadable} kept only part of its body, so ignored fields could not be removed from it)"
            return None, why
        whole = [c for c in matching if self._answer_whole(c)]
        if not whole:
            return None, f"{self.source} kept only part of its answer to this call, so it cannot be replayed"
        identity = (*key, wanted)
        index = self._used[identity] if identity in self._used else 0
        self._used[identity] = index + 1
        return whole[min(index, len(whole) - 1)], ""

    @staticmethod
    def _replayable(call: RecordedCall) -> bool:
        captured = call.exchange.captured
        return captured is not None and captured.answered_by is not AnsweredBy.REFUSAL

    @staticmethod
    def _answer_whole(call: RecordedCall) -> bool:
        captured = call.exchange.captured
        return captured is not None and captured.response.kept in (BodyKept.WHOLE, BodyKept.EMPTY)

    @staticmethod
    def _key(method: str, host: str, path: str, ignore_query: Sequence[str]) -> tuple[str, ...]:
        return (method.upper(), host.lower(), urlsplit(path).path, query_of(path, ignore_query))

    def _key_of(self, call: RecordedCall, ignore_query: Sequence[str]) -> tuple[str, ...]:
        e = call.exchange
        return self._key(e.method, e.host, e.path, ignore_query)

    @staticmethod
    def _body(text: str | None, content_type: str | None, ignore: Sequence[str], raw_digest: str) -> str:
        if text is None:
            return raw_digest
        return digest(canonical(text, content_type, ignore))

    def _recorded_body(self, call: RecordedCall, ignore: Sequence[str]) -> str | None:
        """The recorded request body as `_body` reads a live one; None when ignored fields cannot be taken out
        of it because only part of it was kept."""
        captured = call.exchange.captured
        assert captured is not None
        request = captured.request
        if request.kept in (BodyKept.BINARY, BodyKept.EMPTY) or not ignore:
            return request.sha256
        if request.kept is BodyKept.TRUNCATED or call.exchange.request_body is None:
            return None
        return digest(canonical(call.exchange.request_body, request.content_type, ignore))


@dataclass(frozen=True)
class Replaying:
    """Where a host's calls are answered from before they reach the real host, and what a miss does."""

    recordings: Recordings
    ignore_query: list[str]
    ignore_body: list[str]
    on_miss: OnMiss


# -- the declarations of one world -------------------------------------------------------------------------------


class Capturing:
    """What a world captures: its declarations, the recordings its replays read, and the people a send may
    reach. Empty, it captures nothing and every unclaimed host is refused."""

    def __init__(
        self,
        declared: Sequence[Declaration] = (),
        *,
        replaying: Mapping[str, Replaying] | None = None,
        people: Sequence[Person] = (),
    ) -> None:
        self.declared = list(declared)
        self._patterns = [(HostPattern(d.host), d) for d in self.declared]
        for i, (mine, one) in enumerate(self._patterns):
            for theirs, other in self._patterns[i + 1 :]:
                if mine.overlaps(theirs):
                    raise ProviderConflict(f"outbound hosts {one.host!r} and {other.host!r} overlap")
        self.replaying = dict(replaying or {})
        self.people = list(people)

    def find(self, host: str) -> Declaration | None:
        return next((d for pattern, d in self._patterns if pattern.matches(host)), None)

    def for_people(self, people: Sequence[Person]) -> Capturing:
        return Capturing(self.declared, replaying=self.replaying, people=people)


def refuse_claimed(declared: Sequence[Declaration], registry: Registry, model_hosts: Sequence[str]) -> None:
    """A declared host a provider claims, or a model API, would be answered by two things: refused at load,
    naming both. So is a declaration recorded under a provider's own name."""
    for declaration in declared:
        mine = HostPattern(declaration.host)
        for manifest in registry.manifests:
            if declaration.key == manifest.key:
                raise ProviderConflict(
                    f"outbound host {declaration.host!r} is recorded as {declaration.key!r}, which is the name of "
                    f"provider {manifest.key!r}: give it another `name`"
                )
            for claimed in manifest.hosts:
                if mine.overlaps(HostPattern(claimed)):
                    raise ProviderConflict(
                        f"outbound host {declaration.host!r} is declared {declaration.kind}, and provider "
                        f"{manifest.key!r} claims {claimed!r}: a host is faked or captured, never both"
                    )
        for model in model_hosts:
            if mine.overlaps(HostPattern(model)):
                raise ProviderConflict(
                    f"outbound host {declaration.host!r} is declared {declaration.kind}, and {model!r} is a model "
                    "API, which is tunnelled or recorded with --record-model-calls"
                )


def replaying_for(
    declared: Sequence[Declaration], *, state: Path, parent: str | None = None, after_wake: int = 0
) -> dict[str, Replaying]:
    """The recordings each replaying host reads: a `Replay` its declared source, and in a fork of `parent` taken
    after wake `after_wake`, a `PassThrough` whose `in_forks` says replay, the parent's own recordings, missing
    through to the real host. The fork shares the parent's calls up to that wake, so the parent's later calls
    answer first: the fork's first lookup after the fork is answered as the parent's first lookup after it was."""
    found: dict[str, Replaying] = {}
    for declaration in declared:
        if isinstance(declaration, Replay):
            source = declaration.source
            if isinstance(source, RecordedRun):
                directory, named = state / "runs" / source.run, f"run {source.run}"
            else:
                directory, named = Path(source.directory), f"recordings in {source.directory}"
            found[declaration.host] = Replaying(
                Recordings.read(directory, source=named),
                list(declaration.ignore_query),
                list(declaration.ignore_body),
                declaration.on_miss,
            )
        elif isinstance(declaration, PassThrough) and parent is not None and declaration.in_forks is InForks.REPLAY:
            recorded = Recordings.read(state / "runs" / parent, source=f"parent run {parent}")
            later = [c for c in recorded.calls if c.wake > after_wake]
            shared = [c for c in recorded.calls if c.wake <= after_wake]
            found[declaration.host] = Replaying(
                Recordings(source=recorded.source, calls=later + shared),
                list(declaration.ignore_query),
                list(declaration.ignore_body),
                OnMiss.PASS_THROUGH,
            )
    return found


def write_recordings(directory: Path, calls: Sequence[RecordedCall]) -> None:
    """Every captured call of a run, for a later replay to read."""
    kept = [c for c in calls if c.exchange.captured is not None]
    (directory / RECORDINGS).write_text("".join(c.model_dump_json() + "\n" for c in kept), encoding="utf-8")
