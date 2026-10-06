"""Slack names what it seeds by what it is: an open world takes more of every kind its seed has (people, channels
with history, sign-ins, workspaces, a workspace's members, members without email, faults) and nothing it held
moves; a seeded message's `ts` is its channel's and its second's, and a message minted later never takes one."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld, WorldRefused
from minutehand.domain.scenario import Person, ProviderSeed, Seed, SeededChannel, SignIn
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]
HISTORY = [
    {"by": "sofia", "text": "first", "ago": "PT3H", "replies": [{"by": "owen", "text": "re", "ago": "PT2H"}]},
    {"by": "owen", "text": "second", "ago": "PT1H"},
    {"by": "sofia", "text": "third", "ago": "PT1H"},
    {"by": "owen", "text": "second", "ago": "PT1H"},
]
IVY = Person.model_validate({"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com", "reply": {"kind": "silent"}})
WORKSPACES: dict[str, object] = {"workspaces": [{"team_id": "TALPHA", "domain": "alpha", "bot_user_id": "UALPHABOT",
                              "members": ["owen", "sofia"], "tokens": ["xoxb-alpha"]}]}  # fmt: skip


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


def _seed(slack: dict[str, object] | None = None) -> dict[str, object]:
    seed: dict[str, object] = {
        "starts_at": START.isoformat(),
        "people": PEOPLE,
        "channels": [{"provider": "slack", "name": "launch", "members": ["owen", "sofia"], "history": HISTORY}],
    }
    if slack is not None:
        seed["provider_seeds"] = [{"provider": "slack", "body": json.dumps(slack)}]
    return seed


Opener = Callable[[dict[str, object]], StandingWorld]


@pytest.fixture
def opened(tmp_path: Path) -> Iterator[tuple[Opener, Path]]:
    worlds: list[StandingWorld] = []

    def open_world(seed: dict[str, object]) -> StandingWorld:
        registry = Registry.installed()
        manifests = {m.key: m for m in registry.manifests}
        scenario = Seed.model_validate(seed).starting(START)
        clock = RunClock(scenario.starts_at)
        store = SqliteStore(tmp_path / f"world-{len(worlds)}.db", "world", clock)
        world = StandingWorld(
            scenario=scenario,
            store=store,
            clock=clock,
            provider=lambda k: registry.provider(manifests[k]),
            inbound=[],
            signing={},
            scripted=False,
        )
        world.open(["slack"])
        worlds.append(world)
        return world

    yield open_world, tmp_path
    for world in worlds:
        assert isinstance(world.store, SqliteStore)
        world.store.close()


def _held(store: Store) -> dict[tuple[EntityKind, str], str]:
    found: dict[tuple[EntityKind, str], str] = {}
    for event in store.events():
        stored = store.get(event.entity)
        if event.entity.provider == "slack" and stored is not None:
            found[(event.entity.kind, event.entity.external_id)] = stored.body
    return found


def _users(world: StandingWorld) -> dict[str, wire.SlackUser]:
    return {u.name: u for u in state.SlackWorld(world.store).every_user()}


def _slack(body: dict[str, object]) -> ProviderSeed:
    return ProviderSeed(provider="slack", body=json.dumps(body))


@dataclass(frozen=True)
class Addition:
    """What a world is opened with besides its people and `#launch` (`opened_with`), and what is added to it."""

    opened_with: dict[str, object] | None = None
    people: list[Person] = field(default_factory=list[Person])
    channels: list[SeededChannel] = field(default_factory=list[SeededChannel])
    sign_ins: list[SignIn] = field(default_factory=list[SignIn])
    provider_seeds: list[ProviderSeed] = field(default_factory=list[ProviderSeed])


OPS = {"provider": "slack", "name": "ops", "members": ["owen"], "history": [{"by": "owen", "text": "x", "ago": "PT1H"}]}
BETA = {"team_id": "TBETA", "domain": "beta", "bot_user_id": "UBETABOT", "members": ["owen"], "tokens": ["xoxb-beta"]}
REFUSED = {"call": "auth.test", "answer": {"kind": "refused", "error": "account_inactive"}}
JOINED = _slack({"joined": [{"team_id": "TALPHA", "person": "ivy"}]})
ADDITIONS = {
    "a person": Addition(people=[IVY]),
    "a channel with history": Addition(channels=[SeededChannel.model_validate(OPS)]),
    "a sign-in": Addition(sign_ins=[SignIn(provider="slack", credential="xoxb-added", person="owen")]),
    "a fault": Addition(provider_seeds=[_slack({"faults": [REFUSED]})]),
    "a second workspace": Addition(opened_with=WORKSPACES, provider_seeds=[_slack({"workspaces": [BETA]})]),
    "a person joining a declared workspace": Addition(opened_with=WORKSPACES, people=[IVY], provider_seeds=[JOINED]),
    "a person without email": Addition(people=[IVY], provider_seeds=[_slack({"without_email": ["ivy"]})]),
}


@pytest.mark.parametrize("addition", list(ADDITIONS))
def test_an_open_slack_world_takes_each_kind_of_addition_and_moves_nothing(
    opened: tuple[Opener, Path], addition: str
) -> None:
    open_world, directory = opened
    added = ADDITIONS[addition]
    world = open_world(_seed(added.opened_with))
    before = _held(world.store)
    written = world.extend(
        people=added.people,
        channels=added.channels,
        sign_ins=added.sign_ins,
        provider_seeds=added.provider_seeds,
        directory=directory,
        scratch=_scratch,
    )
    after = _held(world.store)
    assert written["slack"] > 0
    assert {k: v for k, v in after.items() if k in before} == before, "something seeded moved or changed"


def test_an_added_person_lands_as_a_member_with_their_dm(opened: tuple[Opener, Path]) -> None:
    open_world, directory = opened
    world = open_world(_seed())
    world.extend(people=[IVY], directory=directory, scratch=_scratch)
    ivy = _users(world)["ivy"]
    assert ivy.profile.email == "ivy@example.com"
    dm = state.conversation_id([state.BOT_USER_ID, ivy.id])
    assert world.store.get(state.channel_ref(dm)) is not None
    assert world.store.get(state.membership_ref(state.named_channel_id(state.GENERAL), ivy.id)) is not None


def test_a_person_joined_to_a_declared_workspace_is_a_member_there(opened: tuple[Opener, Path]) -> None:
    open_world, directory = opened
    world = open_world(_seed(WORKSPACES))
    world.extend(
        people=[IVY],
        directory=directory,
        scratch=_scratch,
        provider_seeds=[JOINED],
    )
    assert world.store.get(state.user_ref(state.user_id("ivy", "TALPHA"))) is not None


def test_a_hidden_email_for_a_person_already_seeded_rewrites_their_profile(opened: tuple[Opener, Path]) -> None:
    open_world, directory = opened
    world = open_world(_seed())
    hide = ProviderSeed(provider="slack", body=json.dumps({"without_email": ["sofia"]}))
    world.extend(provider_seeds=[hide], directory=directory, scratch=_scratch)
    assert _users(world)["sofia"].profile.email is None


def test_declaring_the_first_workspace_on_a_default_world_is_refused_naming_what_it_would_take_away(
    opened: tuple[Opener, Path],
) -> None:
    """A world that declares no workspace is the default one; declaring one replaces it, team id and all."""
    open_world, directory = opened
    world = open_world(_seed())
    head = world.store.head()
    with pytest.raises(WorldRefused, match="would no longer seed"):
        world.extend(provider_seeds=[ProviderSeed(provider="slack", body=json.dumps(WORKSPACES))],
                     directory=directory, scratch=_scratch)  # fmt: skip
    assert world.store.head() == head


def _seeded(padding: int, tmp_path: Path) -> list[wire.SlackMessage]:
    scenario = Seed.model_validate(_seed()).starting(START)
    store = SqliteStore(tmp_path / f"p{padding}.db", "p", RunClock(START))
    try:
        for n in range(padding):
            ref = EntityRef(provider="padding", kind=EntityKind.RECORD, external_id=str(n))
            store.apply(Change(entity=ref, operation=Operation.CREATE, actor=Actor.SCENARIO, body="{}"))
        build().seed(scenario, store)
        return state.SlackWorld(store).messages(state.named_channel_id("launch"))
    finally:
        store.close()


def test_a_seeded_ts_is_the_same_wherever_seeding_starts_and_keeps_the_seeded_order(tmp_path: Path) -> None:
    early, late = _seeded(0, tmp_path), _seeded(700, tmp_path)
    assert [m.ts for m in early] == [m.ts for m in late]
    by_ts = sorted(early, key=lambda m: (int(m.ts.split(".")[0]), int(m.ts.split(".")[1])))
    assert [m.text for m in by_ts] == ["first", "re", "second", "third", "second"]
    assert len({m.ts for m in early}) == 5
    second = int(START.timestamp()) - 3600
    assert all(m.ts.startswith(f"{second}.") for m in by_ts[2:])


def test_a_minted_ts_passes_over_one_a_seeded_message_holds(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "w.db", "w", RunClock(START))
    try:
        slack = state.SlackWorld(store)
        taken = slack.ts_at(1000)
        slack.write(state.message_ref(taken), state_message(taken), operation=Operation.CREATE,
                    actor=Actor.SCENARIO, parent="C1")  # fmt: skip
        ahead = f"1000.{store.head() + 1:06d}"
        slack.write(state.message_ref(ahead), state_message(ahead), operation=Operation.CREATE,
                    actor=Actor.SCENARIO, parent="C1")  # fmt: skip
        minted = slack.ts_at(1000)
        assert minted not in (taken, ahead) and store.get(state.message_ref(minted)) is None
    finally:
        store.close()


def state_message(ts: str) -> wire.SlackMessage:
    return wire.SlackMessage(ts=ts, user="U1", text="x", team=state.TEAM_ID)
