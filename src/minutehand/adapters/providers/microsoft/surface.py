"""Graph `v1.0` as this provider serves it, gated in front of every surface.

`SERVED` lists each operation of Microsoft's published OpenAPI description of Graph `v1.0`
(`microsoftgraph/msgraph-metadata`, `openapi/v1.0`; the subset for the resources this provider claims is kept with
its source and date in `tests/data/microsoft_graph_v1/`) that this provider answers, spelled as that description
spells it: method and path template. `DOCUMENTED` lists the few it answers that the description leaves out but
Microsoft's reference page documents, each with that page. A call that matches neither is not served and is refused
by name (501 `not_implemented`, naming its method and path) before any surface reads it.

A call is matched in the forms Graph itself reads alike (each documented on the page cited):

- `mailFolders('Inbox')` as `mailFolders/Inbox`, any `name('key')` key segment as its two segments
  (https://learn.microsoft.com/en-us/graph/query-parameters, OData key syntax);
- a drive reached through `/me/drive`, `/users/{id}/drive` or `/sites/{id}/drive` as `/drives/{drive-id}`, and a
  site as `{hostname}:/{path}:` (https://learn.microsoft.com/en-us/graph/api/resources/drive,
  https://learn.microsoft.com/en-us/graph/api/site-getbypath);
- an item by path, `root:/a/b:` or `items/{id}:/b:`, and `root` itself, as `items/{driveItem-id}`
  (https://learn.microsoft.com/en-us/graph/onedrive-addressing-driveitems);
- a function called with no arguments with or without its parentheses (`delta`, `delta()`).
"""

from __future__ import annotations

import re
from functools import cache

_USER = [
    ("GET", ""),
    ("GET", "/presence"),
    ("GET", "/mailboxSettings"),
    ("GET", "/chats"),
    ("GET", "/joinedTeams"),
    ("GET", "/drive"),
    ("POST", "/sendMail"),
    ("GET", "/mailFolders"),
    ("GET", "/mailFolders/{mailFolder-id}"),
    ("GET", "/messages"),
    ("GET", "/mailFolders/{mailFolder-id}/messages"),
    ("GET", "/mailFolders/{mailFolder-id}/messages/delta()"),
    *(
        (method, f"{folder}/messages/{{message-id}}")
        for folder in ("", "/mailFolders/{mailFolder-id}")
        for method in ("GET", "PATCH", "DELETE")
    ),
    *(
        ("POST", f"{folder}/messages/{{message-id}}/{action}")
        for folder in ("", "/mailFolders/{mailFolder-id}")
        for action in (
            "reply",
            "replyAll",
            "send",
            "createReply",
            "createReplyAll",
            "createForward",
            "forward",
            "move",
            "copy",
        )
    ),
    ("POST", "/messages"),
    *(
        (method, f"{folder}/messages/{{message-id}}/attachments")
        for folder in ("", "/mailFolders/{mailFolder-id}")
        for method in ("GET", "POST")
    ),
    *(
        ("GET", f"{folder}/messages/{{message-id}}/attachments/{{attachment-id}}")
        for folder in ("", "/mailFolders/{mailFolder-id}")
    ),
    ("GET", "/calendar"),
    ("GET", "/calendars"),
    ("POST", "/calendar/getSchedule"),
    ("GET", "/calendarView"),
    ("GET", "/calendarView/delta()"),
    ("GET", "/calendar/calendarView"),
    *(
        operation
        for where in ("", "/calendar")
        for operation in (
            ("GET", f"{where}/events"),
            ("POST", f"{where}/events"),
            ("GET", f"{where}/events/{{event-id}}"),
            ("GET", f"{where}/events/{{event-id}}/attachments"),
            ("POST", f"{where}/events/{{event-id}}/attachments"),
            ("GET", f"{where}/events/{{event-id}}/attachments/{{attachment-id}}"),
            ("PATCH", f"{where}/events/{{event-id}}"),
            ("DELETE", f"{where}/events/{{event-id}}"),
            ("POST", f"{where}/events/{{event-id}}/accept"),
            ("POST", f"{where}/events/{{event-id}}/tentativelyAccept"),
            ("POST", f"{where}/events/{{event-id}}/decline"),
            ("POST", f"{where}/events/{{event-id}}/cancel"),
            ("GET", f"{where}/events/{{event-id}}/instances"),
        )
    ),
]
"""What is served under a user, the same from `/me` and from `/users/{user-id}`."""

_ITEM = "/drives/{drive-id}/items/{driveItem-id}"
_CHANNEL = "/teams/{team-id}/channels/{channel-id}"

SERVED: frozenset[tuple[str, str]] = frozenset(
    {
        *((method, f"/me{rest}") for method, rest in _USER),
        *((method, f"/users/{{user-id}}{rest}") for method, rest in _USER),
        ("GET", "/users"),
        ("GET", "/communications/presences/{presence-id}"),
        ("POST", "/communications/getPresencesByUserId"),
        ("GET", "/teams/{team-id}"),
        ("GET", "/teams/{team-id}/members"),
        ("GET", "/teams/{team-id}/primaryChannel"),
        ("GET", "/teams/{team-id}/channels"),
        ("GET", _CHANNEL),
        ("GET", f"{_CHANNEL}/members"),
        ("GET", f"{_CHANNEL}/messages"),
        ("GET", f"{_CHANNEL}/messages/delta()"),
        ("POST", f"{_CHANNEL}/messages"),
        ("GET", f"{_CHANNEL}/messages/{{chatMessage-id}}"),
        ("GET", f"{_CHANNEL}/messages/{{chatMessage-id}}/replies"),
        ("POST", f"{_CHANNEL}/messages/{{chatMessage-id}}/replies"),
        ("GET", "/chats"),
        ("POST", "/chats"),
        ("GET", "/chats/{chat-id}"),
        ("GET", "/chats/{chat-id}/members"),
        ("GET", "/chats/{chat-id}/messages"),
        ("GET", "/chats/{chat-id}/messages/delta()"),
        ("POST", "/chats/{chat-id}/messages"),
        ("GET", "/chats/{chat-id}/messages/{chatMessage-id}"),
        ("GET", "/sites"),
        ("GET", "/sites/{site-id}"),
        ("GET", "/sites/{site-id}/drive"),
        ("GET", "/sites/{site-id}/drives"),
        ("GET", "/drives/{drive-id}"),
        ("GET", "/drives/{drive-id}/root"),
        ("GET", _ITEM),
        ("PATCH", _ITEM),
        ("DELETE", _ITEM),
        ("GET", f"{_ITEM}/children"),
        ("POST", f"{_ITEM}/children"),
        ("GET", f"{_ITEM}/content"),
        ("PUT", f"{_ITEM}/content"),
        ("GET", f"{_ITEM}/delta()"),
        ("GET", f"{_ITEM}/search(q='{{q}}')"),
        ("POST", f"{_ITEM}/createUploadSession"),
        ("POST", f"{_ITEM}/copy"),
        ("POST", f"{_ITEM}/invite"),
        ("POST", f"{_ITEM}/createLink"),
        ("GET", f"{_ITEM}/permissions"),
        ("GET", f"{_ITEM}/permissions/{{permission-id}}"),
        ("DELETE", f"{_ITEM}/permissions/{{permission-id}}"),
        ("GET", "/subscriptions"),
        ("POST", "/subscriptions"),
        ("GET", "/subscriptions/{subscription-id}"),
        ("PATCH", "/subscriptions/{subscription-id}"),
        ("DELETE", "/subscriptions/{subscription-id}"),
    }
)

MAILBOX_SETTINGS = "https://learn.microsoft.com/en-us/graph/api/user-get-mailboxsettings"
DOCUMENTED: dict[tuple[str, str], str] = {
    ("GET", "/me/mailboxSettings/automaticRepliesSetting"): MAILBOX_SETTINGS,
    ("GET", "/users/{user-id}/mailboxSettings/automaticRepliesSetting"): MAILBOX_SETTINGS,
}
"""Served operations Microsoft's reference page documents that the OpenAPI description leaves out, with the page."""

_KEYED = re.compile(r"^([A-Za-z]+)\('((?:[^']|'')*)'\)$")
_SITE = re.compile(r"^sites/([^/:]+):(/[^:]*)?(?::(/.*))?$")
_ITEM_BY_PATH = re.compile(r"/(root|items/[^/:]+):/[^:]*:?(?=/|$)")


def normal(path: str) -> str:
    """`path` (after `/v1.0`) in the one form `SERVED` spells, as the module's docstring lists."""
    segments: list[str] = []
    for part in [p for p in path.split("/") if p]:
        keyed = _KEYED.match(part)
        segments.extend([keyed.group(1), keyed.group(2)] if keyed else [part])
    text = "/".join(segments)
    site = _SITE.match(text)
    if site is not None:
        text = "sites/site" + (site.group(3) or "")
        segments = text.split("/")
    drive_at = 1 if segments[:1] == ["me"] else 2 if segments[:1] in (["users"], ["sites"]) else None
    if drive_at is not None and len(segments) > drive_at + 1 and segments[drive_at] == "drive":
        segments = ["drives", "drive", *segments[drive_at + 1 :]]
    text = "/".join(segments)
    if segments[:1] == ["drives"]:
        text = _ITEM_BY_PATH.sub("/items/item", text)
        segments = text.split("/")
        if len(segments) > 3 and segments[2] == "root" and segments[3] != "content":
            text = "/".join([*segments[:2], "items", "root", *segments[3:]])
    return "/" + re.sub(r"(^|/)delta(?=/|$)", r"\1delta()", text)


_KEY = r"[^/$()][^/()]*"
"""A key in a path: never a `$`-segment (`$count`, `$value`) nor a function (`delta()`), which are segments of their
own in the templates."""


@cache
def _pattern(template: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", _KEY, re.escape(template)) + "$")


def served(method: str, path: str) -> bool:
    """Whether a call to `path` (after `/v1.0`) with `method` is one this provider answers."""
    wanted = normal(path)
    return any(
        listed == method and _pattern(template).match(wanted) is not None for listed, template in (*SERVED, *DOCUMENTED)
    )
