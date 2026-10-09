"""Each behaviour the fidelity audit fixed, held to the page that documents it, through the real proxy with plain
`httpx` and the provider's own sign-in: replies go where Graph's pages say, what is sent is kept as sent, what
would be dropped is refused by name, Exchange's own codes, Outlook's entity tags, `if-match` on drive items, and a
bot's mentions kept."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.adapters.providers.microsoft.wire import Mention
from minutehand.domain.world import MessageSnapshot
from tests.providers.microsoft.outlook import AGENT, OUTLOOK, sent_by_agent, signed_in
from tests.providers.microsoft.tenant import CONNECTOR, GRAPH, Intercepted, Tenant, bearer, seeded, token

REPLY = "https://learn.microsoft.com/en-us/graph/api/message-reply"
REPLY_ALL = "https://learn.microsoft.com/en-us/graph/api/message-replyall"
RESPONSE_CODES = "https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/responsecode"
DRIVEITEM_UPDATE = "https://learn.microsoft.com/en-us/graph/api/driveitem-update"
DRIVEITEM_DELETE = "https://learn.microsoft.com/en-us/graph/api/driveitem-delete"


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", OUTLOOK)


@dataclass
class Outlook:
    http: httpx.AsyncClient
    me: dict[str, str]
    app: dict[str, str]

    async def sent(self, owner: str, subject: str) -> dict[str, Any]:
        """The one message in `owner`'s Sent Items with `subject`."""
        listed = await self.http.get(
            f"{GRAPH}/users/{owner}/mailFolders/sentitems/messages",
            params={"$top": "100", "$orderby": "receivedDateTime desc"},
            headers=self.app,
        )
        [found] = [m for m in listed.json()["value"] if m["subject"] == subject]
        return found

    async def inbox(self, owner: str, subject: str) -> dict[str, Any]:
        listed = await self.http.get(
            f"{GRAPH}/users/{owner}/mailFolders/inbox/messages",
            params={"$top": "100", "$orderby": "receivedDateTime desc"},
            headers=self.app,
        )
        [found] = [m for m in listed.json()["value"] if m["subject"] == subject]
        return found


@pytest.fixture
async def outlook(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Outlook]:
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        app = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        yield Outlook(http=http, me=me, app=app)


def _addresses(recipients: list[dict[str, Any]]) -> list[str]:
    return [r["emailAddress"]["address"] for r in recipients]


async def test_a_reply_goes_to_the_messages_reply_to_and_else_to_its_sender(outlook: Outlook) -> None:
    """Documented (REPLY): a reply goes to the recipients in `replyTo` when the message names any, not to `from`;
    else to the sender. So a reply to one's own sent message goes to oneself, not to its recipients."""
    sent = await outlook.http.post(
        f"{GRAPH}/me/sendMail",
        json={
            "message": {
                "subject": "Venue",
                "body": {"contentType": "text", "content": "Which room?"},
                "toRecipients": [{"emailAddress": {"address": "sofia@example.com"}}],
                "replyTo": [{"emailAddress": {"address": "dania@example.com", "name": "Dania"}}],
            }
        },
        headers=outlook.me,
    )
    assert sent.status_code == 202, sent.text
    hers = await outlook.inbox("sofia@example.com", "Venue")
    assert hers["replyTo"] == [{"emailAddress": {"address": "dania@example.com", "name": "Dania"}}]
    answered = await outlook.http.post(
        f"{GRAPH}/users/sofia@example.com/messages/{hers['id']}/reply", json={"comment": "Room 4"}, headers=outlook.app
    )
    assert answered.status_code == 202, answered.text
    assert _addresses((await outlook.sent("sofia@example.com", "RE: Venue"))["toRecipients"]) == ["dania@example.com"]

    mine = await outlook.sent(AGENT, "Venue")
    again = await outlook.http.post(
        f"{GRAPH}/me/messages/{mine['id']}/reply", json={"comment": "Any news?"}, headers=outlook.me
    )
    assert again.status_code == 202, again.text
    assert _addresses((await outlook.sent(AGENT, "RE: Venue"))["toRecipients"]) == ["dania@example.com"]

    plain = await outlook.http.post(
        f"{GRAPH}/me/sendMail",
        json={"message": {"subject": "Agenda", "toRecipients": [{"emailAddress": {"address": "sofia@example.com"}}]}},
        headers=outlook.me,
    )
    assert plain.status_code == 202, plain.text
    agenda = await outlook.sent(AGENT, "Agenda")
    own = await outlook.http.post(
        f"{GRAPH}/me/messages/{agenda['id']}/reply", json={"comment": "?"}, headers=outlook.me
    )
    assert own.status_code == 202, own.text
    reply = await outlook.sent(AGENT, "RE: Agenda")
    assert _addresses(reply["toRecipients"]) == [AGENT], "from Sent Items the sender is the mailbox itself"


async def test_a_reply_to_all_goes_to_the_sender_and_every_recipient(outlook: Outlook) -> None:
    """Documented (REPLY_ALL): reply-all loads the sender and all recipients of the original message."""
    lunch = await outlook.inbox(AGENT, "Lunch")
    answered = await outlook.http.post(
        f"{GRAPH}/me/messages/{lunch['id']}/replyAll", json={"comment": "Count me in."}, headers=outlook.me
    )
    assert answered.status_code == 202, answered.text
    reply = await outlook.sent(AGENT, "RE: Lunch")
    assert _addresses(reply["toRecipients"]) == ["dania@example.com", AGENT]
    assert _addresses(reply["ccRecipients"]) == ["owen@example.com"]


async def test_a_comment_and_a_body_together_are_refused_400(outlook: Outlook) -> None:
    """Documented (REPLY): specifying both a comment and the message's body returns 400 Bad Request; nothing is
    sent. The page gives no error code, so none is answered."""
    lunch = await outlook.inbox(AGENT, "Lunch")
    refused = await outlook.http.post(
        f"{GRAPH}/me/messages/{lunch['id']}/reply",
        json={"comment": "Yes", "message": {"body": {"contentType": "html", "content": "<p>Yes</p>"}}},
        headers=outlook.me,
    )
    assert refused.status_code == 400 and "code" not in refused.json()["error"]
    listed = (
        await outlook.http.get(
            f"{GRAPH}/me/mailFolders/sentitems/messages", params={"$orderby": "sentDateTime"}, headers=outlook.me
        )
    ).json()
    assert "RE: Lunch" not in [m["subject"] for m in listed["value"]]


async def test_a_replys_body_graph_composes_is_left_out_and_the_run_records_what_was_written(
    outlook: Outlook, tenant: Tenant
) -> None:
    """Graph composes a reply's body from what is written and the quoted original, and no page or recording says
    how, so the reply's `body` and `bodyPreview` are left out rather than invented; the run records what the agent
    wrote."""
    lunch = await outlook.inbox(AGENT, "Lunch")
    html = '<p>Yes, <b>Tuesday</b> at <a href="https://example.com/room">Room 4</a>.</p>'
    answered = await outlook.http.post(
        f"{GRAPH}/me/messages/{lunch['id']}/reply",
        json={"message": {"body": {"contentType": "HTML", "content": html}}},
        headers=outlook.me,
    )
    assert answered.status_code == 202, answered.text
    reply = await outlook.sent(AGENT, "RE: Lunch")
    assert "body" not in reply and "bodyPreview" not in reply
    asked = sent_by_agent(tenant)[-1].after
    assert isinstance(asked, MessageSnapshot) and asked.text == "RE: Lunch\n\nYes, Tuesday at Room 4."


async def test_message_properties_that_would_be_dropped_are_refused_by_name_and_nothing_is_sent(
    outlook: Outlook,
) -> None:
    attachment = {"@odata.type": "#microsoft.graph.fileAttachment", "name": "a.txt", "contentBytes": "SGk="}
    refused = await outlook.http.post(
        f"{GRAPH}/me/sendMail",
        json={
            "message": {
                "subject": "Brief",
                "toRecipients": [{"emailAddress": {"address": "sofia@example.com"}}],
                "attachments": [attachment],
                "categories": ["Red"],
            }
        },
        headers=outlook.me,
    )
    assert refused.status_code == 501
    assert "attachments, categories" in refused.json()["error"]["message"]
    listed = (
        await outlook.http.get(
            f"{GRAPH}/me/mailFolders/sentitems/messages", params={"$orderby": "sentDateTime"}, headers=outlook.me
        )
    ).json()
    assert "Brief" not in [m["subject"] for m in listed["value"]]


EVENT = {
    "subject": "Planning",
    "body": {"contentType": "text", "content": "Agenda:\n1. Budget"},
    "start": {"dateTime": "2026-09-16T10:00:00", "timeZone": "UTC"},
    "end": {"dateTime": "2026-09-16T11:00:00", "timeZone": "UTC"},
    "attendees": [{"emailAddress": {"address": "Sofia@Example.com", "name": "S. Romano"}, "type": "optional"}],
}


async def test_an_events_body_and_attendees_are_kept_as_sent(outlook: Outlook, tenant: Tenant) -> None:
    """The body is kept as sent and read as Graph answers it (HTML unless `outlook.body-content-type="text"`); each
    attendee's address and name are as sent, not resolved against the directory."""
    made = await outlook.http.post(f"{GRAPH}/me/events", json=EVENT, headers=outlook.me)
    assert made.status_code == 201, made.text
    assert made.json()["body"]["contentType"] == "html"
    stored = tenant.world.event(made.json()["id"])
    assert stored is not None and stored.event.body.model_dump() == {
        "contentType": "text",
        "content": "Agenda:\n1. Budget",
    }
    as_text = await outlook.http.get(
        f"{GRAPH}/me/events/{made.json()['id']}",
        headers={**outlook.me, "Prefer": 'outlook.body-content-type="text"'},
    )
    assert as_text.json()["body"] == {"contentType": "text", "content": "Agenda:\n1. Budget"}
    assert as_text.headers["preference-applied"] == 'outlook.body-content-type="text"'
    [attendee] = as_text.json()["attendees"]
    assert attendee["emailAddress"] == {"address": "Sofia@Example.com", "name": "S. Romano"}
    assert attendee["type"] == "optional"


async def test_event_properties_that_would_be_dropped_are_refused_by_name(outlook: Outlook) -> None:
    categories = await outlook.http.post(
        f"{GRAPH}/me/events", json={**EVENT, "categories": ["Blue"]}, headers=outlook.me
    )
    assert categories.status_code == 501 and "categories" in categories.json()["error"]["message"]
    online = await outlook.http.post(f"{GRAPH}/me/events", json={**EVENT, "isOnlineMeeting": True}, headers=outlook.me)
    assert online.status_code == 501 and "isOnlineMeeting" in online.json()["error"]["message"]
    listed = (
        await outlook.http.get(f"{GRAPH}/me/events", params={"$orderby": "start/dateTime"}, headers=outlook.me)
    ).json()["value"]
    assert "Planning" not in [e["subject"] for e in listed]


async def test_an_end_before_the_start_and_an_organizer_answering_are_refused_by_name(outlook: Outlook) -> None:
    """Exchange's EWS codes for these exist, but no Graph page or recording shows Graph's answer, so each is refused
    by name rather than answered with a guessed status and code."""
    backwards = {**EVENT, "end": {"dateTime": "2026-09-16T09:00:00", "timeZone": "UTC"}}
    refused = await outlook.http.post(f"{GRAPH}/me/events", json=backwards, headers=outlook.me)
    assert refused.status_code == 501 and "end is before its start" in refused.json()["error"]["message"]
    made = (await outlook.http.post(f"{GRAPH}/me/events", json=EVENT, headers=outlook.me)).json()
    for action in ("accept", "tentativelyAccept", "decline"):
        answered = await outlook.http.post(f"{GRAPH}/me/events/{made['id']}/{action}", json={}, headers=outlook.me)
        assert answered.status_code == 501 and "organizer" in answered.json()["error"]["message"]


async def test_outlook_items_carry_their_change_key_as_a_weak_etag_and_both_change_with_them(outlook: Outlook) -> None:
    """Graph assigns `@odata.etag` and `changeKey` to a message and an event; every example answer on the message
    pages shows the etag as W/"<changeKey>"."""
    lunch = await outlook.inbox(AGENT, "Lunch")
    assert lunch["@odata.etag"] == f'W/"{lunch["changeKey"]}"'
    unread = await outlook.http.patch(f"{GRAPH}/me/messages/{lunch['id']}", json={"isRead": False}, headers=outlook.me)
    assert unread.json()["changeKey"] != lunch["changeKey"]
    assert unread.json()["@odata.etag"] == f'W/"{unread.json()["changeKey"]}"'
    event = (await outlook.http.post(f"{GRAPH}/me/events", json=EVENT, headers=outlook.me)).json()
    assert event["@odata.etag"] == f'W/"{event["changeKey"]}"'


async def test_a_folder_the_mailbox_does_not_hold_is_refused_by_name(
    outlook: Outlook,
) -> None:
    junk = await outlook.http.get(f"{GRAPH}/me/mailFolders/junkemail/messages", headers=outlook.me)
    assert junk.status_code == 501 and "junkemail" in junk.json()["error"]["message"]
    unknown = await outlook.http.get(f"{GRAPH}/me/mailFolders/AAMkANOSUCHFOLDER/messages", headers=outlook.me)
    assert unknown.status_code == 501 and "does not hold" in unknown.json()["error"]["message"]


async def test_a_stale_if_match_is_refused_412_and_the_item_is_unchanged(outlook: Outlook) -> None:
    """Documented (DRIVEITEM_UPDATE, DRIVEITEM_DELETE): an if-match that is not the item's eTag or cTag is 412 and
    nothing changes; the current eTag goes through."""
    drive = (await outlook.http.get(f"{GRAPH}/users/owen@example.com/drive", headers=outlook.app)).json()["id"]
    made = (
        await outlook.http.put(f"{GRAPH}/drives/{drive}/root:/plan.txt:/content", content=b"v1", headers=outlook.app)
    ).json()
    renamed = await outlook.http.patch(
        f"{GRAPH}/drives/{drive}/items/{made['id']}", json={"name": "plan-2.txt"}, headers=outlook.app
    )
    stale = {**outlook.app, "If-Match": made["eTag"]}
    refused = await outlook.http.patch(
        f"{GRAPH}/drives/{drive}/items/{made['id']}", json={"name": "plan-3.txt"}, headers=stale
    )
    assert (refused.status_code, refused.json()["error"]["code"]) == (412, "resourceModified")
    gone = await outlook.http.delete(f"{GRAPH}/drives/{drive}/items/{made['id']}", headers=stale)
    assert gone.status_code == 412
    held = (await outlook.http.get(f"{GRAPH}/drives/{drive}/items/{made['id']}", headers=outlook.app)).json()
    assert held["name"] == "plan-2.txt"
    current = {**outlook.app, "If-Match": renamed.json()["eTag"]}
    assert (await outlook.http.delete(f"{GRAPH}/drives/{drive}/items/{made['id']}", headers=current)).status_code == 204


async def test_drive_item_properties_that_would_be_dropped_are_refused_by_name(outlook: Outlook) -> None:
    drive = (await outlook.http.get(f"{GRAPH}/users/owen@example.com/drive", headers=outlook.app)).json()["id"]
    made = (
        await outlook.http.put(f"{GRAPH}/drives/{drive}/root:/notes.txt:/content", content=b"x", headers=outlook.app)
    ).json()
    refused = await outlook.http.patch(
        f"{GRAPH}/drives/{drive}/items/{made['id']}", json={"description": "Draft"}, headers=outlook.app
    )
    assert refused.status_code == 501 and "description" in refused.json()["error"]["message"]


async def test_a_bots_mentions_are_kept_and_graph_reads_them(tenant: Tenant, microsoft: Intercepted) -> None:
    """Data stays as sent: the entities a bot sends with an activity are stored with it, and Graph's chatMessage
    lists its mentions."""
    sofia = tenant.world.person("sofia")
    assert sofia is not None
    async with microsoft.http() as http:
        bot = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
        app = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        general = tenant.directory.general_channel_id
        mention = {"type": "mention", "text": "<at>Sofia</at>", "mentioned": {"id": sofia.mri, "name": "Sofia"}}
        other = {"type": "clientInfo", "locale": "en-GB"}
        sent = await http.post(
            f"{CONNECTOR}v3/conversations/{general}/activities",
            json={"type": "message", "text": "<at>Sofia</at> please review", "entities": [mention, other]},
            headers=bot,
        )
        assert sent.status_code == 201, sent.text
        stored = tenant.world.message(sent.json()["id"])
        assert stored is not None
        kept = stored[1].entities or []
        assert [e.model_dump(mode="json", exclude_none=True) if isinstance(e, Mention) else e for e in kept] == [
            mention,
            other,
        ]
        read = await http.get(
            f"{GRAPH}/teams/{tenant.directory.team_id}/channels/{general}/messages/{sent.json()['id']}", headers=app
        )
        assert [m["mentioned"]["user"]["id"] for m in read.json()["mentions"]] == [sofia.user.id]


async def test_a_preference_that_would_change_the_answer_unserved_is_refused_by_name(outlook: Outlook) -> None:
    """`Prefer: outlook.timezone` other than UTC and `IdType="ImmutableId"` change what Graph answers; answering
    as though they were not asked would hand back something else, so each is refused by name. UTC is served."""
    url = f"{GRAPH}/me/events"
    zoned = await outlook.http.get(url, headers={**outlook.me, "Prefer": 'outlook.timezone="Pacific Standard Time"'})
    assert zoned.status_code == 501 and "outlook.timezone" in zoned.json()["error"]["message"]
    immutable = await outlook.http.get(f"{GRAPH}/me/messages", headers={**outlook.me, "Prefer": 'IdType="ImmutableId"'})
    assert immutable.status_code == 501 and "ImmutableId" in immutable.json()["error"]["message"]
    utc = await outlook.http.get(url, headers={**outlook.me, "Prefer": 'outlook.timezone="UTC"'})
    assert utc.status_code == 200


async def test_an_event_list_and_the_calendar_view_without_orderby_answer_by_start_ascending(outlook: Outlook) -> None:
    """Without `$orderby`, events and the calendar view are listed by start, earliest first, whatever order they
    were made in."""
    for when in ("2026-09-17", "2026-09-16"):
        made = {**EVENT, "subject": when, "start": {"dateTime": f"{when}T10:00:00", "timeZone": "UTC"},
                "end": {"dateTime": f"{when}T11:00:00", "timeZone": "UTC"}}  # fmt: skip
        assert (await outlook.http.post(f"{GRAPH}/me/events", json=made, headers=outlook.me)).status_code == 201
    listed = await outlook.http.get(f"{GRAPH}/me/events", headers=outlook.me)
    assert listed.status_code == 200
    assert [e["subject"] for e in listed.json()["value"]] == ["2026-09-16", "2026-09-17"]
    view = await outlook.http.get(
        f"{GRAPH}/me/calendarView",
        params={"startDateTime": "2026-09-14T00:00:00Z", "endDateTime": "2026-09-21T00:00:00Z"},
        headers=outlook.me,
    )
    assert view.status_code == 200
    assert [e["subject"] for e in view.json()["value"]] == ["2026-09-16", "2026-09-17"]


async def test_internet_message_id_is_left_out_and_filtering_on_it_is_refused_by_name(outlook: Outlook) -> None:
    """Exchange assigns `internetMessageId` from its own hosts; any value made up here would be invented, so the
    property is left out, and a `$filter` on it is refused by name."""
    lunch = await outlook.inbox(AGENT, "Lunch")
    assert "internetMessageId" not in lunch
    refused = await outlook.http.get(
        f"{GRAPH}/me/messages", params={"$filter": "internetMessageId eq '<x@y>'"}, headers=outlook.me
    )
    assert refused.status_code == 501


async def test_mail_folders_list_by_display_name_as_graphs_example_answer_does(outlook: Outlook) -> None:
    """user-list-mailfolders' example answer names the well-known folders "Deleted Items", "Drafts", "Inbox",
    "Sent Items", in that order."""
    listed = (await outlook.http.get(f"{GRAPH}/me/mailFolders", headers=outlook.me)).json()["value"]
    assert [f["displayName"] for f in listed] == ["Deleted Items", "Drafts", "Inbox", "Sent Items"]


async def test_only_an_acceptance_carries_a_subject_a_recording_shows(outlook: Outlook) -> None:
    """Microsoft's recorded sample of sent items shows "Accepted: <subject>"; no source shows what a tentative or
    declining answer is subjected, so theirs is left out."""
    planning = next(
        e
        for e in (
            await outlook.http.get(
                f"{GRAPH}/users/sofia@example.com/events", params={"$orderby": "start/dateTime"}, headers=outlook.app
            )
        ).json()["value"]
        if e["subject"] == "Planning"
    )
    for action in ("tentativelyAccept", "accept"):
        answered = await outlook.http.post(
            f"{GRAPH}/users/sofia@example.com/events/{planning['id']}/{action}", json={}, headers=outlook.app
        )
        assert answered.status_code == 202, answered.text
    sent = (
        await outlook.http.get(
            f"{GRAPH}/users/sofia@example.com/mailFolders/sentitems/messages",
            params={"$orderby": "sentDateTime"},
            headers=outlook.app,
        )
    ).json()["value"]
    responses = [m for m in sent if m.get("meetingMessageType") in ("meetingTentativelyAccepted", "meetingAccepted")]
    assert [(m["meetingMessageType"], m.get("subject")) for m in responses] == [
        ("meetingTentativelyAccepted", None),
        ("meetingAccepted", "Accepted: Planning"),
    ]


async def test_a_body_that_cannot_be_read_is_refused_by_name_where_graph_records_no_answer(outlook: Outlook) -> None:
    """Graph's error table gives 400 for a malformed request but no Outlook page or recording gives its code, so an
    unreadable body to mail, calendars or subscriptions is refused by name; OneDrive's error page documents
    `invalidRequest` for files, which keep it."""
    for url in (f"{GRAPH}/me/sendMail", f"{GRAPH}/me/events", f"{GRAPH}/subscriptions"):
        refused = await outlook.http.post(
            url, content=b"{not json", headers={**outlook.me, "Content-Type": "application/json"}
        )
        assert refused.status_code == 501 and "cannot be read" in refused.json()["error"]["message"], url
