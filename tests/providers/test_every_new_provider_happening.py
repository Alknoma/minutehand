"""One scenario file, one run, across Jira, Notion and Microsoft: a ticket happening on Jira, a document happening
on Notion and one on a Microsoft file, a Teams post, and a Teams card press, each at its recorded moment through
its own provider's port. The bot sends the card itself, through the proxy, when the goal reaches it."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters.providers.jira.provider import build as jira
from minutehand.adapters.providers.microsoft.provider import build as microsoft
from minutehand.adapters.providers.microsoft.state import MicrosoftWorld, directory_of
from minutehand.adapters.providers.notion.provider import build as notion
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.files import load_scenario
from minutehand.application.orchestrator import Reach, Services, run_scenario
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentUnderTest, GoalByMessage
from minutehand.domain.people import InboundTarget
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Scenario, TicketState
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    EntityKind,
    InteractionSnapshot,
    MessageSnapshot,
    TicketSnapshot,
)

SCENARIO = Path(__file__).parents[1] / "data" / "every_new_provider" / "scenario.yaml"
START = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
CARD: dict[str, Any] = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [{"type": "TextBlock", "text": "Is the venue booked?"}],
    "actions": [{"type": "Action.Execute", "title": "Approve", "verb": "approve", "data": {"venue": "booked"}}],
}


@dataclass
class Bot:
    """The agent's Teams endpoint: on the goal, it sends Nadia a card through the proxy; it keeps every activity."""

    scenario: Scenario
    store: SqliteStore | None = None
    proxy: str = ""
    trust: ssl.SSLContext | None = None
    url: str = ""
    activities: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: Request) -> Response:
        activity = json.loads(await request.body())
        self.activities.append(activity)
        if activity["type"] == "invoke":
            return JSONResponse(
                {"statusCode": 200, "type": "application/vnd.microsoft.activity.message", "value": "Thanks."}
            )
        if activity["type"] == "message" and activity["text"] == self.scenario.goal:
            await self._send_card(activity["serviceUrl"])
        return Response(status_code=200)

    async def _send_card(self, service_url: str) -> None:
        directory = directory_of(self.scenario)
        assert self.store is not None
        world = MicrosoftWorld(self.store)
        nadia = world.person("nadia")
        assert nadia is not None
        chat = world.personal_with(nadia.user.id, directory.tenant_id)
        assert chat is not None
        async with httpx.AsyncClient(proxy=self.proxy, verify=self.trust or False, trust_env=False) as http:
            signed = await http.post(
                f"https://login.microsoftonline.com/{directory.tenant_id}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": directory.bot_app_id,
                      "client_secret": directory.bot_app_secret, "scope": "https://api.botframework.com/.default"},
            )  # fmt: skip
            sent = await http.post(
                f"{service_url}v3/conversations/{chat.id}/activities",
                json={"type": "message", "attachments": [
                    {"contentType": "application/vnd.microsoft.card.adaptive", "content": CARD}]},
                headers={"Authorization": f"Bearer {signed.json()['access_token']}"},
            )  # fmt: skip
            assert sent.status_code == 201, sent.text

    def target(self) -> InboundTarget:
        return InboundTarget(provider="microsoft", url=self.url)


@pytest.fixture
async def bot() -> AsyncIterator[Bot]:
    handle = Bot(scenario=load_scenario(SCENARIO).starting(START))
    app = Starlette(routes=[Route("/api/messages", handle.handle, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    handle.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api/messages"
    try:
        yield handle
    finally:
        server.should_exit = True
        await serving


async def test_one_run_lands_a_jira_ticket_a_notion_page_a_microsoft_file_a_teams_post_and_a_card_press(
    bot: Bot, tmp_path: Path
) -> None:
    scenario = bot.scenario
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    bot.store = store
    teams = microsoft()
    async with Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca") as proxy:
        bot.proxy, bot.trust = proxy.url, ssl.create_default_context(cafile=str(proxy.ca_bundle))
        record = await run_scenario(
            scenario=scenario,
            agent=AgentUnderTest(name="bot", goal=GoalByMessage(provider="microsoft"), inbound=[bot.target()]),
            reach=Reach(main=None),
            store=store,
            clock=clock,
            services=Services(providers=[jira(), notion(), teams], pushes={"microsoft": teams}),
            replier=ScriptedReplier(scenario),
            mounts=proxy,
            signing={"microsoft": "the Bot Framework signs with its key, not a secret"},
        )

    assert record.stop is StopReason.NOTHING_PENDING
    by_person = [e for e in store.events() if e.actor is Actor.PERSON and e.after is not None]
    moments = {(e.entity.provider, e.after.kind): e.sim_time - START for e in by_person if e.after is not None}
    assert moments[("jira", "ticket")] == timedelta(days=1)
    assert moments[("notion", "document")] == timedelta(days=2)
    assert moments[("microsoft", "document")] == timedelta(days=3)
    assert moments[("microsoft", "interaction")] == timedelta(hours=2), "she presses two hours after the card"
    posts = [e for e in by_person if e.entity.provider == "microsoft" and isinstance(e.after, MessageSnapshot)]
    assert [(p.sim_time - START, p.after.text) for p in posts if isinstance(p.after, MessageSnapshot)][-1] == (
        timedelta(days=4),
        "All three are done on my side",
    )

    ticket = next(e.after for e in by_person if isinstance(e.after, TicketSnapshot))
    assert ticket.state is TicketState.DONE and ticket.assignee_email == "nadia@example.com"
    page = next(e.after for e in by_person if e.entity.provider == "notion" and isinstance(e.after, DocumentSnapshot))
    assert page.text == "Venue first.\nVenue booked." and page.last_edited_by == "nadia@example.com"
    file = next(
        e.after for e in by_person if e.entity.provider == "microsoft" and isinstance(e.after, DocumentSnapshot)
    )
    assert file.title == "Launch plan (final).docx" and file.last_edited_by == "nadia@example.com"
    pressed = next(e.after for e in by_person if isinstance(e.after, InteractionSnapshot))
    assert (pressed.person, pressed.label) == ("nadia", "Approve")

    kinds = [a["type"] for a in bot.activities]
    assert "invoke" in kinds, "the press reached the bot as Teams sends an Action.Execute"
    assert bot.activities[-1]["text"] == "All three are done on my side"
    assert [w.sim_time - START for w in record.wakes] == [timedelta(0), timedelta(hours=2), timedelta(days=4)], (
        "the goal, the press and the Teams post wake the agent; the ticket, the page and the file wake nobody"
    )
    assert not any(e.entity.kind is EntityKind.RECORD and e.actor is Actor.AGENT for e in by_person)
