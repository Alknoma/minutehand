"""The workspace lives in the store and nowhere else: a new app sees it, a fork sees it as of the fork."""

from __future__ import annotations

import subprocess
import sys

import httpx

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.providers.slack.state import BOT_USER_ID
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.provider import Tier
from minutehand.domain.world import Actor, EntityKind
from tests.providers.slack.slack_workspace import (
    GENERAL, SCENARIO, START, Workspace, body, client_for, form, text_of,
)


async def test_the_workspace_survives_a_new_app_over_a_new_connection(workspace: Workspace) -> None:
    async with client_for(workspace.provider, workspace.store, workspace.clock) as first:
        await body(first, "chat.postMessage", channel=GENERAL, text="written by the first app")

    reopened = SqliteStore(workspace.path, "root", RunClock(START))
    async with client_for(build(), reopened, RunClock(START)) as second:
        history = await form(second, "conversations.history", channel=GENERAL)
        dm = await form(second, "conversations.open", users=state.user_id("iris"))

    assert text_of(history) == ["written by the first app"]
    assert dm["already_open"] is True


async def test_a_fork_sees_history_only_up_to_the_fork(workspace: Workspace, client: httpx.AsyncClient) -> None:
    await body(client, "chat.postMessage", channel=GENERAL, text="before the fork")
    at = workspace.store.head()
    await body(client, "chat.postMessage", channel=GENERAL, text="after, in the parent")

    fork = workspace.store.fork("what-if", at_seq=at, clock=workspace.clock)
    async with client_for(workspace.provider, fork, workspace.clock) as forked:
        assert text_of(await form(forked, "conversations.history", channel=GENERAL)) == ["before the fork"]
        await body(forked, "chat.postMessage", channel=GENERAL, text="only in the fork")
        assert text_of(await form(forked, "conversations.history", channel=GENERAL)) == [
            "only in the fork", "before the fork",
        ]

    assert text_of(await form(client, "conversations.history", channel=GENERAL)) == [
        "after, in the parent", "before the fork",
    ]


def test_seeding_writes_the_people_the_bot_their_dms_and_general_as_the_scenario(workspace: Workspace) -> None:
    events = workspace.store.events()
    assert events and {e.actor for e in events} == {Actor.SCENARIO}

    slack = workspace.slack
    assert [u.id for u in slack.every_user()] == sorted([BOT_USER_ID, *(state.user_id(p.key) for p in SCENARIO.people)])
    assert sorted(slack.every_member(GENERAL)) == sorted(u.id for u in slack.every_user())
    for person in SCENARIO.people:
        dm = slack.channel(workspace.dm(person.key))
        assert dm is not None and dm.is_im and dm.user == state.user_id(person.key)
        assert sorted(slack.every_member(dm.id)) == sorted([BOT_USER_ID, state.user_id(person.key)])
        user = slack.user(state.user_id(person.key))
        assert user is not None and user.profile.email == person.email and user.real_name == person.name


def test_member_ids_are_derived_from_the_person_key_alone() -> None:
    assert state.user_id("iris") == state.user_id("iris") != state.user_id("tomas")
    assert state.user_id("iris").startswith("U")


def test_the_manifest_claims_slack_and_imports_nothing_else_of_the_provider() -> None:
    assert (MANIFEST.key, MANIFEST.tier, MANIFEST.hosts) == ("slack", Tier.FINISHED, ["slack.com", "*.slack.com"])
    assert MANIFEST.pushes_events and MANIFEST.kinds == [EntityKind.MESSAGE, EntityKind.CHANNEL]
    loaded = subprocess.run(
        [sys.executable, "-c",
         "import sys, minutehand.adapters.providers.slack.manifest;"
         "print(sorted(m for m in sys.modules if m.startswith('minutehand.adapters.providers.slack.')))"],
        capture_output=True, text=True, check=True,
    )
    assert loaded.stdout.strip() == "['minutehand.adapters.providers.slack.manifest']"
