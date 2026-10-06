"""Vendor claims about the Bot Framework connector as Teams serves it, carried over from an older emulator's own tests
and checked against Microsoft's documentation. `CLAIMS.md` beside the provider lists each one with its source."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from minutehand.adapters.providers.microsoft.state import ConversationRecord, UserRecord
from minutehand.domain.scenario import PersonPosts
from tests.providers.microsoft.tenant import CONNECTOR, Bot, Intercepted, Tenant, bearer, token

REST = "https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference"
TEAMS_CODES = "https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability"
TEAMS_CONTEXT = "https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context"
PROACTIVE = (
    "https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages"
)

CARD = "application/vnd.microsoft.card.adaptive"


@dataclass
class Caller:
    http: httpx.AsyncClient
    auth: dict[str, str]
    tenant: Tenant

    @property
    def general(self) -> str:
        return self.tenant.directory.general_channel_id

    def person(self, key: str) -> tuple[UserRecord, ConversationRecord]:
        user = self.tenant.world.person(key)
        assert user is not None
        chat = self.tenant.world.personal_with(user.user.id, self.tenant.directory.tenant_id)
        assert chat is not None
        return user, chat

    def activities(self, conversation: str) -> str:
        return f"{CONNECTOR}v3/conversations/{conversation}/activities"

    async def send(self, conversation: str, body: dict[str, Any]) -> httpx.Response:
        return await self.http.post(self.activities(conversation), json=body, headers=self.auth)

    def stored(self, activity: str) -> Any:
        found = self.tenant.world.message(activity)
        assert found is not None, f"activity {activity} is not in the world"
        return found[1]


@pytest.fixture
async def caller(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Caller]:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
        yield Caller(http=http, auth=auth, tenant=tenant)


def refusal(answered: httpx.Response, status: int) -> str:
    assert answered.status_code == status, answered.text
    return str(answered.json()["error"]["code"])


def card(text: str) -> dict[str, Any]:
    body = [{"type": "TextBlock", "text": text}]
    return {"contentType": CARD, "content": {"type": "AdaptiveCard", "version": "1.5", "body": body}}


# ---------------------------------------------------------------------- refusals


async def test_a_send_with_no_token_is_refused_401(tenant: Tenant, microsoft: Intercepted) -> None:
    """Documented: 401 means the bot is not authenticated. Class (a), REST and TEAMS_CODES."""
    async with microsoft.http() as http:
        answered = await http.post(
            f"{CONNECTOR}v3/conversations/{tenant.directory.general_channel_id}/activities",
            json={"type": "message", "text": "anyone there"},
        )
    assert answered.status_code == 401


async def test_a_send_to_a_conversation_that_does_not_exist_is_refused_conversation_not_found(
    caller: Caller,
) -> None:
    """Documented: a conversation id that names nothing is 404 `ConversationNotFound`, and nothing is delivered.
    Class (a), TEAMS_CODES."""
    head = caller.tenant.store.head()
    answered = await caller.send("tenant-workspace-key", {"type": "message", "text": "lost"})
    assert refusal(answered, 404) == "ConversationNotFound"
    assert caller.tenant.store.head() == head


async def test_paged_members_of_a_conversation_that_does_not_exist_is_refused_404(caller: Caller) -> None:
    """Documented: the members of a conversation that does not exist are a 404. Class (a), TEAMS_CODES."""
    answered = await caller.http.get(
        f"{CONNECTOR}v3/conversations/not-a-conversation/pagedmembers", headers=caller.auth
    )
    assert refusal(answered, 404) == "ConversationNotFound"


async def test_an_activity_over_the_documented_size_limit_is_refused_message_size_too_big(caller: Caller) -> None:
    """Documented: a message past the size limit is 413 with error code `MessageSizeTooBig`. Class (a),
    TEAMS_CODES, which puts the limit at 100 KB of the message encoded as UTF-16: 52,000 characters are 104 KB."""
    head = caller.tenant.store.head()
    answered = await caller.send(caller.general, {"type": "message", "text": "z" * 52_000})
    assert refusal(answered, 413) == "MessageSizeTooBig"
    assert caller.tenant.store.head() == head


async def test_a_message_of_forty_thousand_characters_is_inside_the_documented_limit_and_delivered(
    caller: Caller,
) -> None:
    """Documented: the limit is 100 KB as UTF-16, so 40,000 characters (80 KB, the size the page calls safe) are
    delivered whole. Class (a), TEAMS_CODES. The old emulator refused this at 28 KB: contradicted, see `CLAIMS.md`."""
    answered = await caller.send(caller.general, {"type": "message", "text": "w" * 40_000})
    assert answered.is_success, answered.text
    assert caller.stored(answered.json()["id"]).text == "w" * 40_000


async def test_an_activity_well_inside_the_size_limit_is_delivered(caller: Caller) -> None:
    """Documented: a message inside the limit is delivered whole. Class (a), TEAMS_CODES."""
    answered = await caller.send(caller.general, {"type": "message", "text": "q" * 20_000})
    assert answered.is_success, answered.text
    assert caller.stored(answered.json()["id"]).text == "q" * 20_000


async def test_details_of_a_team_that_does_not_exist_are_refused_404(caller: Caller) -> None:
    """Observed: an unknown team id is a 404. Class (b), asserted by the old emulator; the connector reference
    documents 404 only as 'resource not found'."""
    answered = await caller.http.get(f"{CONNECTOR}v3/teams/19:no-such-team@thread.tacv2", headers=caller.auth)
    assert answered.status_code == 404


# ---------------------------------------------------------------------- sending, replying, updating, deleting


async def test_a_send_is_stored_and_answered_with_its_activity_id(caller: Caller) -> None:
    """Documented: Send to Conversation answers a ResourceResponse whose id names the posted activity. Class (a),
    REST."""
    answered = await caller.send(caller.general, {"type": "message", "text": "Standup moved to ten"})
    assert answered.is_success, answered.text
    assert caller.stored(answered.json()["id"]).text == "Standup moved to ten"


async def test_an_adaptive_card_attachment_is_kept_as_sent(caller: Caller) -> None:
    """Documented: an activity's attachments travel with it; an Adaptive Card is one by its content type. Class (a),
    REST."""
    answered = await caller.send(caller.general, {"type": "message", "attachments": [card("Approve the budget?")]})
    kept = caller.stored(answered.json()["id"]).attachments
    assert kept is not None and [a.contentType for a in kept] == [CARD]


async def test_a_reply_to_an_activity_in_a_channel_threads_under_it(caller: Caller) -> None:
    """Documented: Reply to Activity adds the activity as a reply to the one it names, where the channel threads.
    Class (a), REST."""
    root = (await caller.send(caller.general, {"type": "message", "text": "Who takes the audit?"})).json()["id"]
    answered = await caller.http.post(
        f"{caller.activities(caller.general)}/{root}",
        json={"type": "message", "text": "Dania does."},
        headers=caller.auth,
    )
    assert answered.is_success, answered.text
    assert caller.stored(answered.json()["id"]).replyToId == root
    assert caller.stored(root).replyToId is None


async def test_a_reply_to_an_activity_in_a_personal_chat_is_delivered_like_a_send(caller: Caller) -> None:
    """Documented: every channel supports Reply to Activity, and where a channel has no nested replies it behaves like
    Send to Conversation. Class (a), REST. The old emulator refused this 400 BadArgument: contradicted, see
    `CLAIMS.md`."""
    _, chat = caller.person("sofia")
    first = (await caller.send(chat.id, {"type": "message", "text": "Can you sign the order?"})).json()["id"]
    answered = await caller.http.post(
        f"{caller.activities(chat.id)}/{first}", json={"type": "message", "text": "Reminder"}, headers=caller.auth
    )
    assert answered.is_success, answered.text
    assert caller.stored(answered.json()["id"]).text == "Reminder"


async def test_an_update_replaces_the_activitys_text(caller: Caller) -> None:
    """Documented: Update Activity changes the named activity in place and answers its id. Class (a), REST and
    PROACTIVE."""
    sent = (await caller.send(caller.general, {"type": "message", "text": "Counting votes"})).json()["id"]
    answered = await caller.http.put(
        f"{caller.activities(caller.general)}/{sent}",
        json={"type": "message", "id": sent, "text": "Votes counted: carried"},
        headers=caller.auth,
    )
    assert answered.status_code == 200 and answered.json()["id"] == sent
    assert caller.stored(sent).text == "Votes counted: carried"


async def test_an_update_carrying_both_text_and_a_card_is_refused_bad_syntax(caller: Caller) -> None:
    """Observed: an update that would render as two messages (a text bubble and a card) is refused 400 `BadSyntax`
    'Activity resulted into multiple skype activities', and the activity is left as it was. Class (b), seen in
    production by the old emulator's authors; not on Microsoft's pages."""
    sent = (await caller.send(caller.general, {"type": "message", "text": "Working on it"})).json()["id"]
    answered = await caller.http.put(
        f"{caller.activities(caller.general)}/{sent}",
        json={"type": "message", "id": sent, "text": "Here it is", "attachments": [card("Result")]},
        headers=caller.auth,
    )
    assert refusal(answered, 400) == "BadSyntax"
    assert "multiple skype activities" in answered.json()["error"]["message"]
    assert caller.stored(sent).text == "Working on it"


async def test_an_update_carrying_only_a_card_is_accepted(caller: Caller) -> None:
    """Observed: the same update with the card alone is accepted. Class (b), as above."""
    sent = (await caller.send(caller.general, {"type": "message", "text": "Working on it"})).json()["id"]
    answered = await caller.http.put(
        f"{caller.activities(caller.general)}/{sent}",
        json={"type": "message", "id": sent, "attachments": [card("Result")]},
        headers=caller.auth,
    )
    assert answered.status_code == 200, answered.text
    kept = caller.stored(sent).attachments
    assert kept is not None and len(kept) == 1


async def test_a_deleted_activity_is_gone_from_the_conversation(caller: Caller) -> None:
    """Documented: Delete Activity removes the activity and answers a status with no body. Class (a), REST. The exact
    success status is not pinned: the old emulator answered 204, this provider 200; `CLAIMS.md` records it."""
    sent = (await caller.send(caller.general, {"type": "message", "text": "Posted by mistake"})).json()["id"]
    answered = await caller.http.delete(f"{caller.activities(caller.general)}/{sent}", headers=caller.auth)
    assert answered.is_success and answered.content == b""
    assert caller.tenant.world.message(sent) is None


@pytest.mark.parametrize("verb", ["reply", "update", "delete"])
async def test_an_activity_that_is_not_in_the_conversation_is_refused_activity_not_found_in_conversation(
    caller: Caller, verb: str
) -> None:
    """Documented: an activity id the conversation does not hold is 404 `ActivityNotFoundInConversation`, and
    nothing is written. Class (a), TEAMS_CODES (status codes from agent conversational APIs)."""
    head = caller.tenant.store.head()
    url = f"{caller.activities(caller.general)}/1700000000000"
    body = {"type": "message", "text": "answering nothing"}
    if verb == "reply":
        answered = await caller.http.post(url, json=body, headers=caller.auth)
    elif verb == "update":
        answered = await caller.http.put(url, json=body, headers=caller.auth)
    else:
        answered = await caller.http.delete(url, headers=caller.auth)
    assert refusal(answered, 404) == "ActivityNotFoundInConversation"
    assert caller.tenant.store.head() == head


@pytest.mark.parametrize("verb", ["update", "delete"])
async def test_changing_a_persons_message_is_refused_not_enough_permissions(
    tenant: Tenant, bot: Bot, caller: Caller, verb: str
) -> None:
    """Documented: an action the bot lacks the permission for is 403 `NotEnoughPermissions`; a bot may change only
    what it sent, so a person's message stays as they wrote it. Class (a), TEAMS_CODES."""
    pushed = await _posted(
        tenant,
        bot,
        PersonPosts(provider="microsoft", person="sofia", channel="general", text="Mine", mentions_agent=True),
    )
    url = f"{caller.activities(caller.general)}/{pushed['id']}"
    written = caller.stored(pushed["id"]).text
    if verb == "update":
        answered = await caller.http.put(url, json={"type": "message", "text": "Rewritten"}, headers=caller.auth)
    else:
        answered = await caller.http.delete(url, headers=caller.auth)
    assert refusal(answered, 403) == "NotEnoughPermissions"
    assert caller.stored(pushed["id"]).text == written


# ---------------------------------------------------------------------- creating a conversation


def _create(caller: Caller, member: str, **changed: Any) -> dict[str, Any]:
    d = caller.tenant.directory
    body: dict[str, Any] = {
        "bot": {"id": d.bot_app_id},
        "members": [{"id": member}],
        "channelData": {"tenant": {"id": d.tenant_id}},
        "isGroup": False,
    }
    return {**body, **changed}


async def test_a_proactive_create_answers_the_persons_one_to_one_conversation(caller: Caller) -> None:
    """Documented: POST /v3/conversations with the user's id and the tenant id answers the 1:1 conversation's id,
    which Teams writes with an `a:` prefix. Class (a), PROACTIVE."""
    user, chat = caller.person("owen")
    answered = await caller.http.post(
        f"{CONNECTOR}v3/conversations", json=_create(caller, user.mri), headers=caller.auth
    )
    assert answered.is_success, answered.text
    assert answered.json()["id"] == chat.id and chat.id.startswith("a:")


async def test_a_proactive_create_without_a_tenant_is_refused_400(caller: Caller) -> None:
    """Documented: the user id and the tenant id must both be supplied. Class (a), PROACTIVE."""
    user, _ = caller.person("owen")
    body = _create(caller, user.mri)
    del body["channelData"]
    answered = await caller.http.post(f"{CONNECTOR}v3/conversations", json=body, headers=caller.auth)
    assert refusal(answered, 400) == "BadArgument"


async def test_a_proactive_create_of_a_group_chat_is_refused_400(caller: Caller) -> None:
    """Documented: a bot cannot create a new group chat proactively. Class (a), PROACTIVE."""
    user, _ = caller.person("owen")
    answered = await caller.http.post(
        f"{CONNECTOR}v3/conversations", json=_create(caller, user.mri, isGroup=True), headers=caller.auth
    )
    assert refusal(answered, 400) == "BadArgument"


async def test_a_proactive_create_naming_another_bot_is_refused_400(caller: Caller) -> None:
    """Observed: `bot.id` naming an app other than the one that signed in is refused 400. Class (b), asserted by the
    old emulator; Microsoft's pages show `bot.id` but do not say what a foreign one answers."""
    user, _ = caller.person("owen")
    answered = await caller.http.post(
        f"{CONNECTOR}v3/conversations",
        json=_create(caller, user.mri, bot={"id": "28:an-app-that-is-not-this-one"}),
        headers=caller.auth,
    )
    assert refusal(answered, 400) == "BadArgument"


# ---------------------------------------------------------------------- teams and rosters


async def test_team_details_answer_the_groups_directory_id_beside_the_thread_id(caller: Caller) -> None:
    """Documented: a team's details carry its Microsoft Entra group id, which is not the team's thread id. Class (a),
    TEAMS_CONTEXT."""
    answered = await caller.http.get(f"{CONNECTOR}v3/teams/{caller.general}", headers=caller.auth)
    details = answered.json()
    assert details["id"] == caller.general
    assert details["aadGroupId"] == caller.tenant.directory.team_id and details["aadGroupId"] != details["id"]


async def test_the_general_channel_is_listed_with_no_name_and_the_teams_own_id(caller: Caller) -> None:
    """Documented: in a team's channel list the default General channel's name is null (it is localised by the
    client) and its id is the team's id. Class (a), TEAMS_CONTEXT. The old emulator listed it named 'General':
    contradicted, see `CLAIMS.md`."""
    answered = await caller.http.get(f"{CONNECTOR}v3/teams/{caller.general}/conversations", headers=caller.auth)
    channels = answered.json()["conversations"]
    general = next(c for c in channels if c["id"] == caller.general)
    assert general["name"] is None


# ---------------------------------------------------------------------- what Teams pushes to the bot


async def _posted(tenant: Tenant, bot: Bot, happening: PersonPosts) -> dict[str, Any]:
    await tenant.provider.happen(happening, bot.target(), tenant.store, tenant.clock, secret="unused")
    return bot.accepted()[-1]


async def test_a_channel_message_names_the_team_by_thread_id_and_by_group_id(tenant: Tenant, bot: Bot) -> None:
    """Documented: a channel activity's `channelData.team` carries the team's `19:…@thread` id and its Entra group id
    (`aadGroupId`), two different ids. Class (a), TEAMS_CONTEXT."""
    pushed = await _posted(
        tenant,
        bot,
        PersonPosts(provider="microsoft", person="sofia", channel="general", text="Bot?", mentions_agent=True),
    )
    team = pushed["channelData"]["team"]
    assert team["id"].startswith("19:") and "@thread." in team["id"]
    assert team["aadGroupId"] == tenant.directory.team_id and team["aadGroupId"] != team["id"]


async def test_a_pushed_message_names_its_sender_by_mri_and_by_directory_id(tenant: Tenant, bot: Bot) -> None:
    """Documented: `from.id` is the user's `29:` id and `from.aadObjectId` their directory object id. Class (a),
    PROACTIVE (which distinguishes the two)."""
    pushed = await _posted(tenant, bot, PersonPosts(provider="microsoft", person="sofia", text="Hi"))
    sofia = tenant.world.person("sofia")
    assert sofia is not None
    assert pushed["from"]["id"].startswith("29:")
    assert pushed["from"]["aadObjectId"] == sofia.user.id


async def test_a_reply_in_a_channel_thread_names_its_root_in_the_conversation_id(tenant: Tenant, bot: Bot) -> None:
    """Observed: a reply inside a channel thread arrives with `;messageid=<root>` on `conversation.id`. Class (b),
    asserted by the old emulator from production traffic."""
    root = await _posted(
        tenant,
        bot,
        PersonPosts(
            provider="microsoft", person="dania", channel="general", text="Kickoff", mentions_agent=True, key="kick"
        ),
    )
    reply = await _posted(
        tenant,
        bot,
        PersonPosts(
            provider="microsoft",
            person="sofia",
            channel="general",
            text="Count me in",
            mentions_agent=True,
            in_thread_of="kick",
        ),
    )
    assert reply["conversation"]["id"].endswith(f";messageid={root['id']}")
