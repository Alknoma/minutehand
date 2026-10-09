"""Files attached to Outlook items (a message, an event): what a caller may attach and how the attachments are read.

A file attachment has a `name` and base64 `contentBytes` (resources/fileattachment) and is under 3 MB when posted to the
item (message-post-attachments, event-post-attachments); an item attachment, a link, a larger file (an upload session)
and a `contentBytes` that is not base64 are refused by name. Neither list page documents an order, so a list of more
than one asks for `$orderby` (`name`, `size`, `lastModifiedDateTime`, `contentType`).
"""

from __future__ import annotations

import base64
import binascii

from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.providers.microsoft import wire
from minutehand.adapters.providers.microsoft.common import GRAPH_JSON, GraphRefusal, query
from minutehand.domain.errors import NotServed

ORDER = ("name", "size", "lastModifiedDateTime", "contentType")  # enum-lint: exempt Graph's property names
LIMIT = 3 * 1024 * 1024
"""A file attached by `POST …/attachments` is under 3 MB."""
OPTIONS = ("$filter", "$search", "$expand", "$count", "$top", "$skip", "$skiptoken")


async def sent(request: Request) -> wire.SentAttachment:
    """The attachment a POST carries."""
    try:
        return wire.read(wire.SentAttachment, await request.body())
    except wire.Unreadable as e:
        raise NotServed(f"a request body that cannot be read ({e.message}): Graph's answer is not recorded") from e


def kept(attachment: wire.SentAttachment, attachment_id: str, now: str) -> wire.StoredAttachment:
    """A file attachment as sent; refuses by name what is not a `fileAttachment` of under 3 MB with base64 bytes."""
    if attachment.model_extra:
        raise NotServed(f"the attachment properties {', '.join(sorted(attachment.model_extra))}")
    if attachment.odata_type.lower().lstrip("#") != "microsoft.graph.fileattachment":
        raise NotServed(f"an attachment of the type {attachment.odata_type!r}: only fileAttachment is held")
    try:
        content = base64.b64decode(attachment.contentBytes, validate=True)
    except binascii.Error as e:
        raise NotServed("an attachment whose contentBytes are not base64: Graph's answer is not documented") from e
    if len(content) >= LIMIT:
        raise NotServed(
            "an attachment of 3 MB or more: the page sends it through an upload session, which is not served"
        )
    return wire.StoredAttachment(
        id=attachment_id,
        lastModifiedDateTime=now,
        name=attachment.name,
        contentType=attachment.contentType,
        size=len(content),
        isInline=attachment.isInline,
        contentId=attachment.contentId,
        contentBytes=attachment.contentBytes,
    )


def read(request: Request, held: list[wire.StoredAttachment], rest: list[str], where: str) -> Response:
    """`GET …/attachments` (`rest` empty) or `GET …/attachments/{id}`; `where` is the `$metadata#` context."""
    for option in OPTIONS:
        if option in request.query_params:
            raise NotServed(f"{option} on attachments")
    fields = [f for f in (query(request, "$select") or "").split(",") if f] or None
    if rest:
        found = next((a for a in held if a.id == rest[0]), None)
        if found is None:
            raise GraphRefusal(404, "ErrorItemNotFound", "The specified object was not found in the store.")
        return Response(
            wire.select(wire.with_context(wire.dump(found), f"{where}/$entity"), fields), media_type=GRAPH_JSON
        )
    listed = list(held)
    ordered = query(request, "$orderby")
    if ordered:
        prop, _, direction = ordered.strip().partition(" ")
        if prop not in ORDER or direction.strip().lower() not in ("", "asc", "desc"):
            raise NotServed(f"$orderby on attachments: {ordered}")
        listed.sort(key=lambda a: getattr(a, prop) or "", reverse=direction.strip().lower() == "desc")
    elif len(listed) > 1:
        raise NotServed(
            "listing attachments without $orderby: Graph documents no order for them (message-list-attachments)"
        )
    body = wire.dump(wire.Page[wire.StoredAttachment](context=where, value=listed))
    return Response(wire.select_page(body, fields), media_type=GRAPH_JSON)
