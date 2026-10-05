"""Microsoft's own JSON: the only module that parses or builds it.

Five families, one per surface the provider answers:

- **Sign-in** — the identity platform's token answer and error, OpenID metadata, the JSON Web Key Set, and the
  claims of every token this provider issues.
- **Bot Framework** — the activity in the shape the connector stores and pushes it, and the bodies a bot sends.
- **Graph, Teams** — users, teams, channels, chats, chat messages, conversation members.
- **Graph, files** — sites, drives, drive items, permissions, upload sessions.
- **Graph, common** — the OData collection envelope, the error envelope, subscriptions and change notifications.

What a caller SENDS is read with unknown fields ignored, as the real services ignore them; what this provider
STORES and ANSWERS is a frozen model with unknown fields refused. `$select` is applied to an answer's JSON text
here, never to a model.
"""

from __future__ import annotations

import base64
import binascii
import json
from enum import StrEnum
from typing import Literal, TypeVar
from urllib.parse import parse_qs

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from minutehand.domain.scenario import Model


class Lenient(BaseModel):
    """A body a caller sends: the field this provider reads are typed, anything else is ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class Aliased(Model):
    """A stored or answered body whose JSON names are not Python names (`from`, `@odata.type`)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


def dump(entity: BaseModel) -> str:
    return entity.model_dump_json(by_alias=True, exclude_none=True)


M = TypeVar("M", bound=BaseModel)


def parse(model: type[M], body: str | bytes) -> M:
    return model.model_validate_json(body)


class Unreadable(Exception):
    """A request body that is not the JSON the route takes. `field` names what was wrong."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def read(model: type[M], body: bytes) -> M:
    try:
        return model.model_validate_json(body or b"{}")
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "body"
        raise Unreadable(f"{where}: {first['msg']}") from e


# =========================================================================== sign-in


class TokenUse(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"
    CODE = "code"
    ACTIVITY = "activity"


class JoseHeader(Lenient):
    alg: str
    kid: str | None = None


class Claims(Model):
    iss: str
    aud: str
    iat: int
    nbf: int
    exp: int
    tid: str | None = None
    appid: str
    azp: str
    oid: str | None = None
    sub: str
    roles: list[str] | None = None
    scp: str | None = None
    serviceurl: str | None = None
    ver: str
    nonce: str | None = None
    uti: str
    preferred_username: str | None = None
    name: str | None = None
    email: str | None = None
    redirect_uri: str | None = None
    minutehand_use: TokenUse


def dump_claims(claims: Claims) -> str:
    return claims.model_dump_json(exclude_none=True)


class TokenRequest(Lenient):
    """A token request's form body (RFC 6749 §4)."""

    grant_type: str = ""
    client_id: str = ""
    client_secret: str | None = None
    scope: str = ""
    code: str | None = None
    redirect_uri: str | None = None
    refresh_token: str | None = None


def read_form(model: type[M], body: bytes) -> M:
    fields = {k: v[0] for k, v in parse_qs(body.decode("utf-8", errors="replace")).items() if v}
    return model.model_validate(fields)


class TokenAnswer(Model):
    token_type: Literal["Bearer"] = "Bearer"
    scope: str
    expires_in: int
    ext_expires_in: int
    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None


class TokenError(Model):
    """The identity platform's error: an OAuth `error` and an `AADSTS` description."""

    error: str
    error_description: str
    error_codes: list[int]
    timestamp: str
    trace_id: str
    correlation_id: str


class OpenIdConfiguration(Model):
    issuer: str
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    jwks_uri: str
    id_token_signing_alg_values_supported: list[str]
    token_endpoint_auth_methods_supported: list[str] | None = None
    response_types_supported: list[str] | None = None
    subject_types_supported: list[str] | None = None
    scopes_supported: list[str] | None = None
    tenant_region_scope: str | None = None


class Jwk(Model):
    kty: Literal["RSA"] = "RSA"
    use: Literal["sig"] = "sig"
    kid: str
    n: str
    e: str
    endorsements: list[str] | None = None


class JwkSet(Model):
    keys: list[Jwk]


# =========================================================================== Bot Framework


class ActivityType(StrEnum):
    MESSAGE = "message"
    CONVERSATION_UPDATE = "conversationUpdate"
    INSTALLATION_UPDATE = "installationUpdate"
    INVOKE = "invoke"
    MESSAGE_REACTION = "messageReaction"
    MESSAGE_UPDATE = "messageUpdate"
    MESSAGE_DELETE = "messageDelete"


class ConversationType(StrEnum):
    PERSONAL = "personal"
    GROUP_CHAT = "groupChat"
    CHANNEL = "channel"


class ChannelAccount(Aliased):
    id: str
    name: str | None = None
    aadObjectId: str | None = None
    role: str | None = None


class ConversationAccount(Aliased):
    id: str
    conversationType: ConversationType | None = None
    tenantId: str | None = None
    isGroup: bool | None = None
    name: str | None = None


class Attachment(Aliased):
    contentType: str
    content: JsonValue = None
    contentUrl: str | None = None
    name: str | None = None
    id: str | None = None


ADAPTIVE_CARD = "application/vnd.microsoft.card.adaptive"


class Mention(Aliased):
    type: Literal["mention"] = "mention"
    mentioned: ChannelAccount
    text: str


class TenantInfo(Aliased):
    id: str


class TeamInfo(Aliased):
    id: str
    name: str | None = None
    aadGroupId: str | None = None


class ChannelInfo(Aliased):
    id: str
    name: str | None = None


class ChannelData(Aliased):
    tenant: TenantInfo | None = None
    team: TeamInfo | None = None
    channel: ChannelInfo | None = None
    eventType: str | None = None
    userPrincipalName: str | None = None


class Reaction(Aliased):
    type: str


class Activity(Aliased):
    """One Bot Framework activity, as the connector stores it and as it is pushed to a bot."""

    type: ActivityType
    id: str
    timestamp: str
    localTimestamp: str | None = None
    serviceUrl: str
    channelId: Literal["msteams"] = "msteams"
    sender: ChannelAccount = Field(validation_alias=AliasChoices("sender", "from"), serialization_alias="from")
    conversation: ConversationAccount
    recipient: ChannelAccount
    text: str | None = None
    textFormat: str | None = None
    attachments: list[Attachment] | None = None
    entities: list[Mention] | None = None
    channelData: ChannelData | None = None
    replyToId: str | None = None
    value: JsonValue = None
    name: str | None = None
    action: str | None = None
    membersAdded: list[ChannelAccount] | None = None
    membersRemoved: list[ChannelAccount] | None = None
    reactionsAdded: list[Reaction] | None = None
    reactionsRemoved: list[Reaction] | None = None
    locale: str | None = None


class SentActivity(Lenient):
    """What a bot sends to the connector. The connector fills in the rest."""

    type: str = "message"
    id: str | None = None
    text: str | None = None
    textFormat: str | None = None
    attachments: list[Attachment] | None = None
    entities: list[JsonValue] | None = None
    replyToId: str | None = None
    summary: str | None = None


class SentConversation(Lenient):
    """`POST /v3/conversations`: the conversation a bot asks the connector to create."""

    bot: ChannelAccount | None = None
    members: list[ChannelAccount] = []
    isGroup: bool | None = None
    topicName: str | None = None
    channelData: ChannelData | None = None
    tenantId: str | None = None
    activity: SentActivity | None = None


class ResourceResponse(Model):
    id: str


class ConversationResourceResponse(Model):
    id: str
    serviceUrl: str
    activityId: str | None = None


class TeamsChannelAccount(Aliased):
    id: str
    name: str
    aadObjectId: str
    email: str | None = None
    userPrincipalName: str
    givenName: str | None = None
    surname: str | None = None
    tenantId: str
    userRole: Literal["user", "bot"] = "user"


class PagedMembers(Model):
    members: list[TeamsChannelAccount]
    continuationToken: str | None = None


class TeamDetails(Model):
    id: str
    name: str
    aadGroupId: str


class ChannelSummary(Model):
    """One channel in a team's list. The General channel's `name` is sent as null, not left out: Teams localises it
    on the client."""

    id: str
    name: str | None


class ConversationList(Model):
    conversations: list[ChannelSummary]


class ConnectorErrorBody(Model):
    code: str
    message: str


class ConnectorError(Model):
    error: ConnectorErrorBody


class InvokeAnswer(Lenient):
    """What a bot answers an `invoke` with: `statusCode`, `type` and `value`. A card in `value` replaces the card
    the person pressed."""

    statusCode: int = 200
    type: str | None = None
    value: JsonValue = None


ACTIVITY_MESSAGE_ANSWER = "application/vnd.microsoft.activity.message"


# =========================================================================== Graph, common


class InnerError(Aliased):
    date: str
    request_id: str = Field(validation_alias=AliasChoices("request_id", "request-id"), serialization_alias="request-id")
    client_request_id: str = Field(
        validation_alias=AliasChoices("client_request_id", "client-request-id"), serialization_alias="client-request-id"
    )


class GraphErrorBody(Aliased):
    code: str
    message: str
    innerError: InnerError


class GraphError(Model):
    error: GraphErrorBody


class Page[T: BaseModel](Aliased):
    context: str = Field(
        validation_alias=AliasChoices("context", "@odata.context"), serialization_alias="@odata.context"
    )
    count: int | None = Field(
        default=None, validation_alias=AliasChoices("count", "@odata.count"), serialization_alias="@odata.count"
    )
    value: list[T]
    next_link: str | None = Field(
        default=None,
        validation_alias=AliasChoices("next_link", "@odata.nextLink"),
        serialization_alias="@odata.nextLink",
    )
    delta_link: str | None = Field(
        default=None,
        validation_alias=AliasChoices("delta_link", "@odata.deltaLink"),
        serialization_alias="@odata.deltaLink",
    )


def select(answer: str, fields: list[str] | None, *, keep: frozenset[str] = frozenset()) -> str:
    """`$select` over one entity's JSON: its named properties, `id` and the OData annotations, and nothing else."""
    if not fields:
        return answer
    found = json.loads(answer)
    assert isinstance(found, dict)
    wanted = set(fields) | {"id"} | keep
    return json.dumps({k: v for k, v in found.items() if k in wanted or k.startswith("@")})


def select_page(answer: str, fields: list[str] | None, *, keep: frozenset[str] = frozenset()) -> str:
    if not fields:
        return answer
    found = json.loads(answer)
    assert isinstance(found, dict)
    values = found["value"]
    assert isinstance(values, list)
    wanted = set(fields) | {"id"} | keep
    found["value"] = [{k: v for k, v in item.items() if k in wanted or k.startswith("@")} for item in values]
    return json.dumps(found)


def with_context(answer: str, context: str) -> str:
    """One entity's JSON with `@odata.context` first, as Graph answers a single entity."""
    found = json.loads(answer)
    assert isinstance(found, dict)
    return json.dumps({"@odata.context": context, **found})


# =========================================================================== Graph, Teams


class GraphUser(Aliased):
    id: str
    displayName: str
    givenName: str | None = None
    surname: str | None = None
    mail: str | None = None
    userPrincipalName: str
    jobTitle: str | None = None
    businessPhones: list[str] = []
    accountEnabled: bool | None = Field(default=None, description="Said only once an administrator changed it")


class OutOfOfficeSettings(Aliased):
    message: str | None = None
    isOutOfOffice: bool


class Presence(Aliased):
    """`GET /users/{id}/presence`: `Away` and `OutOfOffice` while the person is out, else `Available`."""

    id: str
    availability: str
    activity: str
    outOfOfficeSettings: OutOfOfficeSettings


class PresencesByUserId(Aliased):
    """`POST /communications/getPresencesByUserId`'s body."""

    ids: list[str] = Field(min_length=1)


class DateTimeTimeZone(Aliased):
    dateTime: str
    timeZone: str = "UTC"


class AutomaticRepliesSetting(Aliased):
    """A person's automatic replies: `scheduled` with its window while an absence is known, else `disabled`."""

    status: Literal["disabled", "alwaysEnabled", "scheduled"]
    externalAudience: Literal["none", "contactsOnly", "all"] = "all"
    scheduledStartDateTime: DateTimeTimeZone | None = None
    scheduledEndDateTime: DateTimeTimeZone | None = None
    internalReplyMessage: str = ""
    externalReplyMessage: str = ""


class MailboxSettings(Aliased):
    automaticRepliesSetting: AutomaticRepliesSetting
    timeZone: str = "UTC"


class GraphTeam(Aliased):
    id: str
    displayName: str
    description: str | None = None
    internalId: str
    webUrl: str


class GraphChannel(Aliased):
    id: str
    displayName: str
    description: str | None = None
    membershipType: Literal["standard", "private", "shared"] = "standard"
    createdDateTime: str
    webUrl: str
    tenantId: str


class GraphChat(Aliased):
    id: str
    topic: str | None = None
    chatType: Literal["oneOnOne", "group", "meeting"]
    createdDateTime: str
    lastUpdatedDateTime: str
    tenantId: str
    webUrl: str


class Identity(Aliased):
    id: str
    displayName: str | None = None
    userIdentityType: str | None = None
    applicationIdentityType: str | None = None
    tenantId: str | None = None


class IdentitySet(Aliased):
    user: Identity | None = None
    application: Identity | None = None


class ItemBody(Aliased):
    contentType: Literal["text", "html"] = "html"
    content: str


class ChannelIdentity(Aliased):
    teamId: str
    channelId: str


class ChatMessageAttachment(Aliased):
    id: str
    contentType: str
    content: str | None = None
    contentUrl: str | None = None
    name: str | None = None


class MentionedIdentity(Aliased):
    user: Identity | None = None
    application: Identity | None = None


class ChatMessageMention(Aliased):
    id: int
    mentionText: str
    mentioned: MentionedIdentity


class ChatMessageReaction(Aliased):
    reactionType: str
    createdDateTime: str
    user: IdentitySet


class ChatMessage(Aliased):
    id: str
    replyToId: str | None = None
    etag: str
    messageType: Literal["message", "systemEventMessage"] = "message"
    createdDateTime: str
    lastModifiedDateTime: str
    lastEditedDateTime: str | None = None
    deletedDateTime: str | None = None
    subject: str | None = None
    chatId: str | None = None
    channelIdentity: ChannelIdentity | None = None
    sender: IdentitySet | None = Field(
        default=None, validation_alias=AliasChoices("sender", "from"), serialization_alias="from"
    )
    body: ItemBody
    attachments: list[ChatMessageAttachment] = []
    mentions: list[ChatMessageMention] = []
    reactions: list[ChatMessageReaction] = []
    webUrl: str | None = None
    replies: list[ChatMessage] | None = None
    replies_context: str | None = Field(
        default=None,
        validation_alias=AliasChoices("replies_context", "replies@odata.context"),
        serialization_alias="replies@odata.context",
    )


class SentChatMessage(Lenient):
    body: ItemBody
    subject: str | None = None


class ConversationMember(Aliased):
    odata_type: Literal["#microsoft.graph.aadUserConversationMember"] = Field(
        default="#microsoft.graph.aadUserConversationMember",
        validation_alias=AliasChoices("odata_type", "@odata.type"),
        serialization_alias="@odata.type",
    )
    id: str
    roles: list[str] = []
    displayName: str
    userId: str
    email: str | None = None
    tenantId: str


# =========================================================================== Graph, files


class SiteCollection(Aliased):
    hostname: str


class Site(Aliased):
    id: str
    name: str
    displayName: str
    webUrl: str
    createdDateTime: str
    lastModifiedDateTime: str
    siteCollection: SiteCollection


class DriveOwner(Aliased):
    user: Identity | None = None
    group: Identity | None = None


class Quota(Aliased):
    total: int
    used: int
    remaining: int
    state: Literal["normal"] = "normal"


class Drive(Aliased):
    id: str
    name: str
    driveType: Literal["business", "documentLibrary", "personal"]
    webUrl: str
    createdDateTime: str
    lastModifiedDateTime: str
    owner: DriveOwner
    quota: Quota | None = None


class ItemReference(Aliased):
    driveId: str
    driveType: str
    id: str | None = None
    name: str | None = None
    path: str | None = None
    siteId: str | None = None


class Hashes(Aliased):
    quickXorHash: str | None = None
    sha256Hash: str | None = None


class FileFacet(Aliased):
    mimeType: str
    hashes: Hashes | None = None


class FolderFacet(Aliased):
    childCount: int


class DeletedFacet(Aliased):
    state: Literal["deleted"] = "deleted"


class RootFacet(Aliased):
    pass


class Shared(Aliased):
    scope: str


class DriveItem(Aliased):
    """A file or folder as Graph answers it. `content` is never part of the answer: it lives beside it in
    `StoredItem`."""

    id: str
    name: str
    eTag: str
    cTag: str
    size: int
    createdDateTime: str
    lastModifiedDateTime: str
    webUrl: str
    createdBy: IdentitySet
    lastModifiedBy: IdentitySet
    parentReference: ItemReference
    file: FileFacet | None = None
    folder: FolderFacet | None = None
    root: RootFacet | None = None
    deleted: DeletedFacet | None = None
    shared: Shared | None = None
    download_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("download_url", "@microsoft.graph.downloadUrl"),
        serialization_alias="@microsoft.graph.downloadUrl",
    )


class StoredItem(Model):
    """A drive item as the store holds it: Graph's own answer, the bytes, and the lock a person may hold."""

    item: DriveItem
    content_b64: str | None = None
    version: int = 1


def content_of(stored: StoredItem) -> bytes:
    return base64.b64decode(stored.content_b64) if stored.content_b64 is not None else b""


def encoded(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


class SentItem(Lenient):
    """A PATCH, a folder create, or a copy: the fields a caller may set on an item."""

    name: str | None = None
    folder: JsonValue = None
    file: JsonValue = None
    parentReference: ItemReference | None = None
    conflict_behavior: str | None = Field(
        default=None,
        validation_alias=AliasChoices("conflict_behavior", "@microsoft.graph.conflictBehavior"),
        serialization_alias="@microsoft.graph.conflictBehavior",
    )


class ParentReferenceIn(Lenient):
    driveId: str | None = None
    id: str | None = None
    path: str | None = None


class CopyRequest(Lenient):
    name: str | None = None
    parentReference: ParentReferenceIn | None = None


class MoveRequest(Lenient):
    name: str | None = None
    parentReference: ParentReferenceIn | None = None
    file: JsonValue = None
    folder: JsonValue = None


class UploadSessionItem(Lenient):
    conflict_behavior: str | None = Field(
        default=None,
        validation_alias=AliasChoices("conflict_behavior", "@microsoft.graph.conflictBehavior"),
        serialization_alias="@microsoft.graph.conflictBehavior",
    )
    name: str | None = None


class UploadSessionRequest(Lenient):
    item: UploadSessionItem | None = None


class UploadSession(Model):
    uploadUrl: str
    expirationDateTime: str
    nextExpectedRanges: list[str]


class StoredUploadSession(Model):
    """An upload in progress, kept in the store: where it lands and the bytes received so far."""

    session: str
    drive: str
    parent: str
    name: str
    existing: str | None = None
    conflict: str
    expected: int | None = None
    received_b64: str = ""
    expires: str
    by: IdentitySet


class SharingLink(Aliased):
    type: str
    scope: str
    webUrl: str


class SharePointIdentity(Aliased):
    user: Identity | None = None


class Permission(Aliased):
    id: str
    roles: list[str]
    link: SharingLink | None = None
    grantedToV2: SharePointIdentity | None = None
    shareId: str | None = None


class CreateLinkRequest(Lenient):
    type: str
    scope: str | None = None


class InviteRecipient(Lenient):
    email: str


class InviteRequest(Lenient):
    recipients: list[InviteRecipient]
    roles: list[str]
    requireSignIn: bool | None = None
    sendInvitation: bool | None = None


class AsyncOperationStatus(Model):
    status: Literal["notStarted", "inProgress", "completed", "failed"]
    percentageComplete: float
    resourceId: str | None = None


# =========================================================================== subscriptions


class Subscription(Aliased):
    odata_context: str | None = Field(
        default=None,
        validation_alias=AliasChoices("odata_context", "@odata.context"),
        serialization_alias="@odata.context",
    )
    id: str
    resource: str
    applicationId: str
    changeType: str
    clientState: str | None = None
    notificationUrl: str
    lifecycleNotificationUrl: str | None = None
    expirationDateTime: str
    creatorId: str
    latestSupportedTlsVersion: str = "v1_2"
    notificationContentType: str | None = None


class SubscriptionRequest(Lenient):
    changeType: str = ""
    notificationUrl: str = ""
    resource: str = ""
    expirationDateTime: str = ""
    clientState: str | None = None
    lifecycleNotificationUrl: str | None = None


class SubscriptionPatch(Lenient):
    expirationDateTime: str


class ResourceData(Aliased):
    odata_type: str = Field(
        validation_alias=AliasChoices("odata_type", "@odata.type"), serialization_alias="@odata.type"
    )
    odata_id: str = Field(validation_alias=AliasChoices("odata_id", "@odata.id"), serialization_alias="@odata.id")
    id: str


class ChangeNotification(Aliased):
    subscriptionId: str
    clientState: str | None = None
    changeType: str | None = None
    lifecycleEvent: str | None = None
    resource: str
    subscriptionExpirationDateTime: str
    tenantId: str
    resourceData: ResourceData | None = None


class NotificationCollection(Model):
    value: list[ChangeNotification]


def b64_bytes(text: str) -> bytes:
    try:
        return base64.b64decode(text)
    except binascii.Error as e:
        raise Unreadable("not base64") from e


# =========================================================================== faults


class StoredHold(Model):
    """Minutehand's own: a person holds a file open for editing from a moment, for a while or for good, so every
    write to it is refused 423, as SharePoint refuses a locked file."""

    position: int
    item: str
    by: str = Field(description="The holder's email")
    from_time: int = Field(description="Simulated seconds since the epoch")
    until_time: int | None = Field(default=None, description="None: held for good")


class StoredFault(Model):
    """Minutehand's own: a call the scenario fails on purpose, and how many more times it will."""

    position: int
    call: str | None = Field(default=None, description="'METHOD /path prefix' or '/path prefix'; None: every call")
    error: str = Field(description="Graph's or the connector's own error code")
    status: int
    retry_after: int | None = Field(default=None, description="Seconds; set for a rate limit or an outage")
    remaining: int | None = Field(default=None, description="None: every call")
    from_time: int = Field(description="Simulated seconds since the epoch from which it applies")
    only_rich: bool = Field(default=False, description="Only a connector POST: a send, a reply or a new conversation")
    without_id: bool = Field(default=False, description="Answered as sent, with no id; only a connector POST")
