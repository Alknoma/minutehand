"""A person's change to a document lands at its moment as actor PERSON; it wakes the agent only when the agent
asked to be told of changes, and then the telling happens inside that wake."""

from __future__ import annotations

from datetime import datetime, timedelta

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.providers.google_workspace.provider import GoogleWorkspaceProvider
from minutehand.adapters.proxy.registry import Registry
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.domain.agent import AgentUnderTest, GoalByWake, Reported
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import DocumentHappening, Renamed, Scenario, SeededDocument
from minutehand.domain.world import Actor, DocumentSnapshot, WorldEvent
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ChangesDocuments
from minutehand.ports.store import Store
from minutehand.session import _services  # pyright: ignore[reportPrivateUsage]
from tests.orchestrator.rig import T0, Rig, scenario
from tests.orchestrator.world import CHAT, SECRET, RecordingClock

RENAMED_AT = T0 + timedelta(hours=30)


class Watched(GoogleWorkspaceProvider):
    """The Drive provider with a watch the agent registered: the loop must wake it and notify inside the wake."""

    def __init__(self) -> None:
        self.told: list[tuple[datetime, int]] = []

    def watched(self, world: Store, clock: Clock) -> bool:
        return True

    async def notify(self, world: Store, clock: Clock) -> None:
        self.told.append((clock.now(), clock.wake()))


def with_a_document_change() -> Scenario:
    return scenario(
        tom_finishes=False,
        documents=[SeededDocument(provider="google_workspace", title="Pricing Notes", text="40k?")],
        happenings=[
            DocumentHappening(
                document="Pricing Notes",
                person="sofia",
                after=RENAMED_AT - T0,
                action=Renamed(to="Pricing Notes (final)"),
            )
        ],
    )


async def played(rig: Rig, drive: GoogleWorkspaceProvider) -> tuple[RunRecord, list[WorldEvent]]:
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
            schedulers={rig.sched.manifest.key: rig.sched},
        ),
        replier=PeopleReplier(scn, None),
        mounts=rig.mounts,
        signing={CHAT: SECRET},
        traffic=rig.board,
    )
    return record, [e for e in store.events() if e.actor is Actor.PERSON]


async def test_a_change_nobody_watches_lands_at_its_moment_and_wakes_no_one(rig: Rig) -> None:
    record, changed = await played(rig, GoogleWorkspaceProvider())

    assert [(e.sim_time, e.after) for e in changed] == [
        (
            RENAMED_AT,
            DocumentSnapshot(
                title="Pricing Notes (final)",
                mime_type="application/vnd.google-apps.document",
                text="40k?",
                last_edited_by="sofia@example.com",
                last_edited_at=RENAMED_AT,
                owner="owner@example.com",
            ),
        )
    ]
    assert len(record.wakes) == 1, "a change nobody asked to hear of is found on the next read, as a fate is"


async def test_a_watched_change_is_a_wake_and_the_agent_is_told_inside_it(rig: Rig) -> None:
    drive = Watched()
    record, changed = await played(rig, drive)

    assert [e.sim_time for e in changed] == [RENAMED_AT]
    assert [w.sim_time for w in record.wakes] == [T0, RENAMED_AT]
    assert drive.told == [(RENAMED_AT, 2)], "told once, at the change, inside the wake it makes"


def test_a_run_names_the_drive_provider_whose_document_a_happening_changes() -> None:
    agent = AgentUnderTest(
        name="a",
        goal=GoalByWake(),
        wakes=[Reported(wake_url="http://127.0.0.1:1/w", report_url="http://127.0.0.1:1/r")],
    )
    services = _services(with_a_document_change(), agent, Registry.installed())
    assert [p.manifest.key for p in services.providers] == ["google_workspace"]
    assert isinstance(services.providers[0], ChangesDocuments)
