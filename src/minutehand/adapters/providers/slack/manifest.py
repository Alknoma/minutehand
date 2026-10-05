"""What the Slack provider claims. Data only: nothing else in the provider is imported to read it."""

from __future__ import annotations

from minutehand.domain.provider import (
    AccountFact,
    ChannelField,
    Manifest,
    MessagingKind,
    PersonChange,
    PersonFact,
    Tier,
)
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="slack",
    tier=Tier.FINISHED,
    hosts=["slack.com", "*.slack.com"],
    kinds=[EntityKind.MESSAGE, EntityKind.CHANNEL],
    pushes_events=True,
    people_changes=[PersonChange.DEACTIVATED, PersonChange.REACTIVATED],
    channel_fields=[
        ChannelField.NAMED,
        ChannelField.DIRECT,
        ChannelField.PRIVATE,
        ChannelField.ARCHIVED,
        ChannelField.TOPIC,
        ChannelField.PURPOSE,
        ChannelField.WITHOUT_AGENT,
        ChannelField.HISTORY,
        ChannelField.THREADS,
        ChannelField.FILES,
        ChannelField.ID,
        ChannelField.POST_ID,
    ],
    sign_ins=True,
    account_facts=[AccountFact.ID, AccountFact.NAME, AccountFact.EMAIL_HIDDEN],
    person_facts=[
        PersonFact.WITHOUT_EMAIL,
        PersonFact.TITLE,
        PersonFact.GUEST,
        PersonFact.DEACTIVATED,
        PersonFact.BOT,
        PersonFact.WORKING_HOURS,
        PersonFact.ABSENCES,
    ],
    messaging_happenings=[
        MessagingKind.POSTS,
        MessagingKind.EDITS,
        MessagingKind.DELETES,
        MessagingKind.REACTS,
        MessagingKind.JOINS,
        MessagingKind.ADDS_AGENT,
        MessagingKind.OPENS_AGENT,
        MessagingKind.COMMANDS,
    ],
)
