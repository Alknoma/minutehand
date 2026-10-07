"""Gmail v1 over the run's store and clock: mailboxes, sending, reading, labels, and the history an agent polls.

**Mailboxes.** Every scenario person has one, at their address; a service account or any other account has none,
and Gmail answers its calls 400 `failedPrecondition`, as Gmail answers a service account that impersonates nobody.
`userId` is `me` or the caller's own address; another address is refused 403, since nobody delegated it.

**A message is one entity per mailbox** (`EntityKind.MESSAGE`, listed under its mailbox): a message sent from one
person in the world to another is in both mailboxes, each copy with its own id, thread and labels, as in Gmail. Its
id comes from the event that wrote it, so ids grow as messages arrive. Only one copy of each message carries the
world's `MessageSnapshot`, so a check counts a message once: the sender's copy for what the agent sends, the copy
in the asking mailbox for a person's reply. The snapshot's `channel` is the thread, so the ledger folds an agent's
follow-ups in one thread into one ask, as it folds a Slack thread.

**Threads.** A message the agent sends joins the thread its `threadId` names when its subject matches that thread's
(`Re:` and `Fwd:` aside), else it starts one, as Gmail's sending guide has it. A message arriving in a mailbox joins
the thread holding a message its `In-Reply-To` or `References` names, when the subject matches; else it starts one.

**History** is read from the log: every message added to the mailbox, every label change and every deletion, after
`startHistoryId`. A history id is an event's sequence number, so it only grows, and it never expires.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.google_workspace import calendar_wire, gmail_query, wire
from minutehand.adapters.providers.google_workspace import gmail_wire as mail
from minutehand.adapters.providers.google_workspace.access import Caller, bearer, due_fault, signed_in
from minutehand.adapters.providers.google_workspace.manifest import MANIFEST
from minutehand.adapters.providers.google_workspace.state import DriveWorld
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation, Stored
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

GMAIL_HOST = "gmail.googleapis.com"
JSON = "application/json; charset=UTF-8"
DEFAULT_RESULTS = 100
FORMATS = frozenset({"full", "metadata", "minimal", "raw"})
HISTORY_TYPES = frozenset({"messageAdded", "messageDeleted", "labelAdded", "labelRemoved"})
_SCAN = 1000
EVERY_METHOD = ["GET", "POST", "PUT", "PATCH", "DELETE"]

OPERATIONS = frozenset(
    {
        "users.getProfile",
        "users.labels.list",
        "users.messages.list",
        "users.messages.get",
        "users.messages.send",
        "users.messages.modify",
        "users.threads.list",
        "users.threads.get",
        "users.history.list",
    }
)
"""Every Gmail call a fault may name, by Google's own method name."""


def mailbox_parent(address: str) -> str:
    return f"mailbox:{address.lower()}"


def mail_ref(message: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.MESSAGE, external_id=message)


def mail_id(seq: int, mailbox: str) -> str:
    """A message's id: the event that wrote it, then a digest of its mailbox, sixteen hex digits as Gmail's are."""
    return f"{seq:08x}" + hashlib.sha256(f"mail\x1f{mailbox.lower()}\x1f{seq}".encode()).hexdigest()[:8]


class Copy(Model):
    """Where one copy of a message goes, and how it is labelled there."""

    mailbox: str
    labels: list[str]
    thread_id: str | None = None


class MailWorld:
    """Typed reads and writes of the run's mailboxes."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._drive = DriveWorld(store)

    @property
    def store(self) -> Store:
        return self._store

    def owner(self, address: str) -> str | None:
        """The address of the mailbox `address` names, as the scenario spells it; None when nobody has one."""
        wanted = address.strip().lower()
        return next(
            (u.emailAddress for u in self._drive.people() if u.emailAddress and u.emailAddress.lower() == wanted), None
        )

    def message(self, message: str) -> tuple[mail.StoredMail, Stored] | None:
        """An email by its id; None when there is none, or the id is a calendar event's."""
        stored = self._store.get(mail_ref(message))
        if stored is None:
            return None
        kept = calendar_wire.KEPT.validate_json(stored.body)
        return (kept, stored) if isinstance(kept, mail.StoredMail) else None

    def messages(self, mailbox: str) -> list[tuple[mail.StoredMail, Stored]]:
        """Every message in the mailbox, newest first, as Gmail lists them."""
        found: list[tuple[mail.StoredMail, Stored]] = []
        after: str | None = None
        while True:
            page = self._store.children(
                MANIFEST.key, EntityKind.MESSAGE, mailbox_parent(mailbox), after=after, limit=_SCAN
            )
            found += [(mail.StoredMail.model_validate_json(s.body), s) for s in page]
            if len(page) < _SCAN:
                break
            after = page[-1].entity.external_id
        found.sort(key=lambda pair: (pair[0].internal_date, pair[0].id), reverse=True)
        return found

    def write(self, held: mail.StoredMail, *, operation: Operation, actor: Actor, after: MessageSnapshot | None) -> int:
        return self._store.apply(
            Change(
                entity=mail_ref(held.id),
                operation=operation,
                actor=actor,
                body=wire.dump(held),
                parent=mailbox_parent(held.mailbox),
                after=after,
            )
        ).seq

    # ------------------------------------------------------------------ delivery

    def _thread_by_reference(self, mailbox: str, message: EmailMessage) -> str | None:
        """The thread in `mailbox` holding a message this one answers (`In-Reply-To`, `References`), when the
        subjects match."""
        named = set(mail.header(message, "In-Reply-To").split()) | set(mail.header(message, "References").split())
        if not named:
            return None
        subject = mail.thread_subject(mail.header(message, "Subject"))
        for held, _ in self.messages(mailbox):
            other = mail.parsed(mail.decode(held.raw))
            if (
                mail.header(other, "Message-ID").strip() in named
                and mail.thread_subject(mail.header(other, "Subject")) == subject
            ):
                return held.thread_id
        return None

    def _thread_named(self, mailbox: str, thread: str, message: EmailMessage) -> str | None:
        """The thread `thread` in `mailbox`, when the message's subject matches it."""
        subject = mail.thread_subject(mail.header(message, "Subject"))
        for held, _ in self.messages(mailbox):
            if held.thread_id != thread:
                continue
            if mail.thread_subject(mail.header(mail.parsed(mail.decode(held.raw)), "Subject")) == subject:
                return thread
        return None

    def deliver(
        self,
        raw: bytes,
        *,
        sender: str,
        actor: Actor,
        taken_in: datetime,
        thread_id: str | None = None,
        by_reference: bool = True,
        snapshot_in: str | None = None,
        thread_of: str | None = None,
        recipient_labels: list[str] | None = None,
    ) -> mail.StoredMail | None:
        """Put a message in the sender's mailbox (when they have one) and in each recipient's who has one, and answer
        the sender's copy. The copy in `snapshot_in`'s mailbox carries the world's snapshot of the message, whose
        `thread_of` is `thread_of` when given, else the thread the copy joined (None when it starts one). A copy
        joins the thread holding what the message answers; a message sent through the API (`by_reference` False)
        joins, in the sender's mailbox, only the thread `thread_id` names, as Gmail's sending guide has it."""
        message = mail.parsed(raw)
        recipients = mail.addresses(message, "To", "Cc", "Bcc")
        copies: list[Copy] = []
        own = self.owner(sender)
        if own is not None:
            if by_reference:
                named = self._thread_by_reference(own, message)
            else:
                named = self._thread_named(own, thread_id, message) if thread_id is not None else None
            copies.append(Copy(mailbox=own, labels=[mail.SENT], thread_id=named))
        delivered = raw
        if message["Bcc"] is not None:
            stripped = mail.parsed(raw)
            del stripped["Bcc"]
            delivered = stripped.as_bytes()
        for address in recipients:
            box = self.owner(address)
            if box is None or any(c.mailbox == box for c in copies):
                continue
            copies.append(
                Copy(
                    mailbox=box,
                    labels=recipient_labels or [mail.INBOX, mail.UNREAD],
                    thread_id=self._thread_by_reference(box, message),
                )
            )
        people = [self.owner(a) or a for a in recipients]
        subject = mail.header(message, "Subject")
        body = mail.plain_text(message)
        sender_copy: mail.StoredMail | None = None
        stamp = int(taken_in.astimezone(UTC).timestamp() * 1000)
        for copy in copies:
            seq = self._store.head() + 1
            new_id = mail_id(seq, copy.mailbox)
            thread = copy.thread_id or new_id
            held = mail.StoredMail(
                mailbox=copy.mailbox,
                id=new_id,
                thread_id=thread,
                labels=copy.labels,
                raw=mail.encode(raw if copy.mailbox == own else delivered),
                internal_date=stamp,
            )
            after = (
                MessageSnapshot(
                    text=f"{subject}\n\n{body}" if subject else body,
                    channel=thread,
                    recipient_emails=people,
                    thread_of=thread_of if thread_of is not None else copy.thread_id,
                )
                if copy.mailbox == snapshot_in
                else None
            )
            self.write(held, operation=Operation.CREATE, actor=actor, after=after)
            if copy.mailbox == own:
                sender_copy = held
        return sender_copy

    def land(self, reply: PersonReply, clock: Clock) -> None:
        """A person's reply to a message the agent sent them: in their own Sent mail, and in the asking mailbox's
        inbox, threaded with what it answers. A message no longer there is left alone."""
        found = self.message(reply.in_reply_to.external_id)
        if found is None:
            return
        if reply.press is not None:
            raise ValueError(f"{reply.person} presses {reply.press.label!r} on an email, which carries no controls")
        asked, _ = found
        person = self._drive.person(reply.person)
        if person is None or person.emailAddress is None:
            raise ValueError(f"{reply.person} has no Google account in this world")
        original = mail.parsed(mail.decode(asked.raw))
        answered = mail.header(original, "Reply-To") or mail.header(original, "From") or asked.mailbox
        subject = mail.header(original, "Subject")
        replied = subject if subject.lower().startswith("re:") or not subject else f"Re: {subject}"
        message_id = mail.header(original, "Message-ID").strip()
        references = " ".join(part for part in [mail.header(original, "References").strip(), message_id] if part)
        now = clock.now()
        seq = self._store.head() + 1
        written = EmailMessage()
        written["From"] = formataddr((person.displayName, person.emailAddress))
        written["To"] = answered
        if replied:
            written["Subject"] = replied
        written["Date"] = format_datetime(now.astimezone(UTC))
        written["Message-ID"] = f"<{hashlib.sha256(f'reply{seq}'.encode()).hexdigest()[:24]}@mail.gmail.com>"
        if message_id:
            written["In-Reply-To"] = message_id
            written["References"] = references
        written.set_content(reply.text)
        self.deliver(
            written.as_bytes(),
            sender=person.emailAddress,
            actor=Actor.PERSON,
            taken_in=now,
            snapshot_in=asked.mailbox,
            thread_of=asked.id,
        )

    def saw(self, mailbox: str, operation: Operation, message: str | None = None) -> None:
        """Record that the agent read or searched a mailbox, or read one message in it."""
        ref = (
            mail_ref(message)
            if message is not None
            else EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=mailbox_parent(mailbox))
        )
        self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))


# ---------------------------------------------------------------------- the API

Handler = Callable[[Request, str, Caller], Awaitable[Response]]


def _json(answer: Model, request: Request, model: type[Model]) -> Response:
    fields = request.query_params["fields"] if "fields" in request.query_params else None
    return Response(wire.respond(answer, wire.selection(fields, model, "*")), media_type=JSON)


def _refused(refusal: wire.Refusal) -> Response:
    return Response(wire.error_body(refusal), status_code=refusal.code, media_type=JSON, headers=refusal.headers)


def _results(request: Request) -> int:
    spelled = request.query_params["maxResults"] if "maxResults" in request.query_params else None
    if spelled is None:
        return DEFAULT_RESULTS
    if not spelled.isdigit():
        raise mail.invalid_argument(f"Invalid value for maxResults: {spelled}")
    return min(max(int(spelled), 1), mail.MAX_RESULTS)


def _offset(request: Request) -> int:
    spelled = request.query_params["pageToken"] if "pageToken" in request.query_params else None
    try:
        return wire.decode_page(spelled)
    except wire.Refusal as refused:
        raise mail.invalid_argument("Invalid pageToken") from refused


class GmailApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._mail = MailWorld(store)
        self._drive = DriveWorld(store)
        self._clock = clock

    @property
    def mail(self) -> MailWorld:
        return self._mail

    def guarded(self, handler: Handler, operation: str) -> Callable[[Request], Awaitable[Response]]:
        """Know the caller by their token, play any fault due, find the mailbox `userId` names, and answer refusals
        in Gmail's shape."""

        async def endpoint(request: Request) -> Response:
            try:
                access = request.query_params["access_token"] if "access_token" in request.query_params else None
                caller = signed_in(self._drive, self._clock, bearer(request, access), missing=wire.login_required())
                kind = due_fault(self._drive, self._clock, operation)
                if kind is not None:
                    raise mail.fault(kind)
                return await handler(request, self._mailbox(request.path_params["user_id"], caller), caller)
            except wire.Refusal as refusal:
                return _refused(refusal)

        return endpoint

    def _mailbox(self, user_id: str, caller: Caller) -> str:
        if user_id != "me" and user_id.lower() != caller.email.lower():
            raise mail.refusal(403, "forbidden", f"Delegation denied for {caller.email}")
        found = self._mail.owner(caller.email)
        if found is None:
            raise mail.refusal(400, "failedPrecondition", "Precondition check failed.", status="FAILED_PRECONDITION")
        return found

    # ------------------------------------------------------------------ serving one message

    def _served(self, held: mail.StoredMail, stored: Stored, request: Request) -> mail.GmailMessage:
        spelled = request.query_params["format"] if "format" in request.query_params else "full"
        if spelled not in FORMATS:
            raise mail.invalid_argument(f"Invalid value for format: {spelled}")
        raw = mail.decode(held.raw)
        message = mail.parsed(raw)
        common = mail.GmailMessage(
            id=held.id,
            threadId=held.thread_id,
            labelIds=held.labels,
            snippet=mail.snippet(mail.plain_text(message)),
            historyId=str(stored.seq),
            internalDate=str(held.internal_date),
            sizeEstimate=len(raw),
        )
        if spelled == "minimal":
            return common
        if spelled == "raw":  # enum-lint: exempt Gmail's format=raw
            return common.model_copy(update={"raw": mail.encode(raw)})
        if spelled == "metadata":
            wanted = {h.lower() for h in request.query_params.getlist("metadataHeaders")}
            tree = mail.payload(message, with_bodies=False)
            headers = [h for h in tree.headers if not wanted or h.name.lower() in wanted]
            return common.model_copy(update={"payload": tree.model_copy(update={"headers": headers, "parts": None})})
        return common.model_copy(update={"payload": mail.payload(message)})

    def _candidate(self, held: mail.StoredMail, mailbox: str) -> gmail_query.Candidate:
        message = mail.parsed(mail.decode(held.raw))
        return gmail_query.Candidate(
            sender=mail.header(message, "From"),
            to=mail.header(message, "To"),
            cc=mail.header(message, "Cc"),
            bcc=mail.header(message, "Bcc"),
            subject=mail.header(message, "Subject"),
            body=mail.plain_text(message),
            labels=held.labels,
            label_names=[label.lower() for label in held.labels],
            taken_in=datetime.fromtimestamp(held.internal_date / 1000, UTC),
            message_id=mail.header(message, "Message-ID"),
            me=mailbox,
        )

    def _matching(self, request: Request, mailbox: str) -> list[tuple[mail.StoredMail, Stored]]:
        """The mailbox's messages a list call asks for, newest first: by `labelIds`, by `q`, and without the spam
        and the trash unless the call asks for them."""
        spelled = request.query_params["q"] if "q" in request.query_params else ""
        try:
            query = gmail_query.parse(spelled)
        except gmail_query.QueryError as error:
            raise mail.invalid_argument(f"Invalid query: {error}") from error
        except gmail_query.QueryNotSupported as error:
            raise mail.not_implemented(f"searching mail with {error}") from error
        labels = request.query_params.getlist("labelIds")
        with_bin = (
            request.query_params.get("includeSpamTrash", "false") == "true"
            or any(label in (mail.TRASH, mail.SPAM) for label in labels)
            or any(gmail_query.mentions(query, gmail_query.Field.IN, w) for w in ("trash", "spam", "anywhere"))
        )
        now = self._clock.now()
        found: list[tuple[mail.StoredMail, Stored]] = []
        for held, stored in self._mail.messages(mailbox):
            if not with_bin and (mail.TRASH in held.labels or mail.SPAM in held.labels):
                continue
            if any(label not in held.labels for label in labels):
                continue
            try:
                if query.clauses and not gmail_query.matches(query, self._candidate(held, mailbox), now):
                    continue
            except gmail_query.QueryError as error:
                raise mail.invalid_argument(f"Invalid query: {error}") from error
            except gmail_query.QueryNotSupported as error:
                raise mail.not_implemented(f"searching mail with {error}") from error
            found.append((held, stored))
        return found

    # ------------------------------------------------------------------ routes

    async def profile(self, request: Request, mailbox: str, caller: Caller) -> Response:
        every = self._mail.messages(mailbox)
        answer = mail.Profile(
            emailAddress=mailbox,
            messagesTotal=len(every),
            threadsTotal=len({held.thread_id for held, _ in every}),
            historyId=str(self._mail.store.head()),
        )
        self._mail.saw(mailbox, Operation.READ)
        return _json(answer, request, mail.Profile)

    async def labels_list(self, request: Request, mailbox: str, caller: Caller) -> Response:
        labels = [
            mail.Label(
                id=name,
                name=name,
                type="system",
                labelListVisibility="labelHide" if name in mail.HIDDEN_LABELS else None,
            )
            for name in mail.SYSTEM_LABELS
        ]
        self._mail.saw(mailbox, Operation.READ)
        return _json(mail.LabelList(labels=labels), request, mail.LabelList)

    async def messages_list(self, request: Request, mailbox: str, caller: Caller) -> Response:
        found = self._matching(request, mailbox)
        size, offset = _results(request), _offset(request)
        page = found[offset : offset + size]
        more = offset + size < len(found)
        answer = mail.MessageList(
            messages=[mail.MessageRef(id=held.id, threadId=held.thread_id) for held, _ in page] or None,
            nextPageToken=wire.encode_page(offset + size) if more else None,
            resultSizeEstimate=len(found),
        )
        self._mail.saw(mailbox, Operation.SEARCH)
        return _json(answer, request, mail.MessageList)

    def _held(self, message: str, mailbox: str) -> tuple[mail.StoredMail, Stored]:
        if not message or not all(c in "0123456789abcdef" for c in message):
            raise mail.invalid_argument("Invalid id value")
        found = self._mail.message(message)
        if found is None or found[0].mailbox != mailbox:
            raise mail.not_found()
        return found

    async def messages_get(self, request: Request, mailbox: str, caller: Caller) -> Response:
        held, stored = self._held(request.path_params["message_id"], mailbox)
        served = self._served(held, stored, request)
        self._mail.saw(mailbox, Operation.READ, held.id)
        return _json(served, request, mail.GmailMessage)

    async def messages_send(self, request: Request, mailbox: str, caller: Caller) -> Response:
        asked = wire.read_body(mail.SendRequest, wire.read_object(await request.body()))
        if not asked.raw:
            raise mail.invalid_argument(
                "'raw' RFC822 payload message string or uploading message via /upload/* URL required"
            )
        raw = mail.decode(asked.raw)
        message = mail.parsed(raw)
        if not mail.addresses(message, "To", "Cc", "Bcc"):
            raise mail.invalid_argument("Recipient address required")
        raw = self._completed(raw, mailbox, caller)
        sent = self._mail.deliver(
            raw,
            sender=mailbox,
            actor=Actor.AGENT,
            taken_in=self._clock.now(),
            thread_id=asked.threadId,
            by_reference=False,
            snapshot_in=mailbox,
        )
        assert sent is not None
        return _json(
            mail.GmailMessage(id=sent.id, threadId=sent.thread_id, labelIds=sent.labels), request, mail.GmailMessage
        )

    def _completed(self, raw: bytes, mailbox: str, caller: Caller) -> bytes:
        """The message as Gmail sends it: from the mailbox's own address (a `From` that is not it is replaced, as
        Gmail replaces an address that is no send-as alias of the account), with a `Date` and a `Message-ID` when
        the sender gave none."""
        message = mail.parsed(raw)
        named = mail.addresses(message, "From")
        changed = False
        if not named or named[0].lower() != mailbox.lower():
            if message["From"] is not None:
                del message["From"]
            message["From"] = formataddr((caller.user.displayName, mailbox))
            changed = True
        if message["Date"] is None:
            message["Date"] = format_datetime(self._clock.now().astimezone(UTC))
            changed = True
        if message["Message-ID"] is None:
            seq = self._mail.store.head() + 1
            message["Message-ID"] = f"<{hashlib.sha256(f'sent{seq}'.encode()).hexdigest()[:24]}@mail.gmail.com>"
            changed = True
        return message.as_bytes() if changed else raw

    async def messages_modify(self, request: Request, mailbox: str, caller: Caller) -> Response:
        held, _ = self._held(request.path_params["message_id"], mailbox)
        asked = wire.read_body(mail.ModifyRequest, wire.read_object(await request.body()))
        for label in [*asked.addLabelIds, *asked.removeLabelIds]:
            if label not in mail.SYSTEM_LABELS:
                raise mail.invalid_argument(f"Invalid label: {label}")
        labels = [label for label in held.labels if label not in asked.removeLabelIds]
        labels += [label for label in asked.addLabelIds if label not in labels]
        if labels != held.labels:
            held = held.model_copy(update={"labels": labels})
            self._mail.write(held, operation=Operation.UPDATE, actor=Actor.AGENT, after=None)
        return _json(
            mail.GmailMessage(id=held.id, threadId=held.thread_id, labelIds=held.labels), request, mail.GmailMessage
        )

    def _threads(self, found: list[tuple[mail.StoredMail, Stored]]) -> list[str]:
        """The threads the messages are in, the one with the newest message first."""
        return list(dict.fromkeys(held.thread_id for held, _ in found))

    async def threads_list(self, request: Request, mailbox: str, caller: Caller) -> Response:
        found = self._matching(request, mailbox)
        threads = self._threads(found)
        every = self._mail.messages(mailbox)
        size, offset = _results(request), _offset(request)
        page = threads[offset : offset + size]
        more = offset + size < len(threads)
        served: list[mail.GmailThread] = []
        for thread in page:
            members = [(h, s) for h, s in every if h.thread_id == thread]
            newest, _ = members[0]
            served.append(
                mail.GmailThread(
                    id=thread,
                    snippet=mail.snippet(mail.plain_text(mail.parsed(mail.decode(newest.raw)))),
                    historyId=str(max(s.seq for _, s in members)),
                )
            )
        answer = mail.ThreadList(
            threads=served or None,
            nextPageToken=wire.encode_page(offset + size) if more else None,
            resultSizeEstimate=len(threads),
        )
        self._mail.saw(mailbox, Operation.SEARCH)
        return _json(answer, request, mail.ThreadList)

    async def threads_get(self, request: Request, mailbox: str, caller: Caller) -> Response:
        thread = request.path_params["thread_id"]
        if not thread or not all(c in "0123456789abcdef" for c in thread):
            raise mail.invalid_argument("Invalid id value")
        members = [(h, s) for h, s in reversed(self._mail.messages(mailbox)) if h.thread_id == thread]
        if not members:
            raise mail.not_found()
        answer = mail.GmailThread(
            id=thread,
            historyId=str(max(s.seq for _, s in members)),
            messages=[self._served(h, s, request) for h, s in members],
        )
        for held, _ in members:
            self._mail.saw(mailbox, Operation.READ, held.id)
        return _json(answer, request, mail.GmailThread)

    async def history_list(self, request: Request, mailbox: str, caller: Caller) -> Response:
        spelled = request.query_params["startHistoryId"] if "startHistoryId" in request.query_params else None
        if spelled is None:
            raise mail.invalid_argument("Missing required parameter: startHistoryId")
        if not spelled.isdigit():
            raise mail.invalid_argument(f"Invalid value for startHistoryId: {spelled}")
        kinds = set(request.query_params.getlist("historyTypes")) or set(HISTORY_TYPES)
        unknown = sorted(kinds - HISTORY_TYPES)
        if unknown:
            raise mail.invalid_argument(f"Invalid value for historyTypes: {unknown[0]}")
        label = request.query_params["labelId"] if "labelId" in request.query_params else None
        records = self._history(mailbox, int(spelled), kinds, label)
        size, offset = _results(request), _offset(request)
        page = records[offset : offset + size]
        more = offset + size < len(records)
        answer = mail.HistoryList(
            history=page or None,
            nextPageToken=wire.encode_page(offset + size) if more else None,
            historyId=str(self._mail.store.head()),
        )
        self._mail.saw(mailbox, Operation.SEARCH)
        return _json(answer, request, mail.HistoryList)

    def _history(self, mailbox: str, start: int, kinds: set[str], label: str | None) -> list[mail.HistoryRecord]:
        store = self._mail.store
        parent = mailbox_parent(mailbox)
        versions: dict[str, list[Stored]] = {}
        records: list[mail.HistoryRecord] = []
        for event in store.events(since=start):
            ref = event.entity
            if ref.provider != MANIFEST.key or ref.kind is not EntityKind.MESSAGE:
                continue
            if event.operation not in (Operation.CREATE, Operation.UPDATE, Operation.DELETE):
                continue
            if ref.external_id not in versions:
                versions[ref.external_id] = store.versions(ref)
            held_versions = versions[ref.external_id]
            if not held_versions or held_versions[0].parent != parent:
                continue
            before = [v for v in held_versions if v.seq < event.seq]
            at = next((v for v in held_versions if v.seq == event.seq), None)
            current = mail.StoredMail.model_validate_json((at or held_versions[-1]).body)
            if label is not None and label not in current.labels:
                continue
            ref_message = mail.GmailMessage(id=current.id, threadId=current.thread_id, labelIds=current.labels)
            plain = mail.GmailMessage(id=current.id, threadId=current.thread_id)
            record = mail.HistoryRecord(id=str(event.seq), messages=[plain])
            if event.operation is Operation.CREATE and "messageAdded" in kinds:
                records.append(record.model_copy(update={"messagesAdded": [mail.LabelledMessage(message=ref_message)]}))
            elif event.operation is Operation.DELETE and "messageDeleted" in kinds:
                records.append(record.model_copy(update={"messagesDeleted": [mail.LabelledMessage(message=plain)]}))
            elif event.operation is Operation.UPDATE and before:
                earlier = mail.StoredMail.model_validate_json(before[-1].body).labels
                added = [x for x in current.labels if x not in earlier]
                removed = [x for x in earlier if x not in current.labels]
                update: dict[str, list[mail.LabelledMessage]] = {}
                if added and "labelAdded" in kinds:
                    update["labelsAdded"] = [mail.LabelledMessage(message=ref_message, labelIds=added)]
                if removed and "labelRemoved" in kinds:
                    update["labelsRemoved"] = [mail.LabelledMessage(message=ref_message, labelIds=removed)]
                if update:
                    records.append(record.model_copy(update=update))
        return records

    async def not_built(self, request: Request) -> Response:
        return _refused(
            mail.not_implemented(f"minutehand's Gmail does not implement {request.method} {request.url.path}")
        )

    def routes(self) -> list[Route]:
        def call(handler: Handler, operation: str) -> Callable[[Request], Awaitable[Response]]:
            return self.guarded(handler, operation)

        base = "/gmail/v1/users/{user_id}"
        return [
            Route(f"{base}/profile", call(self.profile, "users.getProfile"), methods=["GET"]),
            Route(f"{base}/labels", call(self.labels_list, "users.labels.list"), methods=["GET"]),
            Route(f"{base}/messages", call(self.messages_list, "users.messages.list"), methods=["GET"]),
            Route(f"{base}/messages/send", call(self.messages_send, "users.messages.send"), methods=["POST"]),
            Route(f"{base}/messages/{{message_id}}", call(self.messages_get, "users.messages.get"), methods=["GET"]),
            Route(
                f"{base}/messages/{{message_id}}/modify",
                call(self.messages_modify, "users.messages.modify"),
                methods=["POST"],
            ),
            Route(f"{base}/threads", call(self.threads_list, "users.threads.list"), methods=["GET"]),
            Route(f"{base}/threads/{{thread_id}}", call(self.threads_get, "users.threads.get"), methods=["GET"]),
            Route(f"{base}/history", call(self.history_list, "users.history.list"), methods=["GET"]),
            Route("/gmail/v1/{rest:path}", self.not_built, methods=EVERY_METHOD),
            Route("/upload/gmail/v1/{rest:path}", self.not_built, methods=EVERY_METHOD),
        ]
