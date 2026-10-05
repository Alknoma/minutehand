"""The Slack provider: the Web API, the seeded workspace, pushed events, and people using buttons and modals."""

from __future__ import annotations

from minutehand.adapters.answering import guarded
from minutehand.adapters.providers.slack import inbound, interactive, state, wire
from minutehand.adapters.providers.slack.app import build_app
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.seed import SlackSeed, seed, write_faults
from minutehand.adapters.providers.slack.state import SlackWorld
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
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class SlackProvider:
    manifest: Manifest = MANIFEST
    seed_model = SlackSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return guarded(build_app(world, clock), self, provider=self.manifest.key)

    def error(self, status: int, code: str, message: str) -> Rendered:
        """Minutehand's own error as the Web API words one (`wire.error_answer`): `slack_sdk` raises `SlackApiError`
        for it as it does for Slack's refusals."""
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
            member = slack.find_member(person.key)
            user = slack.user(member) if member is not None else None
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
    """A `Provider` that also `PushesEvents`, `PushesInteractions`, `DeclaresFaults`, `ChangesPeople` and
    `MintsInboundCredentials`; the tests hold it to each."""
    return SlackProvider()
