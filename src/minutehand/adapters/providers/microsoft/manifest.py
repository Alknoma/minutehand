"""What the Microsoft provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import (
    AccountFact,
    ChannelField,
    DocumentChange,
    DocumentField,
    Manifest,
    MessagingKind,
    PersonChange,
    PersonFact,
    SpaceField,
    Tier,
    WorldKey,
)
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="microsoft",
    tier=Tier.FINISHED,
    hosts=[
        "login.microsoftonline.com",
        "login.botframework.com",
        "smba.trafficmanager.net",
        "graph.microsoft.com",
        "*.sharepoint.com",
    ],
    kinds=[EntityKind.MESSAGE, EntityKind.CHANNEL, EntityKind.DOCUMENT],
    pushes_events=True,
    shared_hosts=["login.botframework.com"],
    world_keys=[
        WorldKey(path="/{key}/oauth2/v2.0/"),
        WorldKey(path="/{key}/v2.0/.well-known/"),
        WorldKey(path="/{key}/discovery/"),
        WorldKey(host="{key}.sharepoint.com"),
        WorldKey(host="{key}-my.sharepoint.com"),
    ],
    document_changes=[
        DocumentChange.EDITED,
        DocumentChange.RENAMED,
        DocumentChange.MOVED,
        DocumentChange.SHARED,
        DocumentChange.TRASHED,
    ],
    people_changes=[PersonChange.REMOVED, PersonChange.DEACTIVATED, PersonChange.REACTIVATED],
    document_fields=[
        DocumentField.FOLDER,
        DocumentField.OWNER,
        DocumentField.SPACE,
        DocumentField.SHARED_WITH,
        DocumentField.MODIFIED_BEFORE_START,
        DocumentField.MODIFIED_BY,
        DocumentField.ID,
    ],
    channel_fields=[
        ChannelField.NAMED,
        ChannelField.DIRECT,
        ChannelField.PRIVATE,
        ChannelField.TOPIC,
        ChannelField.PURPOSE,
        ChannelField.WITHOUT_AGENT,
        ChannelField.HISTORY,
        ChannelField.THREADS,
        ChannelField.FILES,
        ChannelField.ID,
        ChannelField.POST_ID,
    ],
    space_fields=[SpaceField.SPACES, SpaceField.ID],
    account_facts=[AccountFact.LOGIN, AccountFact.ID, AccountFact.NAME, AccountFact.EMAIL_HIDDEN],
    person_facts=[
        PersonFact.WITHOUT_EMAIL,
        PersonFact.TITLE,
        PersonFact.GUEST,
        PersonFact.DEACTIVATED,
        PersonFact.BOT,
        PersonFact.ABSENCES,
    ],
    messaging_happenings=[
        MessagingKind.POSTS,
        MessagingKind.EDITS,
        MessagingKind.DELETES,
        MessagingKind.REACTS,
        MessagingKind.JOINS,
        MessagingKind.ADDS_AGENT,
        MessagingKind.COMMANDS,
    ],
)
