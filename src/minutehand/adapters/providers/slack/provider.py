"""The Slack provider: the Web API, the seeded workspace, pushed events, and people using buttons and modals."""

from __future__ import annotations

from minutehand.adapters.providers.slack import inbound, interactive, socket_mode, state, wire
from minutehand.adapters.providers.slack.app import SlackApi, build_app
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.pushing import Listener
from minutehand.adapters.providers.slack.seed import SlackSeed, seed, write_faults
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.application.conversations import PushedConversations
from minutehand.domain.errors import Rendered
from minutehand.domain.people import (
    Header,
    InboundCredential,
    InboundCredentialAsk,
    InboundTarget,
    PersonMessage,
    PersonReply,
)
from minutehand.domain.provider import Manifest, PersonChange, fault_fragment
from minutehand.domain.scenario import MessagingHappening, Person, PersonCommands, Scenario
from minutehand.domain.world import Actor, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, Wakes
from minutehand.ports.store import Store


class SlackProvider:
    manifest: Manifest = MANIFEST
    seed_model = SlackSeed

    def __init__(self) -> None:
        self._listener: Listener | None = None
        self._wakes: Wakes | None = None

    def bind(self, wakes: Wakes) -> None:
        """`BooksWakes`: where a scheduled message books the moment it is posted."""
        self._wakes = wakes

    async def deliver_booking(self, ref: str, world: Store, clock: Clock) -> None:
        """A scheduled message's moment has come: Slack posts it, as the app, at the run's clock."""
        SlackApi(world, clock).post_scheduled(ref)

    async def advance_booking(self, ref: str, world: Store, clock: Clock) -> None:
        """A scheduled message is posted once: nothing follows it, delivered or dropped."""
        del ref, world, clock

    def listen(self, target: InboundTarget | None, secret: str | None) -> None:
        """`ListensForAgent`: where the agent takes its events, so the ones its own calls set off reach it."""
        self._listener = None if target is None or secret is None else Listener(target, secret)

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock, lambda: self._listener, lambda: self._wakes)

    def sockets(self, world: Store, clock: Clock) -> ASGIApp:
        """Socket Mode's connections, at the URL `apps.connections.open` hands out (`socket_mode`)."""
        return socket_mode.socket_app(world, clock)

    def error(self, status: int, code: str, message: str) -> Rendered:
        return wire.error_answer(status, code, message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    async def deliver(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        await inbound.deliver(reply, target, world, clock, secret=secret)

    async def say(
        self, message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        await inbound.say(message, target, world, clock, secret=secret)

    async def happen(
        self, happening: MessagingHappening, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        if isinstance(happening, PersonCommands):
            await interactive.command(happening, target, world, clock, secret=secret)
        else:
            await inbound.happen(happening, target, world, clock, secret=secret)

    async def press(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        await interactive.press(reply, target, world, clock, secret=secret)

    def talking(self, target: InboundTarget | None, secret: str | None) -> PushedConversations:
        """`TalksToAgent`: people's answers to the agent's messages, pushed to `target` signed with `secret`."""
        return PushedConversations(MANIFEST.key, self, target, secret)

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`SlackSeed.faults`, on a world already open."""
        _declare(faults, world, clock)

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """An admin deactivates or reactivates the person's account in every workspace they are in: a deactivated
        user stays listed with `deleted: true`, and cannot be DMed (`user_disabled`), as Slack has it."""
        every = SlackWorld(world)
        changed = False
        for workspace in every.workspaces():
            slack = every.as_team(workspace)
            user = slack.user(state.user_id(person.key, workspace.id))
            if user is None:
                continue
            deleted = change is PersonChange.DEACTIVATED
            if user.deleted == deleted:
                raise ValueError(f"{person.key} is already {change.value} in {workspace.id}")
            slack.write(
                state.user_ref(user.id),
                user.model_copy(update={"deleted": deleted}),
                operation=Operation.UPDATE,
                actor=Actor.SCENARIO,
                parent=workspace.id,
                after=RecordSnapshot(resource="users", text=f"{user.real_name} {change.value}"),
            )
            changed = True
        if not changed:
            raise ValueError(f"{person.key} has no Slack account in this world")
        del clock

    def credential(self, asked: InboundCredentialAsk, world: Store, clock: Clock, *, secret: str) -> InboundCredential:
        """Slack's request signature for `asked.body` at `asked.timestamp`, under the world's signing secret, as
        Slack signs every request it sends an app."""
        if asked.timestamp is None:
            raise ValueError("a Slack signature covers the request's timestamp; give `timestamp`")
        if not secret:
            raise ValueError("this world declares no inbound target for slack, so there is no signing secret")
        stamp = str(asked.timestamp)
        return InboundCredential(
            headers=[
                Header(name="X-Slack-Request-Timestamp", value=stamp),
                Header(name="X-Slack-Signature", value=inbound.sign(secret, stamp, asked.body.encode())),
            ]
        )


def _declare(faults: str, world: Store, clock: Clock) -> None:
    write_faults(
        SlackWorld(world), fault_fragment(SlackSeed, faults, frozenset({"faults"})).faults, clock.now(), declared=True
    )


def build() -> SlackProvider:
    """A `Provider` that also `PushesEvents`, `PushesPresses`, `ServesSockets`, `DeclaresFaults`,
    `ChangesPeople` and `MintsInboundCredentials`; the tests hold it to each."""
    return SlackProvider()
