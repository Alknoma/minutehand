"""A person's change to a document lands at its moment as actor PERSON; it wakes the agent only when the agent
asked to be told of changes, and then the telling happens inside that wake."""

from __future__ import annotations

from datetime import datetime, timedelta

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.providers.google_drive.provider import GoogleDriveProvider
from minutehand.adapters.proxy.registry import Registry
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.domain.agent import AgentUnderTest, GoalByWake, Reported
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import DocumentChange, Renamed, Scenario, SeededDocument
from minutehand.domain.world import Actor, DocumentSnapshot, WorldEvent
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from minutehand.session import _services  # pyright: ignore[reportPrivateUsage]
from tests.orchestrator.rig import T0, Rig, scenario
from tests.orchestrator.world import CHAT, SECRET, RecordingClock

RENAMED_AT = T0 + timedelta(hours=30)


class Watched(GoogleDriveProvider):
    """The Drive provider with a watch the agent registered: the loop must wake it and notify inside the wake."""

    def __init__(self) -> None:
        self.told: list[tuple[datetime, int]] = []

    def watched(self, world: Store, clock: Clock) -> bool:
        return True

    async def notify(self, world: Store, clock: Clock) -> None:
        self.told.append((clock.now(), clock.wake()))


def with_a_document_change() -> Scenario:
    return scenario(
        ticket_fates=[],
        documents=[SeededDocument(provider="google_drive", title="Pricing Notes", text="40k?")],
        document_changes=[
            DocumentChange(
                provider="google_drive",
                document="Pricing Notes",
                by="sofia",
                after=RENAMED_AT - T0,
                action=Renamed(to="Pricing Notes (final)"),
            )
        ],
    )


async def played(rig: Rig, drive: GoogleDriveProvider) -> tuple[RunRecord, list[WorldEvent]]:
    scn = with_a_document_change()
    agent = rig.agent("ask_silent")
    clock = RecordingClock(scn.starts_at)
    store = rig.open("root", clock)
    record = await run_scenario(
        scenario=scn,
        agent=agent,
        reach=reach_for(agent, env=rig.env()),
        store=store,
        clock=clock,
        services=Services(
            providers=[rig.chat, rig.sched, drive],
            pushes={CHAT: rig.chat},
            tickets={CHAT: rig.chat},
            editors={CHAT: rig.chat},
            schedulers={rig.sched.manifest.key: rig.sched},
            changers={drive.manifest.key: drive},
        ),
        replier=ScriptedReplier(scn),
        mounts=rig.board,
        state_dir=rig.tmp / "state",
        signing={CHAT: SECRET},
        traffic=rig.board,
    )
    return record, [e for e in store.events() if e.actor is Actor.PERSON]


async def test_a_change_nobody_watches_lands_at_its_moment_and_wakes_no_one(rig: Rig) -> None:
    record, changed = await played(rig, GoogleDriveProvider())

    assert [(e.sim_time, e.after) for e in changed] == [
        (RENAMED_AT, DocumentSnapshot(title="Pricing Notes (final)", mime_type="application/vnd.google-apps.document"))
    ]
    assert len(record.wakes) == 1, "a change nobody asked to hear of is found on the next read, as a fate is"


async def test_a_watched_change_is_a_wake_and_the_agent_is_told_inside_it(rig: Rig) -> None:
    drive = Watched()
    record, changed = await played(rig, drive)

    assert [e.sim_time for e in changed] == [RENAMED_AT]
    assert [w.sim_time for w in record.wakes] == [T0, RENAMED_AT]
    assert drive.told == [(RENAMED_AT, 2)], "told once, at the change, inside the wake it makes"


def test_a_run_holds_the_drive_provider_to_the_changes_port_when_its_scenario_changes_a_document() -> None:
    agent = AgentUnderTest(
        name="a",
        goal=GoalByWake(),
        wakes=[Reported(wake_url="http://127.0.0.1:1/w", report_url="http://127.0.0.1:1/r")],
    )
    services = _services(with_a_document_change(), agent, Registry.installed())
    assert list(services.changers) == ["google_drive"]
