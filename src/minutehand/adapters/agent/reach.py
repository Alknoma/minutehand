"""The drivers an `AgentUnderTest` declares, assembled into the run loop's `Reach`."""

from __future__ import annotations

from collections.abc import Mapping

from minutehand.adapters.agent.command import CommandDriver
from minutehand.adapters.agent.polled import PolledDriver
from minutehand.adapters.agent.reported import ReportedDriver
from minutehand.application.orchestrator import Reach
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import AgentUnderTest, Booked, Command, GoalByMessage, Polled, Reported
from minutehand.ports.agent import AgentDriver


def reach_for(agent: AgentUnderTest, *, env: Mapping[str, str] | None = None) -> Reach:
    """`Reported` or `Command` answers every wake; `Polled` takes the ticks; `Booked` needs no driver.

    `env` is added to a `Command`'s environment. An agent with two of the first kind, or two `Polled`, is
    refused: which of them a wake goes to would be a guess. An agent that takes its goal by message may have
    no driver at all; one that takes it in the START wake must have one.
    """
    main: list[AgentDriver] = []
    polled: list[Polled] = []
    for source in agent.wakes:
        if isinstance(source, Reported):
            main.append(ReportedDriver(source.wake_url, source.report_url))
        elif isinstance(source, Command):
            main.append(CommandDriver(source.argv, env=env))
        elif isinstance(source, Polled):
            polled.append(source)
        else:
            assert isinstance(source, Booked)
    if len(main) > 1:
        raise RunRefused(f"agent {agent.name} declares {len(main)} Reported/Command wake sources; it may have one")
    if len(polled) > 1:
        raise RunRefused(f"agent {agent.name} declares {len(polled)} Polled wake sources; it may have one")
    if not main and not polled and not isinstance(agent.goal, GoalByMessage):
        raise RunRefused(
            f"agent {agent.name} declares only Booked wakes, so nothing can hand it the START wake and its goal; "
            "add a Reported, Command or Polled source, or take the goal by message"
        )
    tick = polled[0] if polled else None
    return Reach(
        main=main[0] if main else None,
        ticks=PolledDriver(tick.wake_url) if tick else None,
        every=tick.every if tick else None,
    )
