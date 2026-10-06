"""A tenant with mail and calendars: the agent's own mailbox, emails already sent, events already planned, and a
user's sign-in as the services do it (authorization code with `login_hint`), for `/me`."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

from minutehand.domain.people import PersonReply, Press
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, EntityKind, EntityRef, MessageSnapshot, WorldEvent
from tests.providers.microsoft.tenant import LOGIN, START, Tenant

REDIRECT = "https://assistant.example.com/oauth/microsoft/callback"
AGENT = "assistant@outlookcase.onmicrosoft.com"

OUTLOOK = Scenario.model_validate(
    {
        "name": "outlook_case",
        "goal": "The vendor review is booked with Sofia.",
        "owner": "owen",
        "starts_at": START,
        "people": [
            {"key": "owen", "name": "Owen Okafor", "email": "owen@example.com"},
            {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com"},
            {"key": "dania", "name": "Dania Kovac", "email": "dania@example.com"},
        ],
        "provider_seeds": [
            {
                "provider": "microsoft",
                "body": {
                    "mailbox": {"key": "agent", "name": "Assistant", "local": "assistant"},
                    "emails": [
                        {"key": "kickoff", "by": "owen", "to": ["agent"], "subject": "Vendor review", "ago": "PT3H",
                         "text": "Please book the vendor review with Sofia."},
                        {"by": "dania", "to": ["agent"], "cc": ["owen"], "subject": "Lunch", "ago": "PT2H",
                         "read": True, "text": "Sandwiches at noon."},
                        {"by": "owen", "to": ["agent"], "subject": "Re: Vendor review", "ago": "PT1H",
                         "in_reply_to": "kickoff", "text": "Tuesday works best."},
                    ],
                    "events": [
                        {"organizer": "sofia", "subject": "Budget sync", "at": "PT2H", "lasts": "PT1H",
                         "attendees": [{"person": "dania", "response": "accepted"},
                                       {"person": "owen", "response": "declined"}]},
                        {"organizer": "dania", "subject": "Planning", "at": "P1D", "lasts": "PT30M",
                         "attendees": [{"person": "sofia"}]},
                    ],
                },
            }
        ],
    }
)  # fmt: skip


async def signed_in(http: httpx.AsyncClient, tenant: Tenant, login_hint: str) -> str:
    """A user's Graph token, by authorization code, as a service signs a user in."""
    d = tenant.directory
    authorized = await http.get(
        f"{LOGIN}/common/oauth2/v2.0/authorize",
        params={
            "client_id": d.bot_app_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "scope": "openid offline_access Mail.ReadWrite Mail.Send Calendars.ReadWrite",
            "login_hint": login_hint,
        },
    )
    assert authorized.status_code == 302, authorized.text
    code = parse_qs(urlsplit(authorized.headers["location"]).query)["code"][0]
    exchanged = await http.post(
        f"{LOGIN}/common/oauth2/v2.0/token",
        data={
            "grant_type": "authorization_code",
            "client_id": d.bot_app_id,
            "client_secret": d.bot_app_secret,
            "code": code,
            "redirect_uri": REDIRECT,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    found = exchanged.json()["access_token"]
    assert isinstance(found, str)
    return found


def sent_by_agent(tenant: Tenant) -> list[WorldEvent]:
    """The messages the agent sent, as the run reads them."""
    return [
        e
        for e in tenant.store.events()
        if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot) and e.entity.kind is EntityKind.MESSAGE
    ]


def reply(person: str, to: WorldEvent, text: str = "", *, press: str | None = None, after: timedelta) -> PersonReply:
    ref = EntityRef(provider="microsoft", kind=EntityKind.MESSAGE, external_id=to.entity.external_id)
    pressed = (
        Press(
            action_id={"Accept": "accept", "Tentative": "tentativelyAccept", "Decline": "decline"}[press], label=press
        )
        if press is not None
        else None
    )
    return PersonReply(
        person=person, in_reply_to=ref, text=text or (press or ""), at=to.sim_time + after, press=pressed
    )
