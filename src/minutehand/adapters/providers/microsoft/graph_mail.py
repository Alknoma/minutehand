"""Graph for Outlook mail: sending, listing, reading, marking read, replying, `delta`, and people's replies by email.

Every user of the tenant has a mailbox with four well-known folders (`inbox`, `sentitems`, `drafts`,
`deleteditems`), addressed by name in any case or by id, as a path segment or `mailFolders('Inbox')`.

- **A sent message is one copy per mailbox**: the sender's in Sent Items, read, and one in the Inbox of each
  recipient who is a user of the tenant, unread, each with its own id and all with one `conversationId`. A new
  message starts a conversation; a reply, by the agent or a person, stays in the one it answers. The sender's copy
  is the message the run reads (`MessageSnapshot`: subject and text, its recipients, its conversation as the
  channel), so an email is an ask of each person it is addressed to, and a follow-up in the same conversation is
  the same ask.
- **A person's reply** (`PersonReply` to a message in a mailbox) is an email from them to whoever sent what they
  answer, in its conversation, landing in that mailbox's Inbox at its moment; a subscription on that mailbox is
  notified of it.
- **Query options** on a list: `$top` (1 to 1000, 10 by default), `$skip`, `$select`; `$filter` on `isRead`,
  `hasAttachments`, `isDraft`, the four date properties (compared to a date and time), `from/emailAddress/address`,
  `sender/emailAddress/address`, `conversationId`, `subject`, `id` and `importance`, joined
  by `and`; `$orderby` on the four date properties and `subject`. With both, every `$orderby` property must open
  the `$filter` in the same order, or the call is refused 400 `InefficientFilter`, as Exchange refuses it. Any
  other clause or option is not served (501). `Prefer: outlook.body-content-type="text"` answers bodies as text;
  without it a body sent as text is answered as HTML, as Graph does.
- **`delta`** on a folder's messages lists every message in it, paged by `$skiptoken`, then closes with a
  `$deltatoken`; from a token, the messages changed in the folder since, and those that left it as `@removed`.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import operator
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from urllib.parse import quote, urlencode

from pydantic import Field
from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.providers.microsoft import wire
from minutehand.adapters.providers.microsoft.common import GRAPH_JSON, GraphRefusal, bad_request, graph_caller, query
from minutehand.adapters.providers.microsoft.state import (
    GRAPH,
    MAILBOX,
    MicrosoftWorld,
    UserRecord,
    derived_uuid,
    graph_time,
    message_ref,
    user_ref,
)
from minutehand.adapters.providers.microsoft.subscriptions import mail_watch, notify
from minutehand.domain.errors import NotServed
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model
from minutehand.domain.world import Actor, MessageAction, MessageSnapshot, Operation, Snapshot
from minutehand.ports.clock import Clock

PAGE_DEFAULT = 10
PAGE_MAX = 1000
ATTACHMENT_ORDER = ("name", "size", "lastModifiedDateTime", "contentType")  # enum-lint: exempt Graph's property names
ATTACHMENT_LIMIT = 3 * 1024 * 1024
"""A file attached by `POST …/attachments` is under 3 MB (message-post-attachments)."""
MESSAGE_TYPE = "#Microsoft.Graph.Message"
REQUEST_TYPE = "#microsoft.graph.eventMessageRequest"
RESPONSE_TYPE = "#microsoft.graph.eventMessageResponse"
FOLDER_NAMES = {
    wire.MailFolderName.INBOX: "Inbox",
    wire.MailFolderName.SENT: "Sent Items",
    wire.MailFolderName.DRAFTS: "Drafts",
    wire.MailFolderName.DELETED: "Deleted Items",
}
MAIL_SEGMENTS = frozenset({"sendMail", "messages", "mailFolders"})
_DATES = ("receivedDateTime", "sentDateTime", "createdDateTime", "lastModifiedDateTime")
_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
}
_TEXT_PREFERRED = re.compile(r'outlook\.body-content-type\s*=\s*"?text"?', re.IGNORECASE)
_KEYED = re.compile(r"^([A-Za-z]+)\('((?:[^']|'')*)'\)$")


def outlook_id(*parts: str) -> str:
    """An Outlook item id: opaque, URL-safe, derived from what it names."""
    raw = uuid.UUID(derived_uuid(*parts)).bytes + uuid.UUID(derived_uuid(*parts, "tail")).bytes
    return "AAMkA" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


RECIPIENTS_INVALID = "At least one recipient isn't valid."
"""Graph's 400 `ErrorInvalidRecipients` message, as recorded from the real service for `sendMail`
(https://github.com/microsoftgraph/php-connect-sample/issues/13) and for an event's attendees
(https://github.com/microsoftgraph/msgraph-sdk-php/issues/280)."""


def weak_etag(change_key: str) -> str:
    """An Outlook item's `@odata.etag`: its `changeKey` as a weak entity tag, as every example answer of Graph's
    message and event pages shows the two (https://learn.microsoft.com/en-us/graph/api/message-get)."""
    return f'W/"{change_key}"'


def versioned(message: wire.MailMessage, now: str, **changed: object) -> wire.MailMessage:
    """`message` with `changed` written over it: a new `lastModifiedDateTime`, `changeKey` and `@odata.etag`."""
    change_key = outlook_id(message.id, now, *sorted(f"{k}={v}" for k, v in changed.items()))
    return message.model_copy(
        update={**changed, "lastModifiedDateTime": now, "changeKey": change_key, "odata_etag": weak_etag(change_key)}
    )


WELL_KNOWN_FOLDERS = frozenset(
    {
        "archive",
        "clutter",
        "conflicts",
        "conversationhistory",
        "deleteditems",
        "drafts",
        "inbox",
        "junkemail",
        "localfailures",
        "msgfolderroot",
        "outbox",
        "recoverableitemsdeletions",
        "scheduled",
        "searchfolders",
        "sentitems",
        "serverfailures",
        "syncissues",
    }
)
"""Graph's well-known folder names (https://learn.microsoft.com/en-us/graph/api/resources/mailfolder)."""


def folder_id(user: str, folder: wire.MailFolderName) -> str:
    return outlook_id(user, "folder", folder.value)


def address_of(user: UserRecord) -> str:
    return user.user.mail or user.user.userPrincipalName


def recipient_of(user: UserRecord) -> wire.Recipient:
    return wire.Recipient(emailAddress=wire.EmailAddress(name=user.user.displayName, address=address_of(user)))


def plain(body: wire.ItemBody) -> str:
    """A body as the text a reader sees."""
    if body.contentType == "text":
        return body.content
    text = re.sub(r"<\s*br\s*/?>|</p\s*>|</div\s*>", "\n", body.content, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>|<[^>]+>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return html.unescape(text).strip()


def as_html(body: wire.ItemBody) -> wire.ItemBody:
    if body.contentType == "html":
        return body
    escaped = html.escape(body.content).replace("\n", "<br>")
    return wire.ItemBody(contentType="html", content=f"<html><body>{escaped}</body></html>")


def _text_preferred(request: Request) -> bool:
    prefer = request.headers["prefer"] if "prefer" in request.headers else ""
    return _TEXT_PREFERRED.search(prefer) is not None


_TIME_ZONE_PREFERRED = re.compile(r'outlook\.timezone\s*=\s*"?([^",;]+)"?', re.IGNORECASE)
_IMMUTABLE_IDS = re.compile(r'IdType\s*=\s*"?ImmutableId"?', re.IGNORECASE)


def refuse_unserved_preferences(request: Request) -> None:
    """Refuse by name a `Prefer` that would change the answer in a way not served: times in a zone other than UTC
    (`outlook.timezone`, https://learn.microsoft.com/en-us/graph/api/user-list-events) and immutable ids
    (`IdType="ImmutableId"`, https://learn.microsoft.com/en-us/graph/outlook-immutable-id). Answering as though they
    were not asked would hand back what the caller did not ask for."""
    prefer = request.headers["prefer"] if "prefer" in request.headers else ""
    zone = _TIME_ZONE_PREFERRED.search(prefer)
    if zone is not None and zone.group(1).strip().lower() not in ("utc", "etc/utc"):
        raise NotServed(f"Prefer: outlook.timezone={zone.group(1).strip()!r}: answers are in UTC only")
    if _IMMUTABLE_IDS.search(prefer) is not None:
        raise NotServed('Prefer: IdType="ImmutableId"')


def preference_applied(request: Request) -> dict[str, str] | None:
    """`Preference-Applied` when the caller asked for text bodies (message-get, user-list-calendarview)."""
    return {"Preference-Applied": 'outlook.body-content-type="text"'} if _text_preferred(request) else None


def body_as_asked(request: Request, body: wire.ItemBody | None) -> wire.ItemBody | None:
    """A stored body as the caller reads it: text when `Prefer: outlook.body-content-type="text"` asks, else HTML,
    as Graph answers either (message-get, user-list-calendarview). The body stays stored as it was sent."""
    if body is None:
        return None
    if _text_preferred(request):
        return wire.ItemBody(contentType="text", content=plain(body))
    return as_html(body)


def re_subject(subject: str | None) -> str | None:
    """A reply's subject: `RE: ` before the original's, as Outlook's own sent replies are recorded in Microsoft's
    sample of sent items (DATA_CONNECT_SENT_ITEMS); none when the original has none to answer."""
    if subject is None:
        return None
    return subject if subject.lower().startswith("re:") else f"RE: {subject}"


DATA_CONNECT_SENT_ITEMS = (
    "https://github.com/microsoftgraph/dataconnect-solutions/blob/e6b679831b424c6a6a0a68d8246e0b3be38140d9/"
    "Datasets/data-connect-dataset-sentitems.md"
)
"""Microsoft's published sample of a tenant's Sent Items, recorded from Exchange Online (Graph Data Connect)."""


def split_segments(parts: list[str]) -> list[str]:
    """`mailFolders('Inbox')` as two segments, `mailFolders` and `Inbox`, as Graph reads both forms alike."""
    found: list[str] = []
    for part in parts:
        keyed = _KEYED.match(part)
        found.extend([keyed.group(1), keyed.group(2).replace("''", "'")] if keyed else [part])
    return found


def mailbox_owner(world: MicrosoftWorld, claims: wire.Claims, parts: list[str]) -> tuple[UserRecord, list[str]]:
    """The user whose mailbox or calendar `parts` (`me/…` or `users/{id}/…`) names, and the rest of the path. A
    user's token reaches only their own; an application's reaches every user's."""
    if parts[0] == "me":
        if claims.oid is None:
            raise NotServed("/me with no signed-in user: Graph documents no answer to an application")
        key, rest = claims.oid, parts[1:]
    else:
        key, rest = parts[1], parts[2:]
    user = world.user_by(key)
    if user is None:
        raise NotServed(f"the mailbox of {key!r}, who is no user of the tenant: Graph documents no answer")
    if claims.oid is not None and claims.oid != user.user.id:
        raise GraphRefusal(403, "ErrorAccessDenied", "Access is denied. Check credentials and try again.")
    return user, split_segments(rest)


def instant(text: str) -> datetime:
    """A date and time in a `$filter`, quoted or not; one without a zone is UTC."""
    raw = text.strip("'")
    try:
        found = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise bad_request(f"'{raw}' is not a valid date and time.") from e
    return found if found.tzinfo is not None else found.replace(tzinfo=UTC)


def stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Composed(Model):
    """One message as its sender wrote it, before it is put in each mailbox."""

    sender: UserRecord
    subject: str | None
    body: wire.ItemBody | None
    said: str | None = Field(
        default=None, description="What the sender wrote, for the run's record, where the body itself is left out"
    )
    to: list[wire.Recipient]
    cc: list[wire.Recipient] = []
    bcc: list[wire.Recipient] = []
    conversation: str
    reply_to: list[wire.Recipient] = []
    importance: Literal["low", "normal", "high"] = "normal"
    meeting: wire.MeetingMessageType | None = None
    event: str | None = None
    attachments: list[wire.StoredAttachment] = []
    draft: bool = False


class _Clause(Model):
    """One `$filter` clause: the property it reads, and what it holds a message to."""

    prop: str
    op: str
    value: str


class Mail:
    def __init__(self, world: MicrosoftWorld, clock: Clock) -> None:
        self._world = world
        self._clock = clock

    # ------------------------------------------------------------------ writing

    def canonical(self, address: str) -> str:
        """An address as the run names the person: a user's mail when it is one of theirs, else as written."""
        user = self._world.user_by(address)
        return address_of(user) if user is not None else address

    def _message(
        self, composed: Composed, owner: str, folder: wire.MailFolderName, *, read: bool, seeded: str | None
    ) -> wire.MailMessage:
        now = graph_time(self._clock.now())
        made = ("seeded mail", seeded) if seeded is not None else ("mail", str(self._world.next_seq()))
        message_id = outlook_id(owner, *made)
        text = plain(composed.body) if composed.body is not None else None
        change_key = outlook_id(message_id, now)
        return wire.MailMessage(
            odata_etag=weak_etag(change_key),
            changeKey=change_key,
            odata_type=(REQUEST_TYPE if composed.meeting is wire.MeetingMessageType.REQUEST else RESPONSE_TYPE)
            if composed.meeting is not None
            else None,
            id=message_id,
            createdDateTime=now,
            lastModifiedDateTime=now,
            receivedDateTime=now,
            sentDateTime=now,
            hasAttachments=any(not a.isInline for a in composed.attachments),
            subject=composed.subject,
            bodyPreview=text[:255] if text is not None else None,
            importance=composed.importance,
            parentFolderId=folder_id(owner, folder),
            conversationId=composed.conversation,
            isRead=read,
            isDraft=composed.draft,
            webLink=f"https://outlook.office365.com/owa/?ItemID={quote(message_id)}&exvsurl=1&viewmodel=ReadMessageItem",
            body=composed.body,
            sender=None if composed.draft else recipient_of(composed.sender),
            from_=None if composed.draft else recipient_of(composed.sender),
            toRecipients=composed.to,
            ccRecipients=composed.cc,
            bccRecipients=composed.bcc if owner == composed.sender.user.id else [],
            replyTo=composed.reply_to,
            meetingMessageType=composed.meeting,
        )

    @staticmethod
    def _attached(message: wire.MailMessage, kept: list[wire.StoredAttachment]) -> list[wire.StoredAttachment]:
        """The attachments as this message holds them: each its own id, made when the message was."""
        return [
            a.model_copy(
                update={
                    "id": outlook_id(message.id, "attachment", str(n)),
                    "lastModifiedDateTime": message.lastModifiedDateTime,
                }
            )
            for n, a in enumerate(kept)
        ]

    def put(
        self,
        composed: Composed,
        *,
        actor: Actor,
        actions: list[MessageAction] | None = None,
        answerable: bool = True,
        read: bool = False,
        seeded: str | None = None,
    ) -> list[tuple[str, wire.StoredMail]]:
        """Put a message in its sender's Sent Items, the copy the run reads, and a copy in the Inbox of each
        recipient who is a user of the tenant (`read` or not). `seeded` names a message the scenario seeds, so each
        copy's id is derived from what it is rather than where the log stands. Answers each copy with its mailbox's owner, the
        sender's first. `answerable` False: it tells and asks nothing (a meeting response), so it opens no wait."""
        recipients = [*composed.to, *composed.cc, *composed.bcc]
        emails = list(dict.fromkeys(self.canonical(r.emailAddress.address) for r in recipients))
        sent_message = self._message(
            composed, composed.sender.user.id, wire.MailFolderName.SENT, read=True, seeded=seeded
        )
        sent = wire.StoredMail(
            message=sent_message,
            folder=wire.MailFolderName.SENT,
            event=composed.event,
            attachments=self._attached(sent_message, composed.attachments),
        )
        text = plain(composed.body) if composed.body is not None else (composed.said or "")
        snapshot = MessageSnapshot(
            text=f"{composed.subject}\n\n{text}".strip() if composed.subject else text,
            channel=composed.conversation,
            recipient_emails=emails,
            actions=actions or [],
            answerable=answerable,
        )
        self._world.write_mail(composed.sender.user.id, sent, operation=Operation.CREATE, actor=actor, after=snapshot)
        written = [(composed.sender.user.id, sent)]
        reached = dict.fromkeys(u.user.id for r in recipients if (u := self._world.user_by(r.emailAddress.address)))
        for owner in reached:
            copied = self._message(composed, owner, wire.MailFolderName.INBOX, read=read, seeded=seeded)
            copy = wire.StoredMail(
                message=copied,
                folder=wire.MailFolderName.INBOX,
                event=composed.event,
                attachments=self._attached(copied, composed.attachments),
            )
            self._world.write_mail(owner, copy, operation=Operation.CREATE, actor=actor, after=None)
            written.append((owner, copy))
        return written

    async def send(
        self,
        composed: Composed,
        *,
        actor: Actor,
        actions: list[MessageAction] | None = None,
        answerable: bool = True,
    ) -> wire.StoredMail:
        """`put` the message, then notify every subscription on each mailbox it landed in. Answers the sender's
        copy."""
        written = self.put(composed, actor=actor, actions=actions, answerable=answerable)
        for owner, stored in written:
            await self.notify(owner, stored, "created")
        return written[0][1]

    async def notify(self, owner: str, stored: wire.StoredMail, change: str) -> None:
        resource = f"Users/{owner}/Messages/{stored.message.id}"
        for watched in (mail_watch(owner, stored.folder.value), mail_watch(owner, None)):
            await notify(
                self._world,
                self._clock,
                watched,
                change=change,
                odata_type=MESSAGE_TYPE,
                resource=resource,
                item=stored.message.id,
            )

    def _rewrite(self, owner: str, stored: wire.StoredMail, *, actor: Actor, after: Snapshot | None = None) -> None:
        self._world.write_mail(owner, stored, operation=Operation.UPDATE, actor=actor, after=after)

    # ------------------------------------------------------------------ people

    def holds(self, message: str) -> bool:
        return self._world.mail(message) is not None

    def located(self, message: str) -> tuple[UserRecord, wire.StoredMail]:
        found = self._world.mail(message)
        if found is None:
            raise LookupError(f"no mailbox holds the message {message}")
        return found

    async def person_replies(self, reply: PersonReply) -> None:
        """The person answers a message by email: from them to whoever sent it, in its conversation."""
        _, asked = self.located(reply.in_reply_to.external_id)
        user = self._world.person(reply.person)
        if user is None:
            raise LookupError(f"{reply.person} is not a user of the tenant")
        if asked.message.from_ is None:
            raise LookupError("a draft has no sender to answer")
        await self.send(
            Composed(
                sender=user,
                subject=re_subject(asked.message.subject),
                body=wire.ItemBody(contentType="text", content=reply.text),
                to=[asked.message.from_],
                conversation=asked.message.conversationId,
            ),
            actor=Actor.PERSON,
        )

    # ------------------------------------------------------------------ Graph

    async def answer(self, request: Request, parts: list[str]) -> Response:
        claims = graph_caller(request, self._world)
        refuse_unserved_preferences(request)
        owner, rest = mailbox_owner(self._world, claims, parts)
        method = request.method
        if rest == ["sendMail"] and method == "POST":
            return await self._send_mail(request, owner)
        if rest == ["mailFolders"] and method == "GET":
            return self._folders(request, owner)
        folder: wire.MailFolderName | None = None
        if rest[:1] == ["mailFolders"] and len(rest) >= 2:
            folder = self.folder(owner, rest[1])
            if len(rest) == 2 and method == "GET":
                return self._one(
                    request,
                    self._folder_of(owner, folder),
                    f"{GRAPH}/$metadata#users('{owner.user.id}')/mailFolders/$entity",
                )
            rest = rest[2:]
        if rest == ["messages"] and method == "GET":  # enum-lint: exempt Graph's path segment
            return self._list(request, owner, folder)
        if rest == ["messages"] and method == "POST":  # enum-lint: exempt HTTP's method name
            if folder is not None:
                raise NotServed("POST of a message to a folder: only POST /messages, a draft in Drafts, is served")
            return await self._create_draft(request, owner)
        if (
            rest in (["messages", "delta"], ["messages", "delta()"]) and method == "GET"
        ):  # enum-lint: exempt Graph's path segment
            if folder is None:
                raise NotServed(
                    "delta over every folder's messages: only one folder's, mailFolders/{id}/messages/delta"
                )
            return self._delta(request, owner, folder)
        if len(rest) >= 2 and rest[0] == "messages":
            stored = self._found(owner, rest[1], folder)
            if len(rest) == 2 and method == "GET":
                self._world.saw(message_ref(stored.message.id), Operation.READ)
                return self._one(
                    request, stored.message, f"{GRAPH}/$metadata#users('{owner.user.id}')/messages/$entity"
                )
            if len(rest) == 2 and method == "PATCH":
                return await self._patch(request, owner, stored)
            if len(rest) == 2 and method == "DELETE":  # enum-lint: exempt HTTP's method name
                return self._delete(owner, stored)
            if len(rest) == 3 and rest[2] in ("reply", "replyAll") and method == "POST":
                return await self._reply(request, owner, stored, everyone=rest[2] == "replyAll")
            if len(rest) == 3 and method == "POST":  # enum-lint: exempt HTTP's method name
                action = {
                    "send": self._send_draft,
                    "createReply": partial(self._create_reply, everyone=False),
                    "createReplyAll": partial(self._create_reply, everyone=True),
                    "createForward": self._create_forward,
                    "forward": self._forward,
                    "move": self._move,
                    "copy": self._copy,
                    "attachments": self._attach,
                }
                if rest[2] in action:
                    return await action[rest[2]](request, owner, stored)
            if rest[2:3] == ["attachments"] and method == "GET":
                return self._attachments(request, owner, stored, rest[3:])
        raise NotServed(f"{method} /{'/'.join(parts)}")

    def _one(self, request: Request, entity: wire.Aliased, context: str, status: int = 200) -> Response:
        fields = [f for f in (query(request, "$select") or "").split(",") if f] or None
        body = wire.dump(self._shown(request, entity) if isinstance(entity, wire.MailMessage) else entity)
        return Response(
            wire.select(wire.with_context(body, context), fields),
            status_code=status,
            media_type=GRAPH_JSON,
            headers=self._applied(request),
        )

    def _applied(self, request: Request) -> dict[str, str] | None:
        return preference_applied(request)

    def _shown(self, request: Request, message: wire.MailMessage) -> wire.MailMessage:
        return message.model_copy(update={"body": body_as_asked(request, message.body)})

    # ------------------------------------------------------------------ folders

    def folder(self, owner: UserRecord, key: str) -> wire.MailFolderName:
        wanted = key.lower()
        for name in wire.MailFolderName:
            if wanted == name.value or key == folder_id(owner.user.id, name):
                return name
        if wanted in WELL_KNOWN_FOLDERS:
            raise NotServed(f"the mail folder {key!r}: only Inbox, Sent Items, Drafts and Deleted Items are held")
        raise NotServed(f"the mail folder {key!r}, which the mailbox does not hold: Graph's answer is not recorded")

    def _folder_of(self, owner: UserRecord, folder: wire.MailFolderName) -> wire.MailFolder:
        held = [m for m in self._world.mails(owner.user.id) if m.folder is folder]
        return wire.MailFolder(
            id=folder_id(owner.user.id, folder),
            displayName=FOLDER_NAMES[folder],
            parentFolderId=outlook_id(owner.user.id, "folder", "root"),
            unreadItemCount=sum(1 for m in held if not m.message.isRead),
            totalItemCount=len(held),
        )

    def _folders(self, request: Request, owner: UserRecord) -> Response:
        page = wire.Page[wire.MailFolder](
            context=f"{GRAPH}/$metadata#users('{owner.user.id}')/mailFolders",
            value=sorted((self._folder_of(owner, f) for f in wire.MailFolderName), key=lambda f: f.displayName),
        )
        return Response(wire.dump(page), media_type=GRAPH_JSON)

    def _found(self, owner: UserRecord, message: str, folder: wire.MailFolderName | None) -> wire.StoredMail:
        found = self._world.mail(message)
        if found is None or found[0].user.id != owner.user.id or (folder is not None and found[1].folder is not folder):
            raise GraphRefusal(404, "ErrorItemNotFound", "The specified object was not found in the store.")
        return found[1]

    # ------------------------------------------------------------------ listing

    @staticmethod
    def _clauses(text: str) -> list[_Clause]:
        if re.search(r"\s+or\s+|\bnot\b", text, flags=re.IGNORECASE):
            raise NotServed(f"$filter with 'or' or 'not' on messages: {text}")
        found: list[_Clause] = []
        for part in re.split(r"\s+and\s+(?=(?:[^']*'[^']*')*[^']*$)", text.strip(), flags=re.IGNORECASE):
            flag = re.fullmatch(r"\s*(isRead|hasAttachments|isDraft)\s+(eq|ne)\s+(true|false)\s*", part)
            date = re.fullmatch(rf"\s*({'|'.join(_DATES)})\s+(eq|ne|gt|ge|lt|le)\s+('?[0-9][0-9TZ:.+\-]*'?)\s*", part)
            address = re.fullmatch(r"\s*((?:from|sender)/emailAddress/address)\s+(eq|ne)\s+'((?:[^']|'')*)'\s*", part)
            text_eq = re.fullmatch(r"\s*(conversationId|subject|id|importance)\s+(eq|ne)\s+'((?:[^']|'')*)'\s*", part)
            match = flag or date or address or text_eq
            if match is None:
                raise NotServed(f"$filter clause on messages: {part.strip()}")
            found.append(_Clause(prop=match.group(1), op=match.group(2), value=match.group(3).replace("''", "'")))
        return found

    @staticmethod
    def _holds(clause: _Clause, message: wire.MailMessage) -> bool:
        compare = _COMPARE[clause.op]
        if clause.prop in ("isRead", "hasAttachments", "isDraft"):
            return compare(getattr(message, clause.prop), clause.value == "true")
        if clause.prop in _DATES:
            return compare(stamp(getattr(message, clause.prop)), instant(clause.value))
        if clause.prop.endswith("/emailAddress/address"):
            who = message.from_ if clause.prop.startswith("from") else message.sender
            return compare(who.emailAddress.address.lower() if who is not None else None, clause.value.lower())
        return compare(str(getattr(message, clause.prop)), clause.value)

    @staticmethod
    def _order(text: str | None) -> list[tuple[str, bool]]:
        if not text:
            return []
        found: list[tuple[str, bool]] = []
        for part in text.split(","):
            prop, _, direction = part.strip().partition(" ")
            if prop not in (*_DATES, "subject") or direction.strip().lower() not in ("", "asc", "desc"):
                raise NotServed(f"$orderby on messages: {part.strip()}")
            found.append((prop, direction.strip().lower() == "desc"))
        return found

    def _list(self, request: Request, owner: UserRecord, folder: wire.MailFolderName | None) -> Response:
        for option in ("$search", "$expand", "$count", "$skiptoken"):
            if option in request.query_params:
                raise NotServed(f"{option} on messages")
        clauses = self._clauses(query(request, "$filter") or "") if query(request, "$filter") else []
        order = self._order(query(request, "$orderby"))
        if clauses and order and [c.prop for c in clauses[: len(order)]] != [p for p, _ in order]:
            raise GraphRefusal(
                400, "InefficientFilter", "The restriction or sort order is too complex for this operation."
            )
        messages = [
            m.message
            for m in self._world.mails(owner.user.id)
            if (folder is None or m.folder is folder) and all(self._holds(c, m.message) for c in clauses)
        ]
        if not order and len(messages) > 1:
            raise NotServed("listing messages without $orderby: Graph documents no order for them (user-list-messages)")
        for prop, descending in reversed(order):
            messages.sort(key=lambda m, prop=prop: getattr(m, prop) or "", reverse=descending)
        top = query(request, "$top")
        skip = query(request, "$skip")
        if (top is not None and (not top.isdigit() or not 1 <= int(top) <= PAGE_MAX)) or (
            skip is not None and not skip.isdigit()
        ):
            raise NotServed(f"$top={top} $skip={skip}: the page names $top 1 to {PAGE_MAX}, and no answer to more")
        size, offset = int(top) if top else PAGE_DEFAULT, int(skip) if skip else 0
        page = [self._shown(request, m) for m in messages[offset : offset + size]]
        where = f"mailFolders('{folder.value}')/messages" if folder is not None else "messages"
        following = None
        if offset + size < len(messages):
            kept = {k: request.query_params[k] for k in ("$filter", "$orderby", "$select") if k in request.query_params}
            params = urlencode(
                {**kept, "$top": str(size), "$skip": str(offset + size)}, quote_via=quote, safe="$'/:,()"
            )
            following = f"{GRAPH}/users/{owner.user.id}/{where}?{params}"
        self._world.saw(user_ref(owner.user.id), Operation.SEARCH)
        body = wire.dump(
            wire.Page[wire.MailMessage](
                context=f"{GRAPH}/$metadata#users('{owner.user.id}')/{where}", value=page, next_link=following
            )
        )
        fields = [f for f in (query(request, "$select") or "").split(",") if f] or None
        return Response(wire.select_page(body, fields), media_type=GRAPH_JSON, headers=self._applied(request))

    # ------------------------------------------------------------------ delta

    @staticmethod
    def _token(*numbers: int) -> str:
        return base64.urlsafe_b64encode(".".join(str(n) for n in numbers).encode()).decode().rstrip("=")

    @staticmethod
    def _numbers(token: str, count: int) -> list[int]:
        try:
            found = [int(n) for n in base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode().split(".")]
        except (binascii.Error, ValueError, UnicodeDecodeError) as e:
            raise bad_request("The delta or skip token is not valid.") from e
        if len(found) != count:
            raise bad_request("The delta or skip token is not valid.")
        return found

    def _delta(self, request: Request, owner: UserRecord, folder: wire.MailFolderName) -> Response:
        for option in ("$filter", "$orderby", "$top", "$search", "$expand"):
            if option in request.query_params:
                raise NotServed(f"{option} on a message delta")
        skip, delta = query(request, "$skiptoken"), query(request, "$deltatoken")
        since, offset = self._numbers(skip, 2) if skip else ([self._numbers(delta, 1)[0], 0] if delta else [0, 0])
        changed: list[tuple[int, str]] = []
        for stored in self._world.mail_versions(owner.user.id):
            if stored.seq <= since:
                continue
            mail = wire.parse(wire.StoredMail, stored.body)
            if mail.folder is folder:
                changed.append((stored.seq, wire.dump(self._shown(request, mail.message))))
            elif since and self._was_in(stored.entity.external_id, folder, since):
                changed.append((stored.seq, json.dumps({"id": mail.message.id, "@removed": {"reason": "deleted"}})))
        changed.sort(key=lambda pair: pair[0])
        prefer = request.headers["prefer"] if "prefer" in request.headers else ""
        wanted = re.search(r"odata\.maxpagesize\s*=\s*(\d+)", prefer)
        size = max(1, int(wanted.group(1))) if wanted else PAGE_DEFAULT
        page = changed[offset : offset + size]
        link = f"{GRAPH}/users/{owner.user.id}/mailFolders('{folder.value}')/messages/delta"
        select = query(request, "$select")
        kept = f"$select={quote(select, safe=',')}&" if select else ""
        if offset + size < len(changed):
            closing = f'"@odata.nextLink":{json.dumps(f"{link}?{kept}$skiptoken={self._token(since, offset + size)}")}'
        else:
            closing = (
                f'"@odata.deltaLink":{json.dumps(f"{link}?{kept}$deltatoken={self._token(self._world.store.head())}")}'
            )
        context = f"{GRAPH}/$metadata#Collection(message)"
        body = (
            "{"
            + f'"@odata.context":{json.dumps(context)},"value":['
            + ",".join(i for _, i in page)
            + "],"
            + closing
            + "}"
        )
        self._world.saw(user_ref(owner.user.id), Operation.SEARCH)
        fields = [f for f in (select or "").split(",") if f] or None
        return Response(wire.select_page(body, fields), media_type=GRAPH_JSON, headers=self._applied(request))

    def _was_in(self, message: str, folder: wire.MailFolderName, since: int) -> bool:
        before = [v for v in self._world.store.versions(message_ref(message)) if v.seq <= since]
        return bool(before) and wire.parse(wire.StoredMail, before[-1].body).folder is folder

    # ------------------------------------------------------------------ changing

    async def _patch(self, request: Request, owner: UserRecord, stored: wire.StoredMail) -> Response:
        """Whether a message is read; of a draft also its subject, body, recipients and importance, which the page
        says are "updatable only if isDraft = true" (message-update)."""
        try:
            asked = wire.read(wire.MessagePatch, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        if asked.model_extra:
            raise NotServed(f"PATCH of {', '.join(sorted(asked.model_extra))} on a message")
        given = asked.model_fields_set - {"isRead"}
        if given and not stored.message.isDraft:
            raise NotServed(
                f"PATCH of {', '.join(sorted(given))} on a message that is no draft: updatable only if isDraft = true, "
                "and Graph's answer to it is not documented"
            )
        changes: dict[str, object] = {}
        if asked.isRead is not None and asked.isRead != stored.message.isRead:
            changes["isRead"] = asked.isRead
        if "subject" in given:
            changes["subject"] = asked.subject
        if asked.body is not None:
            body = self._body(asked.body)
            changes["body"] = body
            changes["bodyPreview"] = plain(body)[:255]
        for name, sent in (
            ("toRecipients", asked.toRecipients),
            ("ccRecipients", asked.ccRecipients),
            ("bccRecipients", asked.bccRecipients),
            ("replyTo", asked.replyTo),
        ):
            if sent is not None:
                changes[name] = self._recipients(sent, name)
        if asked.importance is not None:
            if asked.importance.lower() not in ("low", "normal", "high"):
                raise NotServed(f"the importance {asked.importance!r}: message names low, normal and high")
            changes["importance"] = asked.importance.lower()
        changed = stored
        if changes:
            message = versioned(stored.message, graph_time(self._clock.now()), **changes)
            changed = stored.model_copy(update={"message": message})
            self._rewrite(owner.user.id, changed, actor=Actor.AGENT)
            await self.notify(owner.user.id, changed, "updated")
        return self._one(request, changed.message, f"{GRAPH}/$metadata#users('{owner.user.id}')/messages/$entity")

    def _delete(self, owner: UserRecord, stored: wire.StoredMail) -> Response:
        """Into Deleted Items, as Graph moves it; from Deleted Items, gone."""
        if stored.folder is wire.MailFolderName.DELETED:
            self._world.remove(
                message_ref(stored.message.id), actor=Actor.AGENT, parent=MAILBOX.format(user=owner.user.id)
            )
            return Response(status_code=204)
        message = versioned(
            stored.message,
            graph_time(self._clock.now()),
            parentFolderId=folder_id(owner.user.id, wire.MailFolderName.DELETED),
        )
        moved = stored.model_copy(update={"message": message, "folder": wire.MailFolderName.DELETED})
        self._rewrite(owner.user.id, moved, actor=Actor.AGENT)
        return Response(status_code=204)

    def _recipients(self, sent: list[wire.SentRecipient], field: str) -> list[wire.Recipient]:
        found: list[wire.Recipient] = []
        for r in sent:
            if not r.emailAddress.address or "@" not in r.emailAddress.address:
                raise GraphRefusal(400, "ErrorInvalidRecipients", RECIPIENTS_INVALID)
            found.append(
                wire.Recipient(emailAddress=wire.EmailAddress(name=r.emailAddress.name, address=r.emailAddress.address))
            )
        return found

    @staticmethod
    def _body(sent: wire.SentBody | None) -> wire.ItemBody:
        if sent is None:
            return wire.ItemBody(contentType="text", content="")
        kind = sent.contentType.lower()
        if kind not in ("text", "html"):
            raise NotServed(f"the body content type {sent.contentType!r}: Graph's bodyType is text or html")
        return wire.ItemBody(contentType=kind, content=sent.content)  # type: ignore[arg-type]

    @staticmethod
    def _unread(
        sent: wire.SentMessage | None, asked: wire.SendMailRequest | wire.ReplyRequest | wire.ForwardRequest | None
    ) -> None:
        """Refuse by name every property of the request or its message this provider would otherwise drop."""
        names: list[str] = sorted(
            {str(n) for n in ((asked.model_extra or {}) if asked else {})}
            | {str(n) for n in ((sent.model_extra or {}) if sent else {})}
        )
        if names:
            raise NotServed(f"the message properties {', '.join(names)}: they would not be kept as sent")

    async def _send_mail(self, request: Request, owner: UserRecord) -> Response:
        try:
            asked = wire.read(wire.SendMailRequest, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        if asked.message is None:
            raise NotServed("sendMail without a message: the page names it required and no answer without it")
        self._unread(asked.message, asked)
        if not asked.saveToSentItems:
            raise NotServed("sendMail with saveToSentItems false: the sent copy is the message a run reads")
        sent = asked.message
        to, cc, bcc = (
            self._recipients(sent.toRecipients, "toRecipients"),
            self._recipients(sent.ccRecipients, "ccRecipients"),
            self._recipients(sent.bccRecipients, "bccRecipients"),
        )
        if not (to or cc or bcc):
            raise GraphRefusal(400, "ErrorInvalidRecipients", RECIPIENTS_INVALID)
        await self.send(
            Composed(
                sender=owner,
                subject=sent.subject,
                body=self._body(sent.body),
                to=to,
                cc=cc,
                bcc=bcc,
                reply_to=self._recipients(sent.replyTo, "replyTo"),
                conversation=outlook_id(owner.user.id, "conversation", str(self._world.next_seq())),
                importance=sent.importance,
                attachments=self._stored_attachments(sent.attachments),
            ),
            actor=Actor.AGENT,
        )
        return Response(status_code=202)

    def _reply_composed(
        self, owner: UserRecord, original: wire.MailMessage, asked: wire.ReplyRequest, *, everyone: bool, draft: bool
    ) -> Composed:
        """A reply goes to the message's `replyTo` when it names any, else to its sender; a reply to all also to
        every recipient of the message (message-reply, message-replyall). `message` properties replace the reply's
        own; specifying both a comment and the message's body is 400."""
        written = asked.message
        self._unread(written, asked)
        if asked.comment is not None and written is not None and written.body is not None:
            raise GraphRefusal(400, None, "Specify either a comment or the body of the message, not both.")
        if original.from_ is None:
            raise NotServed("a reply to a draft, which has no sender: Graph's answer is not documented")
        to = list(original.replyTo) or [original.from_]
        cc: list[wire.Recipient] = []
        if everyone:
            to = [*to, *original.toRecipients]
            cc = list(original.ccRecipients)
        if written is not None and written.toRecipients:
            to = self._recipients(written.toRecipients, "toRecipients")
        if written is not None and written.ccRecipients:
            cc = self._recipients(written.ccRecipients, "ccRecipients")
        said = plain(self._body(written.body)) if written is not None and written.body is not None else asked.comment
        return Composed(
            sender=owner,
            subject=written.subject if written is not None and written.subject else re_subject(original.subject),
            body=None,
            said=said or "",
            to=to,
            cc=cc,
            bcc=self._recipients(written.bccRecipients, "bccRecipients") if written is not None else [],
            reply_to=self._recipients(written.replyTo, "replyTo") if written is not None else [],
            conversation=original.conversationId,
            importance=written.importance if written is not None else "normal",
            draft=draft,
        )

    async def _reply(self, request: Request, owner: UserRecord, stored: wire.StoredMail, *, everyone: bool) -> Response:
        try:
            asked = wire.read(wire.ReplyRequest, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        await self.send(
            self._reply_composed(owner, stored.message, asked, everyone=everyone, draft=False), actor=Actor.AGENT
        )
        return Response(status_code=202)

    # ------------------------------------------------------------------ drafts

    def put_draft(self, composed: Composed) -> wire.StoredMail:
        """A draft in the owner's Drafts folder: told to nobody, so it opens no wait for anyone."""
        owner = composed.sender.user.id
        message = self._message(composed, owner, wire.MailFolderName.DRAFTS, read=True, seeded=None)
        stored = wire.StoredMail(
            message=message,
            folder=wire.MailFolderName.DRAFTS,
            said=composed.said,
            attachments=self._attached(message, composed.attachments),
        )
        self._world.write_mail(owner, stored, operation=Operation.CREATE, actor=Actor.AGENT, after=None)
        return stored

    async def _made_draft(self, request: Request, owner: UserRecord, composed: Composed) -> Response:
        stored = self.put_draft(composed)
        await self.notify(owner.user.id, stored, "created")
        return self._one(
            request, stored.message, f"{GRAPH}/$metadata#users('{owner.user.id}')/messages/$entity", status=201
        )

    async def _create_draft(self, request: Request, owner: UserRecord) -> Response:
        """`POST /messages`: a draft in Drafts, 201 with the message, `isDraft` true (user-post-messages)."""
        if (
            request.headers["content-type"].split(";")[0].strip().lower() != "application/json"
            if "content-type" in request.headers
            else False
        ):
            raise NotServed("a draft in MIME format (Content-Type: text/plain): only JSON is served")
        try:
            sent = wire.read(wire.SentMessage, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        self._unread(sent, None)
        return await self._made_draft(
            request,
            owner,
            Composed(
                sender=owner,
                subject=sent.subject,
                body=self._body(sent.body),
                to=self._recipients(sent.toRecipients, "toRecipients"),
                cc=self._recipients(sent.ccRecipients, "ccRecipients"),
                bcc=self._recipients(sent.bccRecipients, "bccRecipients"),
                reply_to=self._recipients(sent.replyTo, "replyTo"),
                conversation=outlook_id(owner.user.id, "conversation", str(self._world.next_seq())),
                importance=sent.importance,
                attachments=self._stored_attachments(sent.attachments),
                draft=True,
            ),
        )

    async def _create_reply(
        self, request: Request, owner: UserRecord, stored: wire.StoredMail, *, everyone: bool
    ) -> Response:
        """`createReply`, `createReplyAll`: a draft that answers as `reply` and `replyAll` send (message-createreply,
        message-createreplyall)."""
        try:
            asked = wire.read(wire.ReplyRequest, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        return await self._made_draft(
            request, owner, self._reply_composed(owner, stored.message, asked, everyone=everyone, draft=True)
        )

    def _forward_composed(
        self, owner: UserRecord, stored: wire.StoredMail, asked: wire.ForwardRequest, *, draft: bool
    ) -> Composed:
        """A forward names its recipients once, as the parameter or as the message's `toRecipients`, and holds a
        comment or the message's body, not both (message-forward, message-createforward: 400 otherwise)."""
        written = asked.message
        self._unread(written, asked)
        if asked.comment is not None and written is not None and written.body is not None:
            raise GraphRefusal(400, None, "Specify either a comment or the body of the message, not both.")
        named = (asked.toRecipients or []) if asked.toRecipients else []
        inside = written.toRecipients if written is not None else []
        if bool(named) == bool(inside):
            raise GraphRefusal(
                400,
                None,
                "Specify either the toRecipients parameter or the toRecipients property of the message parameter.",
            )
        if stored.attachments:
            raise NotServed("forwarding a message that has attachments: the pages do not say whether they travel")
        said = plain(self._body(written.body)) if written is not None and written.body is not None else asked.comment
        return Composed(
            sender=owner,
            subject=written.subject if written is not None and written.subject else None,
            body=None,
            said=said or "",
            to=self._recipients(named or inside, "toRecipients"),
            cc=self._recipients(written.ccRecipients, "ccRecipients") if written is not None else [],
            bcc=self._recipients(written.bccRecipients, "bccRecipients") if written is not None else [],
            reply_to=self._recipients(written.replyTo, "replyTo") if written is not None else [],
            conversation=outlook_id(owner.user.id, "conversation", str(self._world.next_seq())),
            importance=written.importance if written is not None else "normal",
            draft=draft,
        )

    async def _create_forward(self, request: Request, owner: UserRecord, stored: wire.StoredMail) -> Response:
        try:
            asked = wire.read(wire.ForwardRequest, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        return await self._made_draft(request, owner, self._forward_composed(owner, stored, asked, draft=True))

    async def _forward(self, request: Request, owner: UserRecord, stored: wire.StoredMail) -> Response:
        try:
            asked = wire.read(wire.ForwardRequest, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        await self.send(self._forward_composed(owner, stored, asked, draft=False), actor=Actor.AGENT)
        return Response(status_code=202)

    async def _send_draft(self, request: Request, owner: UserRecord, stored: wire.StoredMail) -> Response:
        """`send`: the draft goes to its recipients and is kept in Sent Items; 202, no body (message-send). It leaves
        Drafts, and, as an item moved from one folder to another, takes a new id (resources/message: `id` changes
        when the item is moved)."""
        del request
        message = stored.message
        if not message.isDraft:
            raise NotServed("send of a message that is no draft: Graph's answer is not documented")
        if not (message.toRecipients or message.ccRecipients or message.bccRecipients):
            raise GraphRefusal(400, "ErrorInvalidRecipients", RECIPIENTS_INVALID)
        self._world.remove(message_ref(message.id), actor=Actor.AGENT, parent=MAILBOX.format(user=owner.user.id))
        await self.send(
            Composed(
                sender=owner,
                subject=message.subject,
                body=message.body,
                said=stored.said,
                to=message.toRecipients,
                cc=message.ccRecipients,
                bcc=message.bccRecipients,
                reply_to=message.replyTo,
                conversation=message.conversationId,
                importance=message.importance,
                attachments=stored.attachments,
            ),
            actor=Actor.AGENT,
        )
        return Response(status_code=202)

    # ------------------------------------------------------------------ move, copy

    async def _relocate(self, request: Request, owner: UserRecord, stored: wire.StoredMail, *, keep: bool) -> Response:
        try:
            asked = wire.read(wire.DestinationRequest, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        if asked.model_extra:
            raise NotServed(f"the properties {', '.join(sorted(asked.model_extra))} of a move or copy")
        destination = self.folder(owner, asked.destinationId)
        now = graph_time(self._clock.now())
        new_id = outlook_id(owner.user.id, "relocated", stored.message.id, str(self._world.next_seq()))
        message = versioned(
            stored.message,
            now,
            id=new_id,
            parentFolderId=folder_id(owner.user.id, destination),
            webLink=f"https://outlook.office365.com/owa/?ItemID={quote(new_id)}&exvsurl=1&viewmodel=ReadMessageItem",
        )
        made = stored.model_copy(
            update={
                "message": message,
                "folder": destination,
                "attachments": self._attached(message, stored.attachments),
            }
        )
        if not keep:
            self._world.remove(
                message_ref(stored.message.id), actor=Actor.AGENT, parent=MAILBOX.format(user=owner.user.id)
            )
        self._world.write_mail(owner.user.id, made, operation=Operation.CREATE, actor=Actor.AGENT, after=None)
        await self.notify(owner.user.id, made, "created")
        return self._one(
            request, made.message, f"{GRAPH}/$metadata#users('{owner.user.id}')/messages/$entity", status=201
        )

    async def _move(self, request: Request, owner: UserRecord, stored: wire.StoredMail) -> Response:
        """`move`: a new copy in the destination folder and the original gone, 201 with the copy (message-move)."""
        return await self._relocate(request, owner, stored, keep=False)

    async def _copy(self, request: Request, owner: UserRecord, stored: wire.StoredMail) -> Response:
        """`copy`: a copy in the destination folder, the original kept, 201 with the copy (message-copy)."""
        return await self._relocate(request, owner, stored, keep=True)

    # ------------------------------------------------------------------ attachments

    @staticmethod
    def _stored_attachment(sent: wire.SentAttachment, attachment_id: str, now: str) -> wire.StoredAttachment:
        """A file attachment as sent: refuses by name what is not a `fileAttachment` of under 3 MB with base64
        bytes (message-post-attachments)."""
        if sent.model_extra:
            raise NotServed(f"the attachment properties {', '.join(sorted(sent.model_extra))}")
        if sent.odata_type.lower().lstrip("#") != "microsoft.graph.fileattachment":
            raise NotServed(f"an attachment of the type {sent.odata_type!r}: only fileAttachment is held")
        try:
            content = base64.b64decode(sent.contentBytes, validate=True)
        except binascii.Error as e:
            raise NotServed("an attachment whose contentBytes are not base64: Graph's answer is not documented") from e
        if len(content) >= ATTACHMENT_LIMIT:
            raise NotServed(
                "an attachment of 3 MB or more: the page sends it through an upload session, which is not served"
            )
        return wire.StoredAttachment(
            id=attachment_id,
            lastModifiedDateTime=now,
            name=sent.name,
            contentType=sent.contentType,
            size=len(content),
            isInline=sent.isInline,
            contentId=sent.contentId,
            contentBytes=sent.contentBytes,
        )

    def _stored_attachments(self, sent: list[wire.SentAttachment]) -> list[wire.StoredAttachment]:
        now = graph_time(self._clock.now())
        return [self._stored_attachment(a, "", now) for a in sent]

    async def _attach(self, request: Request, owner: UserRecord, stored: wire.StoredMail) -> Response:
        """`POST …/attachments`: a file attached to a draft, 201 with the attachment."""
        try:
            sent = wire.read(wire.SentAttachment, await request.body())
        except wire.Unreadable as e:
            raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e
        if not stored.message.isDraft:
            raise NotServed("an attachment added to a message that is no draft: Graph's answer is not documented")
        now = graph_time(self._clock.now())
        made = self._stored_attachment(
            sent, outlook_id(stored.message.id, "attachment", str(len(stored.attachments)), now), now
        )
        message = versioned(
            stored.message, now, hasAttachments=any(not a.isInline for a in [*stored.attachments, made])
        )
        changed = stored.model_copy(update={"message": message, "attachments": [*stored.attachments, made]})
        self._rewrite(owner.user.id, changed, actor=Actor.AGENT)
        await self.notify(owner.user.id, changed, "updated")
        context = f"{GRAPH}/$metadata#users('{owner.user.id}')/messages('{stored.message.id}')/attachments/$entity"
        return Response(wire.with_context(wire.dump(made), context), status_code=201, media_type=GRAPH_JSON)

    def _attachments(self, request: Request, owner: UserRecord, stored: wire.StoredMail, rest: list[str]) -> Response:
        """`GET …/attachments` and `GET …/attachments/{id}`."""
        for option in ("$filter", "$search", "$expand", "$count", "$top", "$skip", "$skiptoken"):
            if option in request.query_params:
                raise NotServed(f"{option} on attachments")
        fields = [f for f in (query(request, "$select") or "").split(",") if f] or None
        where = f"{GRAPH}/$metadata#users('{owner.user.id}')/messages('{stored.message.id}')/attachments"
        self._world.saw(message_ref(stored.message.id), Operation.READ)
        if rest:
            found = next((a for a in stored.attachments if a.id == rest[0]), None)
            if found is None:
                raise GraphRefusal(404, "ErrorItemNotFound", "The specified object was not found in the store.")
            return Response(
                wire.select(wire.with_context(wire.dump(found), f"{where}/$entity"), fields), media_type=GRAPH_JSON
            )
        held = list(stored.attachments)
        ordered = query(request, "$orderby")
        if ordered:
            prop, _, direction = ordered.strip().partition(" ")
            if prop not in ATTACHMENT_ORDER or direction.strip().lower() not in (
                "",
                "asc",
                "desc",
            ):
                raise NotServed(f"$orderby on attachments: {ordered}")
            held.sort(key=lambda a: getattr(a, prop) or "", reverse=direction.strip().lower() == "desc")
        elif len(held) > 1:
            raise NotServed(
                "listing attachments without $orderby: Graph documents no order for them (message-list-attachments)"
            )
        body = wire.dump(wire.Page[wire.StoredAttachment](context=where, value=held))
        return Response(wire.select_page(body, fields), media_type=GRAPH_JSON)
