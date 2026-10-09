"""A provider whose own API calls set off what the agent hears of (`ListensForAgent`) is told the agent's inbound target
for it and its signing secret as the run starts, whether or not it also pushes people's messages (`PushesEvents`):
GitHub pushes no chat, and is told where its webhooks go all the same."""

from __future__ import annotations

from pathlib import Path

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.providers.github.provider import GitHubProvider
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.domain.common import GeneratedSecret
from minutehand.domain.people import InboundTarget
from minutehand.ports.provider import ListensForAgent, PushesEvents
from tests.orchestrator.rig import Rig, scenario
from tests.orchestrator.world import CHAT, SECRET, RecordingClock
from tests.support.people import people_model

HOOKS = InboundTarget(provider="github", url="http://127.0.0.1:9/hooks", secret=GeneratedSecret(env="GH_HOOKS"))


class Told(GitHubProvider):
    """The GitHub provider, keeping what it is told."""

    def __init__(self) -> None:
        super().__init__()
        self.told: list[tuple[InboundTarget | None, str | None]] = []

    def listen(self, target: InboundTarget | None, secret: str | None) -> None:
        self.told.append((target, secret))
        super().listen(target, secret)


async def test_a_provider_that_listens_and_pushes_no_events_is_told_the_agents_target_as_the_run_starts(
    rig: Rig, tmp_path: Path
) -> None:
    github = Told()
    assert isinstance(github, ListensForAgent) and not isinstance(github, PushesEvents)
    agent = rig.agent("keep_waking")
    agent = agent.model_copy(update={"inbound": [*agent.inbound, HOOKS]})
    scn = scenario(tom_finishes=False, max_wakes=1)
    clock = RecordingClock(scn.starts_at)
    store = rig.open("root", clock)
    await run_scenario(
        scenario=scn,
        agent=agent,
        reach=reach_for(agent, env=rig.env()),
        store=store,
        clock=clock,
        services=Services(
            providers=[rig.chat, rig.sched, github],
            pushes={CHAT: rig.chat},
            schedulers={rig.sched.manifest.key: rig.sched},
        ),
        replier=PeopleReplier(scn, people_model()),
        mounts=rig.mounts,
        signing={CHAT: SECRET, "github": "the-hooks-secret"},
        traffic=rig.board,
    )
    assert github.told == [(HOOKS, "the-hooks-secret")]
