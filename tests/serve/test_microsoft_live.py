"""A Microsoft tenant changed while its world is open, through the control API, and read through the clients a
service runs: a send answered without its id, a user disabled, removed and enabled again, every Teams shape a
person pushes verified by the bot's own check, a minted Bot Framework token, a person out of office as Graph shows
it, and access the agent gives to a file."""

from __future__ import annotations

import json
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld, Inbound
from minutehand.adapters.providers.microsoft.state import Directory, directory_of
from minutehand.domain.people import InboundCredentialAsk, Press
from minutehand.domain.scenario import PersonAddsAgent, PersonPosts, Seed
from minutehand.domain.world import Actor, DocumentSnapshot, EntityKind, GrantSnapshot, MessageSnapshot
from minutehand.testing.client import Refused, Unsupported
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
CONNECTOR = "https://smba.trafficmanager.net/teams/"
BOT_LOGIN = "login.botframework.com"
BOT_METADATA = f"https://{BOT_LOGIN}/v1/.well-known/openidconfiguration"
BOT_ISSUER = "https://api.botframework.com"
STARTS = "2026-09-01T09:00:00Z"


def _seed(name: str) -> Seed:
    return Seed.model_validate(
        {
            "name": name,
            "starts_at": STARTS,
            "people": [
                {"key": "owen", "name": "Owen Owner", "email": f"owen@{name}.example.com", "reply": {"kind": "silent"},
                 "absences": [{"starts_after": "P5D", "lasts": "P1D", "reason": "a conference"}]},
                {"key": "sofia", "name": "Sofia Romano", "email": f"sofia@{name}.example.com",
                 "reply": {"kind": "silent"}, "absences": [{"lasts": "P2D", "reason": "parental leave"}]},
                {"key": "dania", "name": "Dania Diaz", "email": f"dania@{name}.example.com",
                 "reply": {"kind": "silent"},
                 "absences": [{"trigger": "on_first_ask", "lasts": "P1D", "reason": "off-site training"}]},
                {"key": "ines", "name": "Ines Ito", "email": f"ines@{name}.example.com", "reply": {"kind": "silent"}},
            ],
            "provider_seeds": [{"provider": "microsoft", "body": {"not_installed_for": ["ines"]}}],
        }
    )  # fmt: skip


def _directory(seed: Seed) -> Directory:
    return directory_of(seed.starting(datetime(2026, 9, 1, 9, tzinfo=UTC)))


def _claims(directory: Directory, hosts: list[str]) -> Claims:
    return Claims(
        keys=[directory.tenant_id, directory.tenant_domain, directory.sharepoint_host.split(".")[0]], hosts=hosts
    )


def _http(served: Served) -> httpx.Client:
    trust = ssl.create_default_context(cafile=served.bundle)
    return httpx.Client(proxy=served.proxy, verify=trust, trust_env=False, timeout=30, follow_redirects=False)


def _app_token(http: httpx.Client, directory: Directory, scope: str) -> dict[str, str]:
    answered = http.post(
        f"{LOGIN}/{directory.tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": directory.bot_app_id,
            "client_secret": directory.bot_app_secret,
            "scope": scope,
        },
    )
    assert answered.status_code == 200, answered.text
    return {"Authorization": f"Bearer {answered.json()['access_token']}"}


def _user_sign_in(http: httpx.Client, directory: Directory, email: str) -> httpx.Response:
    """A user signing in to the bot's app by the authorization code flow: the code, then the token."""
    authorized = http.get(
        f"{LOGIN}/{directory.tenant_id}/oauth2/v2.0/authorize",
        params={"client_id": directory.bot_app_id, "redirect_uri": "https://app.example.com/cb",
                "login_hint": email, "scope": "User.Read", "response_type": "code"},
    )  # fmt: skip
    if authorized.status_code != 302:
        return authorized
    code = parse_qs(urlsplit(authorized.headers["location"]).query)["code"][0]
    return http.post(
        f"{LOGIN}/{directory.tenant_id}/oauth2/v2.0/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": directory.bot_app_id,
              "client_secret": directory.bot_app_secret, "redirect_uri": "https://app.example.com/cb",
              "scope": "User.Read"},
    )  # fmt: skip


@contextmanager
def _world(served: Served, name: str, inbound: str | None = None) -> Iterator[tuple[OpenWorld, Directory]]:
    """No world claims the Bot Framework's sign-in host: its OpenID metadata and keys, fetched with no credential
    and no tenant in the URL, are a shared host (`Manifest.shared_hosts`), answered the same with no world."""
    seed = _seed(name)
    directory = _directory(seed)
    spec = CreateWorld(
        seed=seed,
        claims=_claims(directory, []),
        inbound=[Inbound(provider="microsoft", url=inbound)] if inbound is not None else [],
    )
    world = OpenWorld(served.client, served.client.create_world(spec))
    try:
        yield world, directory
    finally:
        served.client.close_world(world.world_id)


# ------------------------------------------------------------------ a bot that checks what it is pushed


@dataclass
class Bot:
    """A bot's messaging endpoint that validates every pushed token as the Bot Framework SDK does: the signing key
    from the JWKS the Bot Framework's OpenID metadata names, fetched through the proxy; `iss`, `aud` (its app id)
    and `exp` checked; and the `serviceurl` claim equal to the activity's `serviceUrl`."""

    app_id: str
    keys: jwt.PyJWKSet | None = None
    url: str = ""
    accepted: list[dict[str, Any]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def check(self, authorization: str, service_url: str) -> str | None:
        if not authorization.startswith("Bearer "):
            return "no bearer token"
        presented = authorization.removeprefix("Bearer ")
        if self.keys is None:
            return "the bot has not fetched the Bot Framework's keys"
        try:
            kid = jwt.get_unverified_header(presented)["kid"]
            key = next(k for k in self.keys.keys if k.key_id == kid)
            claims = jwt.decode(
                presented,
                key.key,
                algorithms=["RS256"],
                audience=self.app_id,
                issuer=BOT_ISSUER,
                options={"require": ["exp", "iss", "aud"]},
            )
        except (jwt.PyJWTError, StopIteration) as e:
            return f"{type(e).__name__}: {e}"
        claimed = claims["serviceurl"] if "serviceurl" in claims else ""
        if claimed.rstrip("/") != service_url.rstrip("/"):
            return "serviceurl does not match the activity"
        return None


def _keys(served: Served) -> jwt.PyJWKSet:
    with _http(served) as http:
        metadata = http.get(BOT_METADATA).json()
        return jwt.PyJWKSet.from_dict(http.get(metadata["jwks_uri"]).json())


@contextmanager
def _bot(served: Served, app_id: str) -> Iterator[Bot]:
    bot = Bot(app_id=app_id)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            activity = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            why = bot.check(self.headers["Authorization"] or "", activity["serviceUrl"])
            if why is None:
                bot.accepted.append(activity)
                self.send_response(200)
            else:
                bot.refused.append(why)
                self.send_response(401)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    bot.url = f"http://127.0.0.1:{server.server_address[1]}/api/messages"
    try:
        yield bot
    finally:
        server.shutdown()
        server.server_close()


def _personal(world: OpenWorld, email: str) -> str:
    """The connector id of the person's 1:1 chat with the bot, as the bot learns it."""
    for stored in world.entities(provider="microsoft", kind=EntityKind.CHANNEL):
        body = json.loads(stored.body)
        if body["type"] == "personal" and len(body["members"]) == 1:
            user = next(
                json.loads(u.body)
                for u in world.entities(provider="microsoft", kind=EntityKind.RECORD)
                if u.entity.external_id == f"user:{body['members'][0]}"
            )
            if user["user"]["mail"] == email:
                return str(body["id"])
    raise LookupError(email)


# ------------------------------------------------------------------ a send answered without its id


def test_the_next_send_is_answered_without_its_id_and_the_one_after_with_it(served: Served) -> None:
    with _world(served, "noid_tenant") as (world, directory), _http(served) as http:
        world.declare_faults(
            "microsoft", {"faults": [{"call": "POST /teams/v3/conversations", "answer": {"kind": "without_id"}}]}
        )
        bot = _app_token(http, directory, "https://api.botframework.com/.default")
        url = f"{CONNECTOR}v3/conversations/{directory.general_channel_id}/activities"
        first = http.post(url, json={"type": "message", "text": "first"}, headers=bot)
        second = http.post(url, json={"type": "message", "text": "second"}, headers=bot)
        assert first.status_code == 201 and "id" not in first.json(), first.text
        assert second.status_code == 201 and "id" in second.json(), second.text
        sent = [e.after.text for e in world.events(actor=Actor.AGENT) if isinstance(e.after, MessageSnapshot)]
        assert sent == ["first", "second"], "the send answered without its id still lands"


# ------------------------------------------------------------------ people changed by an administrator


def test_a_disabled_user_cannot_sign_in_or_be_messaged_until_enabled_and_a_removed_one_is_gone(
    served: Served,
) -> None:
    with _world(served, "people_tenant") as (world, directory), _http(served) as http:
        graph = _app_token(http, directory, "https://graph.microsoft.com/.default")
        bot = _app_token(http, directory, "https://api.botframework.com/.default")
        email = "sofia@people_tenant.example.com"
        chat = _personal(world, email)
        assert _user_sign_in(http, directory, email).status_code == 200

        changed = world.deactivate_person("microsoft", "sofia")
        assert changed.actor is Actor.SCENARIO
        assert http.get(f"{GRAPH}/users/{email}", headers=graph).json()["accountEnabled"] is False
        refused = _user_sign_in(http, directory, email)
        assert refused.status_code == 400 and "AADSTS50057" in refused.text, refused.text
        blocked = http.post(f"{CONNECTOR}v3/conversations/{chat}/activities",
                            json={"type": "message", "text": "hello?"}, headers=bot)  # fmt: skip
        assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "BotNotInConversationRoster"

        world.reactivate_person("microsoft", "sofia")
        assert _user_sign_in(http, directory, email).status_code == 200
        sent = http.post(f"{CONNECTOR}v3/conversations/{chat}/activities",
                         json={"type": "message", "text": "hello"}, headers=bot)  # fmt: skip
        assert sent.status_code == 201, sent.text

        world.remove_person("microsoft", "sofia")
        gone = http.get(f"{GRAPH}/users/{email}", headers=graph)
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "Request_ResourceNotFound"
        assert _user_sign_in(http, directory, email).status_code == 400


def test_enabling_a_user_who_is_not_disabled_is_refused(served: Served) -> None:
    with _world(served, "refuse_tenant") as (world, _):
        with pytest.raises(Refused) as refused:
            world.reactivate_person("microsoft", "owen")
        assert refused.value.status == 409 and "enabled already" in refused.value.error


def test_a_provider_without_people_changes_is_refused_as_unsupported(served: Served) -> None:
    with _world(served, "unsupported_tenant") as (world, _):
        with pytest.raises(Unsupported):
            world.deactivate_person("google_workspace", "owen")


# ------------------------------------------------------------------ what a person pushes, checked as a bot checks it


def test_every_teams_shape_a_person_pushes_passes_the_bots_own_check(served: Served) -> None:
    seed = _seed("inbound_tenant")
    directory = _directory(seed)
    with (
        _bot(served, directory.bot_app_id) as bot,
        _world(served, "inbound_tenant", bot.url) as (
            world,
            _,
        ),
    ):
        bot.keys = _keys(served)
        with _http(served) as http:
            token = _app_token(http, directory, "https://api.botframework.com/.default")
            card = {"type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": "Approve?"}],
                    "actions": [{"type": "Action.Execute", "title": "Approve", "verb": "approve", "id": "approve"}]}  # fmt: skip
            asked = http.post(
                f"{CONNECTOR}v3/conversations/{_personal(world, 'owen@inbound_tenant.example.com')}/activities",
                json={"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                          "content": card}]},
                headers=token,
            )  # fmt: skip
            posted = http.post(
                f"{CONNECTOR}v3/conversations/{directory.general_channel_id}/activities",
                json={"type": "message", "text": "Standup notes are up"},
                headers=token,
            )
        assert asked.status_code == posted.status_code == 201
        message = lambda activity: next(  # noqa: E731
            e.entity for e in world.events(provider="microsoft") if e.entity.external_id == activity
        )
        world.say("owen", "Can you chase the venue?", provider="microsoft")
        world.reply("owen", "Thanks, seen.", to=message(posted.json()["id"]))
        world.happen(PersonPosts(provider="microsoft", person="sofia", channel="general", text="Anyone?"))
        world.happen(PersonPosts(provider="microsoft", person="sofia", channel="general", text="please look",
                                 mentions_agent=True))  # fmt: skip
        world.happen(PersonAddsAgent(provider="microsoft", person="ines"))
        world.press("owen", message(asked.json()["id"]), Press(action_id="approve", label="Approve"))
    assert bot.refused == []
    shapes = [(a["type"], a["conversation"]["conversationType"]) for a in bot.accepted]
    assert shapes == [
        ("message", "personal"),
        ("message", "channel"),
        ("message", "channel"),
        ("installationUpdate", "personal"),
        ("conversationUpdate", "personal"),
        ("invoke", "personal"),
    ], "a channel post without a mention is not pushed, as Teams does not push it"


def test_a_minted_bot_framework_token_passes_the_bots_check_and_a_missing_audience_is_refused(
    served: Served,
) -> None:
    with _world(served, "mint_tenant") as (world, directory):
        bot = Bot(app_id=directory.bot_app_id, keys=_keys(served))
        minted = world.inbound_credential(
            "microsoft", InboundCredentialAsk(service_url=CONNECTOR, audience=directory.bot_app_id)
        )
        assert [h.name for h in minted.headers] == ["Authorization"]
        assert bot.check(minted.headers[0].value, CONNECTOR) is None
        assert bot.check(minted.headers[0].value, "https://elsewhere.example.com/") is not None
        with pytest.raises(Refused) as refused:
            world.inbound_credential("microsoft", InboundCredentialAsk(service_url=CONNECTOR))
        assert refused.value.status == 409


# ------------------------------------------------------------------ out of office, as Graph shows it


def test_a_person_away_shows_out_of_office_while_it_lasts_and_available_after(served: Served) -> None:
    with _world(served, "away_tenant") as (world, directory), _http(served) as http:
        graph = _app_token(http, directory, "https://graph.microsoft.com/.default")
        sofia = http.get(f"{GRAPH}/users/sofia@away_tenant.example.com", headers=graph).json()["id"]
        presence = http.get(f"{GRAPH}/users/{sofia}/presence", headers=graph).json()
        assert (presence["availability"], presence["activity"]) == ("Away", "OutOfOffice")
        assert "parental leave" in presence["outOfOfficeSettings"]["message"]
        replies = http.get(f"{GRAPH}/users/{sofia}/mailboxSettings", headers=graph).json()["automaticRepliesSetting"]
        assert replies["status"] == "scheduled"
        assert replies["scheduledEndDateTime"]["dateTime"].startswith("2026-09-03T09:00:00")
        batch = http.post(f"{GRAPH}/communications/getPresencesByUserId", json={"ids": [sofia]}, headers=graph)
        assert batch.json()["value"][0]["activity"] == "OutOfOffice"

        owen = http.get(f"{GRAPH}/users/owen@away_tenant.example.com/presence", headers=graph).json()
        assert owen["availability"] == "Available", "an absence not begun yet shows nothing"
        planned = http.get(f"{GRAPH}/users/owen@away_tenant.example.com/mailboxSettings", headers=graph).json()
        assert planned["automaticRepliesSetting"]["scheduledStartDateTime"]["dateTime"].startswith("2026-09-06T09")

        world.advance(timedelta(days=3))
        after = http.get(f"{GRAPH}/communications/presences/{sofia}", headers=graph).json()
        assert (after["availability"], after["outOfOfficeSettings"]["isOutOfOffice"]) == ("Available", False)
        ended = http.get(f"{GRAPH}/users/{sofia}/mailboxSettings/automaticRepliesSetting", headers=graph).json()
        assert ended["status"] == "disabled"


def test_an_absence_from_the_first_ask_shows_only_once_the_agent_has_messaged_them(served: Served) -> None:
    with _world(served, "ask_tenant") as (world, directory), _http(served) as http:
        graph = _app_token(http, directory, "https://graph.microsoft.com/.default")
        bot = _app_token(http, directory, "https://api.botframework.com/.default")
        email = "dania@ask_tenant.example.com"
        before = http.get(f"{GRAPH}/users/{email}/presence", headers=graph).json()
        assert before["availability"] == "Available"
        sent = http.post(f"{CONNECTOR}v3/conversations/{_personal(world, email)}/activities",
                         json={"type": "message", "text": "Could you review the filing?"}, headers=bot)  # fmt: skip
        assert sent.status_code == 201
        after = http.get(f"{GRAPH}/users/{email}/presence", headers=graph).json()
        assert after["activity"] == "OutOfOffice" and "off-site training" in after["outOfOfficeSettings"]["message"]


# ------------------------------------------------------------------ what the agent makes and shares


def test_a_file_the_agent_creates_and_shares_carries_its_owner_its_place_and_the_grant(served: Served) -> None:
    with _world(served, "files_tenant") as (world, directory), _http(served) as http:
        graph = _app_token(http, directory, "https://graph.microsoft.com/.default")
        site = http.get(f"{GRAPH}/sites?search=*", headers=graph).json()["value"][0]
        made = http.put(
            f"{GRAPH}/sites/{site['id']}/drive/root:/Report.txt:/content",
            content=b"The filing is ready.",
            headers={**graph, "Content-Type": "text/plain"},
        )
        assert made.status_code in (200, 201), made.text
        invited = http.post(
            f"{GRAPH}/drives/{made.json()['parentReference']['driveId']}/items/{made.json()['id']}/invite",
            json={"recipients": [{"email": "sofia@files_tenant.example.com"}], "roles": ["write"],
                  "requireSignIn": True, "sendInvitation": False},
            headers=graph,
        )  # fmt: skip
        assert invited.status_code == 200, invited.text
        created = [e.after for e in world.events(actor=Actor.AGENT) if isinstance(e.after, DocumentSnapshot)]
        assert created[0].title == "Report.txt"
        assert (created[0].owner, created[0].space) == (directory.bot_name, site["displayName"])
        grants = [e.after for e in world.events(actor=Actor.AGENT) if isinstance(e.after, GrantSnapshot)]
        assert [(g.document, g.to, g.role.value) for g in grants] == [
            ("Report.txt", "sofia@files_tenant.example.com", "writer")
        ]


# ------------------------------------------------------------------ two tenants, two bots, one server


def test_two_tenants_with_two_bots_each_send_only_in_their_own_world(served: Served) -> None:
    with _world(served, "bots_one") as (one, first), _world(served, "bots_two") as (two, second):
        assert first.bot_app_id != second.bot_app_id and first.bot_app_secret != second.bot_app_secret
        results: dict[str, int] = {}

        def send(label: str, directory: Directory) -> None:
            with _http(served) as http:
                bot = _app_token(http, directory, "https://api.botframework.com/.default")
                results[label] = http.post(
                    f"{CONNECTOR}v3/conversations/{directory.general_channel_id}/activities",
                    json={"type": "message", "text": f"from {label}"},
                    headers=bot,
                ).status_code

        threads = [threading.Thread(target=send, args=a) for a in (("one", first), ("two", second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert results == {"one": 201, "two": 201}
        with _http(served) as http:
            crossed = http.post(
                f"{LOGIN}/{second.tenant_id}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": first.bot_app_id,
                      "client_secret": first.bot_app_secret, "scope": "https://api.botframework.com/.default"},
            )  # fmt: skip
        assert crossed.status_code == 200, "Minutehand does not enforce credentials: any client signs in anywhere"
        texts = {
            label: [e.after.text for e in w.events(actor=Actor.AGENT) if isinstance(e.after, MessageSnapshot)]
            for label, w in (("one", one), ("two", two))
        }
        assert texts == {"one": ["from one"], "two": ["from two"]}
