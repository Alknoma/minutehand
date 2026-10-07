"""Google Workspace, driven through Drive v3 (https://developers.google.com/workspace/drive/api/reference/rest/v3),
Docs v1 (https://developers.google.com/workspace/docs/api/reference/rest), Gmail v1
(https://developers.google.com/workspace/gmail/api/reference/rest) and Calendar v3
(https://developers.google.com/workspace/calendar/api/v3/reference), as an installed app's client calls them:
it signs in at `https://oauth2.googleapis.com/token` with a refresh token (the `refresh_token` grant,
https://developers.google.com/identity/protocols/oauth2/native-app#offline), sends the access token it is answered
as a bearer, and reads every answer as Google documents it. Every call on a file says it supports shared drives
(`supportsAllDrives=true`), as Google asks of every client that may meet one
(https://developers.google.com/workspace/drive/api/guides/enable-shareddrives).

Drive is its documents family; Gmail its messaging family, whose one conversation is the mail between the agent's
mailbox and a person's address (Gmail has no channel, and a sent message cannot be edited or reacted to); Calendar
is in no family, and is read where the agent's answers are read (`observe`) and faulted (`faults`).

A world is reached by its refresh tokens, its own by `tag`: one the agent signs in with (as the scenario's owner,
whose Drive the agent works in) and one per person, each declared as a Google sign-in for that person, so the world
answers exactly these and nothing else.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import google_auth_httplib2
import httplib2
import httpx
import socks
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt
from google.auth import jwt as google_jwt
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.domain.scenario import AccessRole, DocumentKind, Seed
from tests.conformance.contract import (
    Api,
    ChannelSeen,
    CommentSeen,
    Documents,
    DocumentSeen,
    Driver,
    FaultCase,
    IdKind,
    MessageSeen,
    Messaging,
    PersonSeen,
    Session,
    ok,
)

PROVIDER = "google_workspace"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DRIVE = "https://www.googleapis.com/drive/v3/"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents/"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/"
CALENDAR = "https://www.googleapis.com/calendar/v3/"
CLIENT_ID = "conformance.apps.googleusercontent.com"
CLIENT_SECRET = "conformance-client-secret"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]
UNSEEDED = "ya29.a0unseededaccesstokennobodywasevergiven0000"
"""An access token of Google's shape (`ya29.`) that no world's token endpoint minted."""

FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
GOOGLE_APPS = "application/vnd.google-apps."
KINDS = {
    GOOGLE_DOC: DocumentKind.DOCUMENT,
    GOOGLE_SHEET: DocumentKind.SPREADSHEET,
    GOOGLE_SLIDES: DocumentKind.PRESENTATION,
}
ROLES = {
    "reader": AccessRole.READER,
    "commenter": AccessRole.COMMENTER,
    "writer": AccessRole.WRITER,
    "organizer": AccessRole.ORGANIZER,
    # Drive's "content manager" in a shared drive: it may do everything an organizer may but manage members
    # (https://developers.google.com/workspace/drive/api/guides/ref-roles); the neutral roles' nearest is organizer.
    "fileOrganizer": AccessRole.ORGANIZER,
}

LISTING = 100
"""The page size this driver reads whole listings with: files.list's default (its range is 1 to 1000)."""
FILE_FIELDS = "id,name,mimeType,parents,driveId,trashed,createdTime,modifiedTime,owners(emailAddress),lastModifyingUser(emailAddress)"
OBSERVED_FIELDS = "nextPageToken,files(id,name,mimeType,parents,trashed,createdTime,modifiedTime)"
LISTED = f"trashed = false and mimeType != '{FOLDER}'"
"""Every file that is not a folder and not in the trash (https://developers.google.com/workspace/drive/api/guides/ref-search-terms)."""
BOUNDARY = "conformance_multipart_boundary_7f3a9c"
SECRET_FIELDS = ("refresh_token", "client_secret", "assertion")
"""The credentials a grant sends, each kept in `secrets` as written and as the form encodes it."""

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

Doc = Mapping[str, Any]
"""A JSON object as Google answered it, read only inside this driver."""


def agent_token(tag: str) -> str:
    """A refresh token of Google's shape (`1//`) for the agent's sign-in."""
    return f"1//0{tag}-agent"


def person_token(tag: str, person: str) -> str:
    return f"1//0{tag}-{person}"


def tag_of(world: WorldView) -> str:
    """The tag the world was made with, read back from the agent's refresh token (its first claim)."""
    first = world.claims.tokens[0]
    if not (first.startswith("1//0") and first.endswith("-agent")):
        raise LookupError(f"the world's first claim {first!r} is not this driver's agent token")
    return first.removeprefix("1//0").removesuffix("-agent")


def when(stamp: object) -> datetime:
    """A Drive timestamp, RFC 3339 in UTC with milliseconds and `Z` as Drive writes it; anything else is refused."""
    if not isinstance(stamp, str) or STAMP.match(stamp) is None:
        raise ValueError(f"{stamp!r} is not a Drive timestamp")
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def quoted(value: str) -> str:
    """A string literal in Drive's query language: `\\` and `'` escaped with a backslash."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _email(user: object) -> str | None:
    if isinstance(user, Mapping) and "emailAddress" in user:
        found = user["emailAddress"]
        return found if isinstance(found, str) else None
    return None


def _text_of(content: list[Doc]) -> str:
    """The characters of a Docs body's structural elements, tables' cells included, in order."""
    found: list[str] = []
    for element in content:
        if "paragraph" in element:
            for part in element["paragraph"]["elements"]:
                if "textRun" in part:
                    found.append(str(part["textRun"]["content"]))
        if "table" in element:
            for row in element["table"]["tableRows"]:
                for cell in row["tableCells"]:
                    found.append(_text_of(list(cell["content"])))
    return "".join(found)


def sign_in(http: httpx.Client, form: Mapping[str, str]) -> str:
    """An access token from Google's token endpoint for `form` (a grant), or `VendorRefused`."""
    answered = ok("oauth2 token", http.post(TOKEN_URI, data=dict(form))).json()
    return str(answered["access_token"])


def refresh_grant(refresh_token: str) -> dict[str, str]:
    return {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }


def service_account_grant(email: str) -> dict[str, str]:
    """The JWT-bearer grant a service account signs in with
    (https://developers.google.com/identity/protocols/oauth2/service-account#httprest), its assertion signed RS256
    with a key made here: the account's own key is Google's to hold, and the world holds none to check it with."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    now = int(time.time())
    claims = {"iss": email, "scope": " ".join(SCOPES), "aud": TOKEN_URI, "iat": now, "exp": now + 3600}
    assertion = google_jwt.encode(crypt.RSASigner.from_string(pem.decode()), claims).decode()
    return {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}


class WorkspaceSession(Documents, Messaging):
    def __init__(self, api: Api, grant: Mapping[str, str]) -> None:
        self._http = api.http()
        try:
            self._access = sign_in(self._http, grant)
        except BaseException:
            self._http.close()
            raise
        self._http.headers["Authorization"] = f"Bearer {self._access}"
        sent = [grant[k] for k in SECRET_FIELDS if k in grant]
        self.secrets = (*sent, *(quote(s, safe="") for s in sent), self._access, UNSEEDED)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ the APIs

    def _drive(self, what: str, method: str, path: str, **more: Any) -> httpx.Response:
        return ok(what, self._http.request(method, DRIVE + path, **more))

    def _json(self, what: str, method: str, path: str, **more: Any) -> Doc:
        answered = self._drive(what, method, path, **more).json()
        if not isinstance(answered, dict):
            raise TypeError(f"{what} answered no JSON object")
        return answered

    def _file_pages(self, q: str, fields: str, page_size: int, **more: str) -> list[list[Doc]]:
        """Every page of `files.list` for `q` across My Drive and every shared drive the account is in."""
        pages: list[list[Doc]] = []
        token = ""
        while True:
            params = {
                "q": q,
                "fields": f"nextPageToken,files({fields})",
                "pageSize": str(page_size),
                "corpora": "allDrives",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                **more,
                **({"pageToken": token} if token else {}),
            }
            answered = self._json("files.list", "GET", "files", params=params)
            pages.append(list(answered["files"]))
            token = str(answered["nextPageToken"]) if "nextPageToken" in answered else ""
            if not token:
                return pages

    def _files(self, q: str, fields: str, **more: str) -> list[Doc]:
        return [f for page in self._file_pages(q, fields, LISTING, **more) for f in page]

    def _get(self, file_id: str, fields: str) -> Doc:
        return self._json(
            "files.get", "GET", f"files/{file_id}", params={"fields": fields, "supportsAllDrives": "true"}
        )

    def _update(self, file_id: str, body: Mapping[str, object], **more: str) -> Doc:
        return self._json(
            "files.update",
            "PATCH",
            f"files/{file_id}",
            params={"supportsAllDrives": "true", "fields": "id", **more},
            json=dict(body),
        )

    def _folder(self, path: str, root: str, drive_id: str | None) -> str:
        """The folder at '/'-separated `path` under `root` (My Drive's alias `root`, or a shared drive's id), each
        level found by name in its parent or created there."""
        parent = root
        for name in [p for p in path.split("/") if p]:
            scope = {"corpora": "drive", "driveId": drive_id} if drive_id is not None else {"corpora": "user"}
            q = f"name = {quoted(name)} and {quoted(parent)} in parents and mimeType = '{FOLDER}' and trashed = false"
            found = self._json(
                "files.list",
                "GET",
                "files",
                params={
                    "q": q,
                    "fields": "files(id)",
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                    **scope,
                },
            )["files"]
            if found:
                parent = str(found[0]["id"])
                continue
            made = self._json(
                "files.create",
                "POST",
                "files",
                params={"supportsAllDrives": "true", "fields": "id"},
                json={"name": name, "mimeType": FOLDER, "parents": [parent]},
            )
            parent = str(made["id"])
        return parent

    # ------------------------------------------------------------------ accounts

    def whoami(self) -> str:
        """`about.get`'s user: Drive names an account by its email address."""
        about = self._json("about.get", "GET", "about", params={"fields": "user(displayName,emailAddress)"})
        return str(about["user"]["emailAddress"])

    def people(self) -> list[PersonSeen]:
        raise NotImplementedError(ABSENT_PEOPLE)

    def people_pages(self, page_size: int) -> list[list[str]]:
        raise NotImplementedError(ABSENT_PEOPLE)

    def unknown_credential(self) -> httpx.Response:
        return self._http.get(
            DRIVE + "about", params={"fields": "user(emailAddress)"}, headers={"Authorization": f"Bearer {UNSEEDED}"}
        )

    def observe(self) -> str:
        """About and every file through Drive, the mailbox's messages through Gmail, and the primary calendar's
        events through Calendar."""
        answered: list[str] = []
        for path, params in (
            ("about", {"fields": "user(displayName,emailAddress,permissionId)"}),
            (
                "files",
                {
                    "q": "trashed = false",
                    "fields": OBSERVED_FIELDS,
                    "orderBy": "name,createdTime",
                    "pageSize": "1000",
                    "corpora": "allDrives",
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                },
            ),
        ):
            answered.append(self._drive(f"GET {path}", "GET", path, params=params).text)
        answered.append(
            ok("users.messages.list", self._http.get(GMAIL + "messages", params={"maxResults": "500"})).text
        )
        events = CALENDAR + "calendars/primary/events"
        answered.append(ok("events.list", self._http.get(events, params={"maxResults": "2500"})).text)
        return "\n".join(answered)

    def change(self, label: str) -> None:
        self.create_document(label, None)

    # ------------------------------------------------------------------ documents

    def documents(self) -> list[DocumentSeen]:
        found = self._files(LISTED, FILE_FIELDS)
        folders = {
            str(f["id"]): f for f in self._files(f"mimeType = '{FOLDER}' and trashed = false", "id,name,parents")
        }
        spaces: dict[str, str] = {}
        if any("driveId" in f for f in found):
            spaces = {str(d["id"]): str(d["name"]) for d in self._drives()}
        return [self._seen(f, folders, spaces) for f in found]

    def _drives(self) -> list[Doc]:
        drives: list[Doc] = []
        token = ""
        while True:
            params = {
                "pageSize": "100",
                "fields": "nextPageToken,drives(id,name)",
                **({"pageToken": token} if token else {}),
            }
            answered = self._json("drives.list", "GET", "drives", params=params)
            drives += list(answered["drives"])
            token = str(answered["nextPageToken"]) if "nextPageToken" in answered else ""
            if not token:
                return drives

    def _seen(self, file: Doc, folders: Mapping[str, Doc], spaces: Mapping[str, str]) -> DocumentSeen:
        mime = str(file["mimeType"])
        owners = list(file["owners"]) if "owners" in file else []
        drive_id = str(file["driveId"]) if "driveId" in file else None
        return DocumentSeen(
            id=str(file["id"]),
            title=str(file["name"]),
            kind=KINDS[mime] if mime in KINDS else (None if mime.startswith(GOOGLE_APPS) else DocumentKind.FILE),
            folder=self._path(file, folders),
            owner_email=_email(owners[0]) if owners else None,
            space=spaces[drive_id] if drive_id is not None else None,
            shared=self._shared(str(file["id"])),
            last_editor_email=_email(file["lastModifyingUser"]) if "lastModifyingUser" in file else None,
            modified=when(file["modifiedTime"]),
            created=when(file["createdTime"]),
            trashed=file["trashed"] is True,
            mime_type=mime,
        )

    def _path(self, file: Doc, folders: Mapping[str, Doc]) -> str | None:
        """The names of the folders above the file, from the top. The climb stops at a parent `files.list` does not
        answer: My Drive and a shared drive's root are never listed, and nor is a folder the account cannot see
        (a file shared on its own sits, for the account it was shared with, at no path)."""
        names: list[str] = []
        parents = list(file["parents"]) if "parents" in file else []
        while parents and str(parents[0]) in folders:
            above = folders[str(parents[0])]
            names.append(str(above["name"]))
            parents = list(above["parents"]) if "parents" in above else []
        return "/".join(reversed(names)) or None

    def _shared(self, file_id: str) -> dict[str, AccessRole]:
        """Who the file is shared with, by email: every `user` permission but its owner's."""
        shared: dict[str, AccessRole] = {}
        token = ""
        while True:
            params = {
                "supportsAllDrives": "true",
                "fields": "nextPageToken,permissions(id,type,role,emailAddress)",
                **({"pageToken": token} if token else {}),
            }
            answered = self._json("permissions.list", "GET", f"files/{file_id}/permissions", params=params)
            for permission in answered["permissions"]:
                if permission["type"] == "user" and permission["role"] != "owner":
                    shared[str(permission["emailAddress"])] = ROLES[str(permission["role"])]
            token = str(answered["nextPageToken"]) if "nextPageToken" in answered else ""
            if not token:
                return shared

    def create_document(self, title: str, folder: str | None) -> str:
        """`files.create` with the Docs type and no media makes a blank Google Doc
        (https://developers.google.com/workspace/drive/api/guides/create-file)."""
        parents = [self._folder(folder, "root", None)] if folder else []
        made = self._json(
            "files.create",
            "POST",
            "files",
            params={"supportsAllDrives": "true", "fields": "id"},
            json={"name": title, "mimeType": GOOGLE_DOC, **({"parents": parents} if parents else {})},
        )
        return str(made["id"])

    def _document(self, document: str) -> Doc:
        response = ok("documents.get", self._http.get(DOCS + document))
        answered = response.json()
        if not isinstance(answered, dict):
            raise TypeError("documents.get answered no JSON object")
        return answered

    def write(self, document: str, text: str) -> None:
        """Everything between the body's first index and its final newline deleted, then `text` inserted at 1
        (https://developers.google.com/workspace/docs/api/how-tos/move-text)."""
        content = list(self._document(document)["body"]["content"])
        end = int(content[-1]["endIndex"])
        requests: list[dict[str, object]] = []
        if end - 1 > 1:
            requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end - 1}}})
        if text:
            requests.append({"insertText": {"location": {"index": 1}, "text": text}})
        if requests:
            ok("documents.batchUpdate", self._http.post(DOCS + f"{document}:batchUpdate", json={"requests": requests}))

    def read_text(self, document: str) -> str:
        """A Doc's body through the Docs API, without the final newline every body ends with; any other file's
        text through Drive: a sheet's CSV export, a deck's plain-text export, a binary file's bytes as UTF-8."""
        mime = str(self._get(document, "mimeType")["mimeType"])
        if mime == GOOGLE_DOC:
            return _text_of(list(self._document(document)["body"]["content"])).removesuffix("\n")
        if mime in (GOOGLE_SHEET, GOOGLE_SLIDES):
            exported = "text/csv" if mime == GOOGLE_SHEET else "text/plain"
            return self._drive(
                "files.export", "GET", f"files/{document}/export", params={"mimeType": exported}
            ).content.decode("utf-8-sig")
        return self.download(document).decode("utf-8")

    def rename(self, document: str, title: str) -> None:
        self._update(document, {"name": title})

    def move(self, document: str, folder: str) -> None:
        """`addParents`/`removeParents`, the only way Drive moves a file
        (https://developers.google.com/workspace/drive/api/guides/folder#move-files), within its own drive."""
        found = self._get(document, "id,parents,driveId")
        drive_id = str(found["driveId"]) if "driveId" in found else None
        target = self._folder(folder, drive_id if drive_id is not None else "root", drive_id)
        old = ",".join(str(p) for p in found["parents"]) if "parents" in found else ""
        self._update(document, {}, addParents=target, removeParents=old)

    def share(self, document: str, email: str, role: AccessRole) -> None:
        ok(
            "permissions.create",
            self._http.post(
                DRIVE + f"files/{document}/permissions",
                params={"supportsAllDrives": "true", "sendNotificationEmail": "false"},
                json={"type": "user", "role": role.value, "emailAddress": email},
            ),
        )

    def trash(self, document: str) -> None:
        self._update(document, {"trashed": True})

    def comments(self, document: str) -> list[CommentSeen]:
        """`comments.list`, which requires a field mask (https://developers.google.com/workspace/drive/api/guides/fields-parameter)."""
        seen: list[CommentSeen] = []
        token = ""
        while True:
            params = {
                "fields": "nextPageToken,comments(author(displayName,emailAddress),content,deleted)",
                "pageSize": "100",
                **({"pageToken": token} if token else {}),
            }
            answered = self._json("comments.list", "GET", f"files/{document}/comments", params=params)
            seen += [
                CommentSeen(
                    author_email=_email(c["author"]) if "author" in c else None,
                    text=str(c["content"]),
                    # Drive documents a comment author's email as never populated; its displayName is.
                    author_name=str(c["author"]["displayName"])
                    if "author" in c and "displayName" in c["author"]
                    else None,
                )
                for c in answered["comments"]
                if c["deleted"] is not True
            ]
            token = str(answered["nextPageToken"]) if "nextPageToken" in answered else ""
            if not token:
                return seen

    def document_pages(self, page_size: int) -> list[list[str]]:
        return [[str(f["id"]) for f in page] for page in self._file_pages(LISTED, "id", page_size)]

    def upload(self, name: str, content: bytes, mime_type: str) -> str:
        """A multipart upload: the metadata and the bytes in one `multipart/related` request
        (https://developers.google.com/workspace/drive/api/guides/manage-uploads#multipart)."""
        metadata = json.dumps({"name": name, "mimeType": mime_type}).encode()
        body = b"".join(
            [
                f"--{BOUNDARY}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                metadata,
                f"\r\n--{BOUNDARY}\r\nContent-Type: {mime_type}\r\n\r\n".encode(),
                content,
                f"\r\n--{BOUNDARY}--".encode(),
            ]
        )
        answered = ok(
            "files.create (upload)",
            self._http.post(
                UPLOAD,
                params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"},
                content=body,
                headers={"Content-Type": f"multipart/related; boundary={BOUNDARY}"},
            ),
        ).json()
        return str(answered["id"])

    def download(self, document: str) -> bytes:
        """`alt=media` (https://developers.google.com/workspace/drive/api/guides/manage-downloads)."""
        return self._drive(
            "files.get (media)", "GET", f"files/{document}", params={"alt": "media", "supportsAllDrives": "true"}
        ).content

    # ------------------------------------------------------------------ mail

    def person_id(self, email: str) -> str:
        """Gmail names a correspondent by address: a message's `From`, `To` and `Cc` hold addresses, and searching
        by `from:` and `to:` takes one (https://support.google.com/mail/answer/7190)."""
        return email

    def channel(self, name: str) -> str:
        raise NotImplementedError(ABSENT_CHANNELS)

    def channels(self) -> list[ChannelSeen]:
        raise NotImplementedError(ABSENT_CHANNELS)

    def direct(self, person: str) -> str:
        """The conversation with a person is the mail between the agent's mailbox and their address."""
        if "@" not in person:
            raise ValueError(f"{person!r} is not an address, which is how Gmail names a correspondent")
        return person

    def _gmail(self, what: str, method: str, path: str, **more: Any) -> Doc:
        answered = ok(what, self._http.request(method, GMAIL + path, **more)).json()
        if not isinstance(answered, dict):
            raise TypeError(f"{what} answered no JSON object")
        return answered

    def _send(self, to: str, text: str, *, subject: str = "", thread: Doc | None = None) -> str:
        """`users.messages.send` with the whole RFC 2822 message base64url in `raw`
        (https://developers.google.com/workspace/gmail/api/guides/sending); a reply names the thread and the message
        it answers, and keeps its subject, as Gmail's threading asks
        (https://developers.google.com/workspace/gmail/api/guides/threads)."""
        message = EmailMessage()
        message["To"] = to
        if subject:
            message["Subject"] = subject
        body: dict[str, str] = {}
        if thread is not None:
            message["In-Reply-To"] = str(thread["message_id"])
            message["References"] = str(thread["message_id"])
            body["threadId"] = str(thread["threadId"])
        message.set_content(text)
        body["raw"] = base64.urlsafe_b64encode(message.as_bytes()).decode()
        return str(self._gmail("users.messages.send", "POST", "messages/send", json=body)["id"])

    def post(self, channel: str, text: str) -> str:
        return self._send(self.direct(channel), text)

    def post_button(self, channel: str, text: str, action_id: str, label: str) -> str:
        raise NotImplementedError(ABSENT_BUTTON)

    def reply(self, channel: str, thread: str, text: str) -> str:
        answered = self._gmail(
            "users.messages.get",
            "GET",
            f"messages/{thread}",
            params={"format": "metadata", "metadataHeaders": ["Message-ID", "Subject"]},
        )
        headers = _headers(answered["payload"])
        subject = headers.get("subject", "")
        replied = subject if not subject or subject.lower().startswith("re:") else f"Re: {subject}"
        named = {"threadId": answered["threadId"], "message_id": headers["message-id"]}
        return self._send(self.direct(channel), text, subject=replied, thread=named)

    def edit(self, channel: str, message: str, text: str) -> None:
        raise NotImplementedError(ABSENT_EDIT)

    def react(self, channel: str, message: str, reaction: str) -> None:
        raise NotImplementedError(ABSENT_REACT)

    def delete_message(self, channel: str, message: str) -> None:
        """`users.messages.trash`: Gmail's way to take a message out of the mailbox's conversations, as its delete
        is permanent and needs the full mail scope
        (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/trash)."""
        self._gmail("users.messages.trash", "POST", f"messages/{message}/trash")

    def _listed(self, channel: str, page_size: int) -> list[list[str]]:
        """Every page of `users.messages.list` for the mail to or from the address, newest first."""
        address = self.direct(channel)
        pages: list[list[str]] = []
        token = ""
        while True:
            params = {
                "q": f"from:{address} OR to:{address}",
                "maxResults": str(page_size),
                **({"pageToken": token} if token else {}),
            }
            answered = self._gmail("users.messages.list", "GET", "messages", params=params)
            pages.append([str(m["id"]) for m in answered["messages"]] if "messages" in answered else [])
            token = str(answered["nextPageToken"]) if "nextPageToken" in answered else ""
            if not token:
                return pages

    def history(self, channel: str) -> list[MessageSeen]:
        """Each message to or from the address, read in full, oldest first. A message is in the thread of the
        thread's first message, whose id is the thread's (observed)."""
        found: list[MessageSeen] = []
        for message in reversed([m for page in self._listed(channel, LISTING) for m in page]):
            answered = self._gmail("users.messages.get", "GET", f"messages/{message}", params={"format": "full"})
            payload = answered["payload"]
            thread = str(answered["threadId"])
            found.append(
                MessageSeen(
                    id=str(answered["id"]),
                    text=_mail_text(payload).strip(),
                    author_email=parseaddr(_headers(payload).get("from", ""))[1] or None,
                    thread_of=thread if thread != str(answered["id"]) else None,
                    at=datetime.fromtimestamp(int(answered["internalDate"]) / 1000, UTC),
                    files=tuple(_files(payload)),
                )
            )
        return found

    def history_pages(self, channel: str, page_size: int) -> list[list[str]]:
        return self._listed(channel, page_size)


def _headers(part: Doc) -> dict[str, str]:
    """A MIME part's headers as Gmail serves them, by lowercase name."""
    return {str(h["name"]).lower(): str(h["value"]) for h in part["headers"]} if "headers" in part else {}


def _mail_text(part: Doc) -> str:
    """The first `text/plain` part's text, its `body.data` base64url as Gmail serves it."""
    if str(part["mimeType"]) == "text/plain" and "data" in part["body"]:
        data = str(part["body"]["data"])
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
    for child in part["parts"] if "parts" in part else []:
        found = _mail_text(child)
        if found:
            return found
    return ""


def _files(part: Doc) -> list[str]:
    filename = str(part["filename"]) if "filename" in part else ""
    named = [filename] if filename else []
    return named + [f for child in (part["parts"] if "parts" in part else []) for f in _files(child)]


ABSENT_CHANNELS = (
    "Gmail has no channels: mail is exchanged between addresses and grouped in threads, and a mailbox's labels "
    "hold no members (https://developers.google.com/workspace/gmail/api/guides/threads); the provider refuses a "
    "seeded channel, naming itself"
)
ABSENT_EDIT = (
    "a sent email cannot be changed: Gmail's messages resource offers send, insert, import, modify (labels only), "
    "trash and delete, and no edit (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)"
)
ABSENT_REACT = (
    "the Gmail API has no reactions: a message carries labels, and the emoji reactions of Gmail's web client are "
    "not in the API (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)"
)
ABSENT_BUTTON = (
    "an email carries no button a person presses back through the Gmail API: what is sent is an RFC 2822 message "
    "(https://developers.google.com/workspace/gmail/api/guides/sending), and nothing a reader clicks reaches the API"
)
ABSENT_PEOPLE = (
    "Drive v3 lists no accounts: it knows users only as a file's owners, last modifier and permissions, and as "
    "`about.get`'s own user (https://developers.google.com/workspace/drive/api/reference/rest/v3/about), and Gmail "
    "and Calendar know people only as addresses; listing a domain's accounts is the Admin SDK Directory API's "
    "`users.list`, a separate product for Workspace domains"
)


# ---------------------------------------------------------------------------------------------- faults


def _client(api: Api, world: WorldView, service: str) -> tuple[Any, httplib2.Http]:
    """`googleapiclient` over `httplib2`, as `googleapiclient.http.build_http` makes it, through the proxy and
    trusting the CA bundle the server hands out, signed in by `google-auth` with the agent's refresh token."""
    proxy = urlsplit(api.proxy)
    assert proxy.hostname is not None and proxy.port is not None
    http = httplib2.Http(
        ca_certs=api.bundle,
        proxy_info=httplib2.ProxyInfo(socks.PROXY_TYPE_HTTP, proxy.hostname, proxy.port),
        timeout=30,
    )
    http.redirect_codes = http.redirect_codes - {308}
    credentials = Credentials(
        token=None,
        refresh_token=world.claims.tokens[0],
        token_uri=TOKEN_URI,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
    )
    authorized = google_auth_httplib2.AuthorizedHttp(credentials, http=http)
    version = VERSIONS[service]
    return build(service, version, http=authorized, cache_discovery=False, static_discovery=True), http


VERSIONS = {"drive": "v3", "gmail": "v1", "calendar": "v3"}

CALLS: dict[str, tuple[str, Callable[[Any], object]]] = {
    "files.list": ("drive", lambda drive: drive.files().list(pageSize=10, fields="files(id)").execute()),
    "files.get": ("drive", lambda drive: drive.files().get(fileId="root", fields="id").execute()),
    "about.get": ("drive", lambda drive: drive.about().get(fields="user(emailAddress)").execute()),
    "changes.list": (
        "drive",
        lambda drive: (
            drive.changes().list(pageToken=drive.changes().getStartPageToken().execute()["startPageToken"]).execute()
        ),
    ),
    "users.messages.list": ("gmail", lambda gmail: gmail.users().messages().list(userId="me").execute()),
    "users.getProfile": ("gmail", lambda gmail: gmail.users().getProfile(userId="me").execute()),
    "events.list": ("calendar", lambda calendar: calendar.events().list(calendarId="primary").execute()),
}
"""The one call each fault is declared on, by Google's method name: the service it is in and the call through the
client library."""


def _raised(operation: str) -> Callable[[Api, WorldView], BaseException | None]:
    def trigger(api: Api, world: WorldView) -> BaseException | None:
        service, call = CALLS[operation]
        client, http = _client(api, world, service)
        try:
            call(client)
        except HttpError as refused:
            return refused
        finally:
            http.close()
        return None

    return trigger


def _succeeds(operation: str) -> Callable[[Api, WorldView], None]:
    def then(api: Api, world: WorldView) -> None:
        service, call = CALLS[operation]
        client, http = _client(api, world, service)
        try:
            call(client)
        finally:
            http.close()

    return then


def _fault(name: str, operation: str, kind: str, status: int, holds: str, *, times: int = 1) -> FaultCase:
    return FaultCase(
        name=name,
        fragment={"faults": [{"operation": operation, "kind": kind, "times": times}]},
        trigger=_raised(operation),
        typed=HttpError,
        status=status,
        holds=holds,
        then=_succeeds(operation),
    )


# ---------------------------------------------------------------------------------------------- the driver


class WorkspaceDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = WorkspaceSession
    absent: ClassVar[Mapping[str, str]] = {
        "accounts.people": ABSENT_PEOPLE,
        "listing.people": "Drive v3 has no account listing to page (see accounts.people); the Admin SDK Directory "
        "API's users.list is a separate product",
        "accounts.title": "Drive's User resource carries displayName, emailAddress, permissionId, photoLink and me, "
        "and no job title (https://developers.google.com/workspace/drive/api/reference/rest/v3/User)",
        "accounts.bot": "Drive's User resource does not say whether an account is a bot; a service account is a user "
        "like any other to Drive (https://developers.google.com/workspace/drive/api/reference/rest/v3/User)",
        "accounts.guest": "Drive's User resource has no guest flag; anyone with a Google account is granted access "
        "the same way (https://developers.google.com/workspace/drive/api/reference/rest/v3/User)",
        "accounts.deactivated": "Drive lists no accounts, so a deactivated one cannot be listed; suspending an "
        "account is the Admin SDK Directory API's business",
        "accounts.no_email": "every Google account signs in with an email address, which Drive shows as the user's "
        "emailAddress (https://developers.google.com/workspace/drive/api/reference/rest/v3/User)",
        "messaging.channels": ABSENT_CHANNELS,
        "messaging.edit": ABSENT_EDIT,
        "messaging.react": ABSENT_REACT,
        "messaging.button": ABSENT_BUTTON,
        "accounts.vendor_login": "a Google account signs in with its email address; there is no separate login name "
        "(https://support.google.com/accounts/answer/27441)",
    }
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {
        # A user's `permissionId` is "the user's ID as visible in Permission resources"
        # (https://developers.google.com/workspace/drive/api/reference/rest/v3/User); Google publishes no format,
        # and every one Drive answers is a string of decimal digits (observed).
        IdKind.PERSON: re.compile(r"^\d+$"),
        # A document's id is what follows /document/d/ in its URL, documented as matching
        # `/document/d/([a-zA-Z0-9-_]+)` (https://developers.google.com/workspace/docs/api/concepts/document#document_id);
        # a Drive file id is the same string.
        IdKind.DOCUMENT: re.compile(r"^[a-zA-Z0-9_-]+$"),
        # A Gmail message's `id` is "the immutable ID of the message"
        # (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages); Google publishes no
        # format, and every one Gmail answers is sixteen lowercase hex digits (observed).
        IdKind.MESSAGE: re.compile(r"^[0-9a-f]{16}$"),
    }
    page_floor: ClassVar[Mapping[str, int]] = {
        # files.list `pageSize`: "Acceptable values are 1 to 1000, inclusive"
        # (https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list).
        "documents": 1,
        # users.messages.list `maxResults`: "The default value is 100. The maximum allowed value for this field is
        # 500" (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list).
        "history": 1,
    }
    unknown_refusal: ClassVar[tuple[int, str]] = (401, "Invalid Credentials")
    """Drive's answer to an access token it does not accept: 401, reason `authError`, message "Invalid Credentials"
    (https://developers.google.com/workspace/drive/api/guides/handle-errors#resolve_a_401_error_invalid_credentials)."""

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError("Google accounts have no logins (accounts.vendor_login is declared absent)")
        people = [str(p["key"]) for p in _objects(seed, "people")]
        owner = str(seed["owner"]) if "owner" in seed else people[0]
        given = _objects(seed, "sign_ins")
        theirs = [str(s["credential"]) for s in given if s["provider"] == PROVIDER]
        ours = [
            {"provider": PROVIDER, "credential": agent_token(tag), "person": owner},
            *({"provider": PROVIDER, "credential": person_token(tag, k), "person": k} for k in people),
        ]
        merged = dict(seed) | {"sign_ins": [*(dict(s) for s in given), *ours]}
        tokens = [agent_token(tag), *(person_token(tag, k) for k in people), *theirs]
        return CreateWorld(seed=Seed.model_validate(merged), claims=Claims(tokens=tokens))

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        credential = self.credential_of(world, person)
        assert credential is not None
        return WorkspaceSession(api, refresh_grant(credential))

    def signed_in(self, api: Api, world: WorldView, credential: str) -> Session:
        """A refresh token signs in with the refresh-token grant; a service account (named by its email, as the
        seed's sign-in names one) with the JWT-bearer grant."""
        if "@" in credential:
            return WorkspaceSession(api, service_account_grant(credential))
        return WorkspaceSession(api, refresh_grant(credential))

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        tag = tag_of(world)
        return agent_token(tag) if person is None else person_token(tag, person)

    def faults(self) -> list[FaultCase]:
        """Each `wire.FaultKind`, on a Drive call it is documented for
        (https://developers.google.com/workspace/drive/api/guides/handle-errors); and on Gmail and Calendar, a rate
        limit as 429 `rateLimitExceeded` (https://developers.google.com/workspace/gmail/api/guides/handle-errors,
        https://developers.google.com/workspace/calendar/api/guides/errors) and a backend error as 503
        `backendError`."""
        return [
            _fault("rate_limited", "files.list", "rate_limited", 403, "rateLimitExceeded"),
            _fault("user_rate_limited", "files.list", "user_rate_limited", 403, "userRateLimitExceeded"),
            _fault("forbidden", "files.get", "forbidden", 403, "insufficientFilePermissions"),
            _fault("not_found", "files.get", "not_found", 404, "File not found"),
            # google-auth answers a 401 by refreshing the token and sending the call again, twice
            # (`google_auth_httplib2.AuthorizedHttp`, `_DEFAULT_MAX_REFRESH_ATTEMPTS`), so one call through the library
            # meets the fault three times before it raises.
            _fault("unauthenticated", "about.get", "unauthenticated", 401, "Invalid Credentials", times=3),
            _fault("expired", "changes.list", "expired", 410, "expired"),
            _fault("unavailable", "files.list", "unavailable", 503, "backendError"),
            _fault("gmail_rate_limited", "users.messages.list", "rate_limited", 429, "rateLimitExceeded"),
            _fault("gmail_unavailable", "users.getProfile", "unavailable", 503, "backendError"),
            _fault("calendar_rate_limited", "events.list", "rate_limited", 429, "rateLimitExceeded"),
        ]


def _objects(seed: Mapping[str, object], key: str) -> list[Mapping[str, Any]]:
    found = seed[key] if key in seed else []
    if not isinstance(found, list):
        raise TypeError(f"the seed's {key} is not a list")
    return [x for x in found if isinstance(x, Mapping)]


DRIVER = WorkspaceDriver()
