"""Gmail's search operators, as `q` on `users.messages.list` and `users.threads.list` carries them.

Terms are joined by AND; `OR` (in capitals) joins the term before it with the one after; `-` before a term negates it.
A term is a word, a `"quoted phrase"`, or an operator and its value:

| Operator | Matches |
|---|---|
| `from:` `to:` `cc:` `bcc:` | the header's addresses and names hold the value, in any case; `me` is the mailbox's own address |
| `subject:` | the subject holds the value's words (a quoted value: as a phrase) |
| `is:` | `unread`, `read`, `starred`, `important` |
| `in:` | `inbox`, `sent`, `trash`, `spam`, `draft`, `anywhere` |
| `label:` | a label by its id or its name, in any case |
| `after:` `before:` | the date Gmail took the message in, `YYYY/MM/DD` (midnight UTC) or seconds since the epoch |
| `newer_than:` `older_than:` | relative to now, in days (`d`), months (`m`, 30 days) or years (`y`, 365 days) |
| `rfc822msgid:` | the `Message-ID` header |
| a word or phrase | the subject, the body, or a sender's or recipient's address or name, as whole words |

Anything else Gmail's search box takes (grouping in parentheses or braces, `AROUND`, `has:`, `filename:`, `size:`,
`category:`) is refused as not implemented. An operator Gmail does not know (`colour:red`) is searched for as words,
as Gmail does.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import Field as Described

from minutehand.domain.scenario import Model

NOT_BUILT = frozenset({"has", "filename", "size", "larger", "smaller", "category", "list", "deliveredto"})
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class QueryNotSupported(Exception):
    """Valid Gmail search this fake does not answer."""


class QueryError(Exception):
    """Not a query Gmail could read."""


class Field(StrEnum):
    """Gmail's operators, each spelled with its colon as a query writes it; a bare word or phrase is `TEXT`."""

    TEXT = "a word or phrase"
    FROM = "from:"
    TO = "to:"
    CC = "cc:"
    BCC = "bcc:"
    SUBJECT = "subject:"
    IS = "is:"
    IN = "in:"
    LABEL = "label:"
    AFTER = "after:"
    BEFORE = "before:"
    NEWER_THAN = "newer_than:"
    OLDER_THAN = "older_than:"
    MESSAGE_ID = "rfc822msgid:"


class Term(Model):
    field: Field
    value: str
    phrase: bool = False
    negated: bool = False


class Query(Model):
    """Clauses joined by AND, each a list of terms joined by OR."""

    clauses: list[list[Term]] = []


class Candidate(Model):
    """What one message offers a search."""

    sender: str
    to: str
    cc: str
    bcc: str
    subject: str
    body: str
    labels: list[str]
    label_names: list[str]
    taken_in: datetime
    message_id: str
    me: str = Described(description="The mailbox's own address, which `from:me` and `to:me` name")


def _tokens(text: str) -> list[str]:
    found: list[str] = []
    at = 0
    while at < len(text):
        if text[at].isspace():
            at += 1
            continue
        if text[at] in "(){}":
            raise QueryNotSupported("grouping search terms in parentheses or braces")
        start = at
        while at < len(text) and not text[at].isspace():
            if text[at] == '"':
                close = text.find('"', at + 1)
                if close < 0:
                    raise QueryError("an unclosed quote")
                at = close + 1
            elif text[at] in "(){}":
                raise QueryNotSupported("grouping search terms in parentheses or braces")
            else:
                at += 1
        found.append(text[start:at])
    return found


def _term(token: str) -> Term:
    negated = token.startswith("-") and len(token) > 1
    body = token[1:] if negated else token
    name, colon, value = body.partition(":")
    if colon and not name.startswith('"') and value:
        key = name.lower()
        if key in NOT_BUILT:
            raise QueryNotSupported(f"the search operator {key}:")
        if f"{key}:" in {f.value for f in Field}:
            phrase = value.startswith('"') and value.endswith('"') and len(value) > 1
            return Term(field=Field(f"{key}:"), value=value.strip('"'), phrase=phrase, negated=negated)
    phrase = body.startswith('"') and body.endswith('"') and len(body) > 1
    return Term(field=Field.TEXT, value=body.strip('"'), phrase=phrase, negated=negated)


def parse(text: str) -> Query:
    tokens = _tokens(text)
    clauses: list[list[Term]] = []
    joining = False
    for token in tokens:
        if token == "AROUND":
            raise QueryNotSupported("AROUND")
        if token == "OR":
            if not clauses or joining:
                raise QueryError("OR with nothing before it")
            joining = True
            continue
        term = _term(token)
        if joining:
            clauses[-1].append(term)
            joining = False
        else:
            clauses.append([term])
    if joining:
        raise QueryError("OR with nothing after it")
    return Query(clauses=clauses)


def mentions(query: Query, field: Field, value: str) -> bool:
    """Whether the query asks, without negating it, for `field` to be `value` (`in:trash`, `in:anywhere`)."""
    return any(t.field is field and t.value.lower() == value and not t.negated for c in query.clauses for t in c)


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _holds_words(haystack: str, value: str, *, phrase: bool) -> bool:
    words, wanted = _words(haystack), _words(value)
    if not wanted:
        return False
    if not phrase:
        return all(w in words for w in wanted)
    span = len(wanted)
    return any(words[at : at + span] == wanted for at in range(len(words) - span + 1))


def _date(value: str) -> datetime:
    if value.isdigit():
        return datetime.fromtimestamp(int(value), UTC)
    for spelled in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, spelled).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise QueryError(f"a date Gmail cannot read: {value}")


def _span(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dmy])", value.lower())
    if match is None:
        raise QueryError(f"a time span Gmail cannot read: {value}")
    days = {"d": 1, "m": 30, "y": 365}[match.group(2)]
    return timedelta(days=int(match.group(1)) * days)


_IS_LABEL = {"unread": "UNREAD", "starred": "STARRED", "important": "IMPORTANT"}
_IN_LABEL = {"inbox": "INBOX", "sent": "SENT", "trash": "TRASH", "spam": "SPAM", "draft": "DRAFT"}


def _matches(term: Term, found: Candidate, now: datetime) -> bool:
    value = term.value
    headers = {Field.FROM: found.sender, Field.TO: found.to, Field.CC: found.cc, Field.BCC: found.bcc}
    if term.field in headers:
        header = headers[term.field]
        wanted = found.me if value.lower() == "me" else value
        return wanted.casefold() in header.casefold()
    if term.field is Field.SUBJECT:
        return _holds_words(found.subject, value, phrase=term.phrase)
    if term.field is Field.IS:
        word = value.lower()
        if word == "read":  # enum-lint: exempt Gmail's is:read operator
            return "UNREAD" not in found.labels
        if word in _IS_LABEL:
            return _IS_LABEL[word] in found.labels
        raise QueryNotSupported(f"is:{word}")
    if term.field is Field.IN:
        word = value.lower()
        if word == "anywhere":
            return True
        if word in _IN_LABEL:
            return _IN_LABEL[word] in found.labels
        raise QueryNotSupported(f"in:{word}")
    if term.field is Field.LABEL:
        wanted = value.casefold().replace("-", " ")
        names = [n.casefold().replace("-", " ").replace("/", " ") for n in found.label_names]
        return wanted in names or value.upper() in found.labels or value in found.labels
    if term.field is Field.AFTER:
        return found.taken_in >= _date(value)
    if term.field is Field.BEFORE:
        return found.taken_in < _date(value)
    if term.field is Field.NEWER_THAN:
        return found.taken_in > now - _span(value)
    if term.field is Field.OLDER_THAN:
        return found.taken_in < now - _span(value)
    if term.field is Field.MESSAGE_ID:
        return value.strip("<>") == found.message_id.strip().strip("<>")
    haystack = "\n".join([found.subject, found.body, found.sender, found.to, found.cc])
    return _holds_words(haystack, value, phrase=term.phrase)


def matches(query: Query, found: Candidate, now: datetime) -> bool:
    return all(any(_matches(t, found, now) != t.negated for t in clause) for clause in query.clauses)
