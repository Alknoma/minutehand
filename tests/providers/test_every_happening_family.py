"""One scenario file, one run, every family of happening: an Asana and a YouTrack ticket, a Drive document and a
Slack message, each done by a person at its moment through its own provider's port, with nobody asking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from minutehand.adapters.providers.asana.provider import build as asana
from minutehand.adapters.providers.google_drive.provider import build as google_drive
from minutehand.adapters.providers.slack.provider import build as slack
from minutehand.adapters.providers.youtrack.provider import build as youtrack
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.files import load_scenario
from minutehand.application.orchestrator import Reach, Services, run_scenario
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentUnderTest, GoalByMessage
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, DocumentSnapshot, EntityKind, Operation, TicketSnapshot
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, agent

__all__ = ["agent"]

SCENARIO = Path(__file__).parents[1] / "data" / "every_happening_family" / "scenario.yaml"
START = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


async def test_one_run_plays_a_ticket_happening_on_asana_and_youtrack_a_document_one_on_drive_and_a_slack_post(
    agent: AgentEndpoint, tmp_path: Path
) -> None:
    scenario = load_scenario(SCENARIO).starting(START)
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    providers = [asana(), google_drive(), slack(), youtrack()]
    chat = providers[2]
    async with Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca") as proxy:
        record = await run_scenario(
            scenario=scenario,
            agent=AgentUnderTest(name="listener", goal=GoalByMessage(provider="slack"), inbound=[agent.target()]),
            reach=Reach(main=None),
            store=store,
            clock=clock,
            services=Services(providers=providers, pushes={"slack": chat}),
            replier=ScriptedReplier(scenario),
            mounts=proxy,
            signing={"slack": SECRET},
        )

    assert record.stop is StopReason.NOTHING_PENDING
    by_person = [e for e in store.events() if e.actor is Actor.PERSON]
    acted = [(e.sim_time - START, e.entity.provider, e.entity.kind, e.operation) for e in by_person]
    assert acted == [
        (timedelta(0), "slack", EntityKind.MESSAGE, Operation.CREATE),
        (timedelta(days=1), "asana", EntityKind.TICKET, Operation.UPDATE),
        (timedelta(days=2), "youtrack", EntityKind.COMMENT, Operation.CREATE),
        (timedelta(days=3), "google_drive", EntityKind.DOCUMENT, Operation.UPDATE),
        (timedelta(days=4), "slack", EntityKind.MESSAGE, Operation.CREATE),
    ]
    assert isinstance(by_person[1].after, TicketSnapshot) and by_person[1].after.state is TicketState.DONE
    assert isinstance(by_person[3].after, DocumentSnapshot) and by_person[3].after.title == "Launch plan (final)"
    texts = [r.json["event"]["text"] for r in agent.received]
    assert texts[-1] == "All three are done on my side", "the Slack post is pushed to the agent"
    assert [w.sim_time - START for w in record.wakes] == [timedelta(0), timedelta(days=4)], (
        "the tickets and the document wake nobody; the Slack post does"
    )
