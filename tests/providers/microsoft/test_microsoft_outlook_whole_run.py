"""One whole run of an agent that chases a person by email and books them in Outlook, through the real proxy with
plain `httpx`: it emails Sofia from its own mailbox, she answers by email four hours later, it sends her an
invitation, and she accepts it four hours after that. Every step of hers lands at its moment, the agent's
subscription on its Inbox is notified of both, and the expectations and the scorecard read the run. The agent talks to
people only by email, so it declares no Teams inbound target; without a subscription her email wakes nobody."""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx

from minutehand.adapters.providers.microsoft.provider import build as microsoft
from minutehand.adapters.providers.microsoft.state import directory_of
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.orchestrator import Reach, Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.application.run_clock import RunClock
from minutehand.checks.runner import evaluate_run
from minutehand.domain.agent import AgentReport, AgentStatus, AgentUnderTest, Command, WakeReason, WakeRequest
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, InteractionSnapshot, MessageSnapshot
from tests.providers.microsoft.tenant import GRAPH, LOGIN, START, Webhook

AGENT = "assistant@roombooking.onmicrosoft.com"
TELL = "Room Ferris"

SCENARIO = Scenario.model_validate(
    {
        "name": "room_booking",
        "goal": "Book the vendor review with Sofia in a free room, and tell Owen where it is.",
        "owner": "owen",
        "starts_at": START,
        "deadline_after": "P3D",
        "people": [
            {"key": "owen", "name": "Owen Okafor", "email": "owen@example.com",
             "reply": {"kind": "scripted", "then": "silent", "replies": []}},
            {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com",
             "reply": {"kind": "scripted", "then": "silent", "delay": {"shortest": "PT4H", "longest": "PT4H"},
                       "replies": [{"to_ask": 1, "verbatim": f"{TELL} is free on Tuesday at 10."},
                                   {"to_ask": 2, "press": {"label": "Accept"}}]}},
        ],
        "expect": [
            {"kind": "person_asked", "person": "sofia", "mentions": ["room"], "at_most": 2},
            {"kind": "relayed", "said_by": "sofia", "to": "owen", "holding": [TELL]},
        ],
        "provider_seeds": [{"provider": "microsoft",
                            "body": {"mailbox": {"key": "agent", "name": "Assistant", "local": "assistant"}}}],
    }
)  # fmt: skip


@dataclass
class Booker:
    """The agent, in this process: every call it makes goes through the proxy, as a service's would."""

    proxy: str
    trust: ssl.SSLContext
    notify_url: str
    subscribe: bool = True
    woken: list[WakeRequest] = field(default_factory=list)
    event: str | None = None
    report: AgentReport = field(default_factory=lambda: AgentReport(status=AgentStatus.IDLE))

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(proxy=self.proxy, verify=self.trust, trust_env=False)

    async def _auth(self, http: httpx.AsyncClient) -> dict[str, str]:
        d = directory_of(SCENARIO)
        signed = await http.post(
            f"{LOGIN}/{d.tenant_id}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": d.bot_app_id, "client_secret": d.bot_app_secret,
                  "scope": "https://graph.microsoft.com/.default"},
        )  # fmt: skip
        return {"Authorization": f"Bearer {signed.json()['access_token']}"}

    async def wake(self, request: WakeRequest) -> None:
        self.woken.append(request)
        async with self._http() as http:
            auth = await self._auth(http)
            if request.reason is WakeReason.START:
                await self._start(http, auth, request)
            elif self.event is None:
                await self._book(http, auth)
            else:
                await self._confirm(http, auth)

    async def settled(self) -> AgentReport:
        return self.report

    async def _start(self, http: httpx.AsyncClient, auth: dict[str, str], request: WakeRequest) -> None:
        later = (request.now + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        if self.subscribe:
            await self._watch(http, auth, later)
        sent = await http.post(
            f"{GRAPH}/users/{AGENT}/sendMail",
            json={"message": {"subject": "Vendor review", "toRecipients": [{"emailAddress": {"address": "sofia@example.com"}}],
                              "body": {"contentType": "Text", "content": "Which room is free on Tuesday for the vendor review?"}}},
            headers=auth,
        )  # fmt: skip
        assert sent.status_code == 202, sent.text

    async def _watch(self, http: httpx.AsyncClient, auth: dict[str, str], later: str) -> None:
        watched = await http.post(
            f"{GRAPH}/subscriptions",
            json={"changeType": "created", "notificationUrl": self.notify_url, "clientState": "inbox",
                  "resource": f"/users/{AGENT}/mailFolders('Inbox')/messages", "expirationDateTime": later},
            headers=auth,
        )  # fmt: skip
        assert watched.status_code == 201, watched.text

    async def _book(self, http: httpx.AsyncClient, auth: dict[str, str]) -> None:
        unread = await http.get(
            f"{GRAPH}/users/{AGENT}/mailFolders/inbox/messages",
            params={"$filter": "isRead eq false and from/emailAddress/address eq 'sofia@example.com'"},
            headers={**auth, "Prefer": 'outlook.body-content-type="text"'},
        )
        [answer] = unread.json()["value"]
        await http.patch(f"{GRAPH}/users/{AGENT}/messages/{answer['id']}", json={"isRead": True}, headers=auth)
        room = answer["body"]["content"].split(" is free")[0]
        made = await http.post(
            f"{GRAPH}/users/{AGENT}/events",
            json={"subject": "Vendor review", "location": {"displayName": room},
                  "start": {"dateTime": "2026-09-15T10:00:00", "timeZone": "UTC"},
                  "end": {"dateTime": "2026-09-15T10:30:00", "timeZone": "UTC"},
                  "attendees": [{"emailAddress": {"address": "sofia@example.com"}, "type": "required"}]},
            headers=auth,
        )  # fmt: skip
        assert made.status_code == 201, made.text
        self.event = made.json()["id"]
        await self._tell_owen(http, auth, f"Sofia says {room} is free; I have invited her for Tuesday at 10.")

    async def _confirm(self, http: httpx.AsyncClient, auth: dict[str, str]) -> None:
        event = (await http.get(f"{GRAPH}/users/{AGENT}/events/{self.event}", headers=auth)).json()
        if event["attendees"][0]["status"]["response"] != "accepted":
            return
        await self._tell_owen(
            http, auth, f"Sofia accepted: the vendor review is in {event['location']['displayName']}."
        )
        self.report = AgentReport(status=AgentStatus.DONE)

    async def _tell_owen(self, http: httpx.AsyncClient, auth: dict[str, str], text: str) -> None:
        sent = await http.post(
            f"{GRAPH}/users/{AGENT}/sendMail",
            json={"message": {"subject": "Vendor review", "body": {"contentType": "Text", "content": text},
                              "toRecipients": [{"emailAddress": {"address": "owen@example.com"}}]}},
            headers=auth,
        )  # fmt: skip
        assert sent.status_code == 202, sent.text


async def _run(tmp_path: Path, webhook: Webhook, *, subscribe: bool) -> tuple[RunRecord, Booker, SqliteStore]:
    """The run, with an agent that declares no inbound target: nothing of Sofia's is pushed to a bot."""
    clock = RunClock(SCENARIO.starts_at)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    provider = microsoft()
    async with Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca") as proxy:
        booker = Booker(
            proxy=proxy.url,
            trust=ssl.create_default_context(cafile=str(proxy.ca_bundle)),
            notify_url=webhook.url,
            subscribe=subscribe,
        )
        record = await run_scenario(
            scenario=SCENARIO,
            agent=AgentUnderTest(name="booker", wakes=[Command(argv=["in-process"])], inbound=[]),
            reach=Reach(main=booker),
            store=store,
            clock=clock,
            services=Services(providers=[provider], pushes={"microsoft": provider}),
            replier=PeopleReplier(SCENARIO, None),
            mounts=proxy,
            signing={},
        )
    return record, booker, store


async def test_an_agent_emails_a_person_hears_back_by_email_invites_her_and_she_accepts(
    tmp_path: Path, webhook: Webhook
) -> None:
    record, booker, store = await _run(tmp_path, webhook, subscribe=True)

    assert record.stop is StopReason.AGENT_DONE
    assert [(w.now - START, w.reason) for w in booker.woken] == [
        (timedelta(0), WakeReason.START),
        (timedelta(hours=4), WakeReason.PERSON_REPLIED),
        (timedelta(hours=8), WakeReason.PERSON_REPLIED),
    ], "her email and her accept each wake the agent at their moment"
    assert [w.sim_time for w in record.wakes] == [w.now for w in booker.woken]
    events = store.events()
    hers = [e for e in events if e.actor is Actor.PERSON and e.after is not None]
    said = [(e.sim_time - START, e.after.text) for e in hers if isinstance(e.after, MessageSnapshot)]
    assert said == [
        (timedelta(hours=4), f"RE: Vendor review\n\n{TELL} is free on Tuesday at 10."),
        (timedelta(hours=8), "Accepted: Vendor review"),
    ]
    pressed = [e for e in hers if isinstance(e.after, InteractionSnapshot)]
    assert [(e.sim_time - START, e.after.label) for e in pressed if isinstance(e.after, InteractionSnapshot)] == [
        (timedelta(hours=8), "Accept")
    ]
    assert [len(n["value"]) for n in webhook.notifications] == [1, 1], "her email and her answer reach the Inbox"
    asked = [e for e in events if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)]
    assert [
        (e.after.recipient_emails, e.after.actions != []) for e in asked if isinstance(e.after, MessageSnapshot)
    ] == [
        (["sofia@example.com"], False),
        (["sofia@example.com"], True),
        (["owen@example.com"], False),
        (["owen@example.com"], False),
    ]
    assert {c.exchange.host for c in store.calls()} == {"login.microsoftonline.com", "graph.microsoft.com"}

    result = evaluate_run(SCENARIO, events, record.wakes, store.replies(), stop=record.stop)
    assert [f.message for f in result.findings if f.kind is FindingKind.FAIL] == []
    assert (result.effectiveness.expectations_met, result.effectiveness.expectations_total) == (2, 2)


async def test_an_email_reply_with_no_subscription_on_the_mailbox_lands_and_wakes_nobody(
    tmp_path: Path, webhook: Webhook
) -> None:
    record, booker, store = await _run(tmp_path, webhook, subscribe=False)

    assert [w.reason for w in booker.woken] == [WakeReason.START], (
        "nothing told the agent: it finds her email by polling"
    )
    landed = [e for e in store.events() if e.actor is Actor.PERSON and isinstance(e.after, MessageSnapshot)]
    assert [(e.sim_time - START, e.after.recipient_emails) for e in landed if isinstance(e.after, MessageSnapshot)] == [
        (timedelta(hours=4), [AGENT])
    ]
    assert webhook.notifications == [] and record.stop is not StopReason.AGENT_DONE
