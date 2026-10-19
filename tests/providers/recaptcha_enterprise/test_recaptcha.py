"""reCAPTCHA Enterprise, reached the way a page's backend reaches it: Google's own discovery client
(`google-api-python-client`) and stock `google-auth` with a user's refresh token, in a process of its own configured
only by the environment Minutehand hands an agent, through the proxy. The refresh grant is answered by the Google
Workspace provider; the assessment by this one."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand.adapters.providers.recaptcha_enterprise.manifest import MANIFEST
from minutehand.adapters.providers.recaptcha_enterprise.provider import (
    AssessmentRecord,
    RecaptchaSeed,
    SeededToken,
    UnseededVerdict,
    build,
)
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import AfterScript, Person, ProviderSeed, Scenario, Scripted, SignIn
from minutehand.domain.world import Actor, EntityKind, RecordSnapshot
from tests.providers.google_workspace.proxied import Client, client_environment

pytestmark = pytest.mark.timeout(120)

START = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
REFRESH = "1//ines-recaptcha-refresh-token"
PROJECT = "checkout-site"

PRELUDE = f"""
import json, sys
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

creds = Credentials(token=None, refresh_token={REFRESH!r}, token_uri="https://oauth2.googleapis.com/token",
                    client_id="1234.apps.googleusercontent.com", client_secret="client-secret",
                    scopes=["https://www.googleapis.com/auth/cloud-platform"])
recaptcha = build("recaptchaenterprise", "v1", credentials=creds, cache_discovery=False, static_discovery=True)
session = AuthorizedSession(creds)
PARENT = "projects/{PROJECT}"

def say(**found):
    print(json.dumps(found), flush=True)

def wait():
    return sys.stdin.readline().strip()

def assess(**event):
    return recaptcha.projects().assessments().create(parent=PARENT, body={{"event": event}}).execute()

def refused(call):
    try:
        call()
    except HttpError as error:
        return [error.resp.status, json.loads(error.content)["error"]["status"], json.loads(error.content)["error"]["message"]]
    return None
"""


@dataclass
class Recaptcha:
    store: SqliteStore
    clock: RunClock
    environment: dict[str, str]

    async def client(self, program: str) -> Client:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            PRELUDE + program,
            env=self.environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return Client(child)

    def assessments(self) -> list[AssessmentRecord]:
        return [
            AssessmentRecord.model_validate_json(self.store.versions(e.entity)[-1].body)
            for e in self.store.events()
            if e.entity.provider == MANIFEST.key and e.actor is Actor.AGENT
        ]


def scenario(given: RecaptchaSeed) -> Scenario:
    return Scenario(
        name="checkout",
        goal="Only people place orders.",
        owner="ines",
        starts_at=START,
        people=[
            Person(key="ines", name="Ines Moreau", email="ines@example.com", reply=Scripted(then=AfterScript.SILENT))
        ],
        sign_ins=[SignIn(provider="google_workspace", credential=REFRESH, person="ines")],
        provider_seeds=[ProviderSeed(provider=MANIFEST.key, body=given.model_dump_json())],
    )


SEED = RecaptchaSeed(
    tokens=[
        SeededToken(token="03AFcWeA-human", action="checkout", score=0.9, hostname="shop.example.com"),
        SeededToken(
            token="03AFcWeA-bot",
            action="checkout",
            score=0.1,
            hostname="shop.example.com",
            reasons=["AUTOMATION"],  # type: ignore[list-item]
        ),
        SeededToken(
            token="03AFcWeA-stale",
            action="login",
            score=0.7,
            hostname="shop.example.com",
            minted_at=START - timedelta(minutes=5),
        ),
    ]
)


async def opened(tmp_path: Path, given: RecaptchaSeed) -> AsyncIterator[Recaptcha]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    registry = Registry()
    registry.discover("minutehand.adapters.providers")
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(store, clock, {}, scenario=scenario(given))
        yield Recaptcha(store, clock, client_environment(proxy))


@pytest.fixture
async def recaptcha(tmp_path: Path) -> AsyncIterator[Recaptcha]:
    async for found in opened(tmp_path, SEED):
        yield found


async def test_a_seeded_token_is_answered_with_its_verdict_and_the_event_echoed(recaptcha: Recaptcha) -> None:
    client = await recaptcha.client(
        """
say(human=assess(token="03AFcWeA-human", siteKey="6Lc-site", expectedAction="checkout", userIpAddress="203.0.113.9",
                 userAgent="Mozilla/5.0"),
    bot=assess(token="03AFcWeA-bot", siteKey="6Lc-site", expectedAction="checkout"))
"""
    )
    heard = await client.heard()
    await client.finished()

    human, bot = heard["human"], heard["bot"]
    assert isinstance(human, dict) and isinstance(bot, dict)
    assert str(human["name"]).startswith(f"projects/{PROJECT}/assessments/")
    assert human["event"] == {
        "token": "03AFcWeA-human",
        "siteKey": "6Lc-site",
        "userAgent": "Mozilla/5.0",
        "userIpAddress": "203.0.113.9",
        "expectedAction": "checkout",
    }
    assert human["tokenProperties"] == {
        "valid": True,
        "createTime": "2026-09-14T08:30:00Z",
        "hostname": "shop.example.com",
        "action": "checkout",
    }
    assert human["riskAnalysis"] == {"score": 0.9}, "no reasons: proto3 JSON leaves an empty list out"
    assert bot["riskAnalysis"] == {"score": 0.1, "reasons": ["AUTOMATION"]}

    records = recaptcha.assessments()
    assert [(r.token, r.valid, r.score) for r in records] == [
        ("03AFcWeA-human", True, 0.9),
        ("03AFcWeA-bot", True, 0.1),
    ]
    made = [e for e in recaptcha.store.events() if e.entity.provider == MANIFEST.key and e.actor is Actor.AGENT]
    assert all(e.entity.kind is EntityKind.RECORD for e in made)
    calls = [c.exchange for c in recaptcha.store.calls() if c.exchange.host == "recaptchaenterprise.googleapis.com"]
    assert '"token": "03AFcWeA-human"' in (calls[0].request_body or ""), "a response token is data, kept as sent"
    assert isinstance(made[0].after, RecordSnapshot) and made[0].after.resource == "assessment"


async def test_a_token_assessed_again_is_dupe_and_one_older_than_two_minutes_is_expired(recaptcha: Recaptcha) -> None:
    client = await recaptcha.client(
        """
first = assess(token="03AFcWeA-human", expectedAction="checkout")
again = assess(token="03AFcWeA-human", expectedAction="checkout")
stale = assess(token="03AFcWeA-stale", expectedAction="login")
say(first=first["tokenProperties"], again=again["tokenProperties"], again_risk=again["riskAnalysis"],
    stale=stale["tokenProperties"])
"""
    )
    heard = await client.heard()
    await client.finished()
    assert heard["first"] == {
        "valid": True,
        "createTime": "2026-09-14T08:30:00Z",
        "hostname": "shop.example.com",
        "action": "checkout",
    }
    assert heard["again"] == {"invalidReason": "DUPE"}, "valid is false, which proto3 JSON leaves out"
    assert heard["again_risk"] == {}
    assert heard["stale"] == {"invalidReason": "EXPIRED"}


async def test_a_token_expires_two_minutes_after_it_was_minted_on_the_runs_clock(recaptcha: Recaptcha) -> None:
    client = await recaptcha.client(
        """
wait()
say(at_two=assess(token="03AFcWeA-human")["tokenProperties"])
wait()
say(past_two=assess(token="03AFcWeA-bot")["tokenProperties"])
"""
    )
    recaptcha.clock.jump(START + timedelta(minutes=2))
    await client.go()
    assert (await client.heard())["at_two"] == {
        "valid": True,
        "createTime": "2026-09-14T08:30:00Z",
        "hostname": "shop.example.com",
        "action": "checkout",
    }
    recaptcha.clock.jump(START + timedelta(minutes=2, seconds=1))
    await client.go()
    assert (await client.heard())["past_two"] == {"invalidReason": "EXPIRED"}
    await client.finished()


async def test_an_unseeded_token_is_malformed_and_a_missing_one_missing(recaptcha: Recaptcha) -> None:
    client = await recaptcha.client(
        """
say(unseeded=assess(token="03AFcWeA-never-minted"), missing=assess(siteKey="6Lc-site", expectedAction="checkout"),
    empty=recaptcha.projects().assessments().create(parent=PARENT, body={}).execute())
"""
    )
    heard = await client.heard()
    await client.finished()
    unseeded, missing, empty = heard["unseeded"], heard["missing"], heard["empty"]
    assert isinstance(unseeded, dict) and isinstance(missing, dict) and isinstance(empty, dict)
    assert unseeded["tokenProperties"] == {"invalidReason": "MALFORMED"}
    assert missing["tokenProperties"] == {"invalidReason": "MISSING"}
    assert missing["event"] == {"siteKey": "6Lc-site", "expectedAction": "checkout"}
    assert empty["tokenProperties"] == {"invalidReason": "MISSING"} and empty["event"] == {}


async def test_the_unseeded_verdict_stands_for_a_fresh_token_each_time_and_is_never_dupe(tmp_path: Path) -> None:
    given = SEED.model_copy(
        update={"unseeded": UnseededVerdict(action="signup", score=0.7, hostname="shop.example.com")}
    )
    async for recaptcha in opened(tmp_path, given):
        client = await recaptcha.client(
            """
wait()
say(once=assess(token="03AFcWeA-anything")["tokenProperties"], twice=assess(token="03AFcWeA-anything")["tokenProperties"])
"""
        )
        recaptcha.clock.jump(START + timedelta(hours=3, seconds=7))
        await client.go()
        heard = await client.heard()
        await client.finished()
        fresh = {
            "valid": True,
            "createTime": "2026-09-14T11:30:07Z",
            "hostname": "shop.example.com",
            "action": "signup",
        }
        assert heard["once"] == fresh and heard["twice"] == fresh


async def test_an_assessment_asking_for_more_than_a_tokens_verdict_is_refused_by_name(recaptcha: Recaptcha) -> None:
    client = await recaptcha.client(
        """
say(fraud=refused(lambda: assess(token="03AFcWeA-human", transactionData={"value": 12})),
    express=refused(lambda: assess(express=True, siteKey="6Lc-site")),
    leak=refused(lambda: recaptcha.projects().assessments().create(parent=PARENT, body={
        "event": {"token": "03AFcWeA-human"}, "privatePasswordLeakVerification": {"lookupHashPrefix": "AAAA"}}).execute()),
    unknown=refused(lambda: assess(token="03AFcWeA-human", favouriteColour="blue")))
"""
    )
    heard = await client.heard()
    await client.finished()
    fraud, express, leak, unknown = heard["fraud"], heard["express"], heard["leak"], heard["unknown"]
    assert isinstance(fraud, list) and fraud[:2] == [501, "UNIMPLEMENTED"]
    assert str(fraud[2]).endswith(
        "recaptchaenterprise.projects.assessments.create with event.transactionData: fraud prevention (an event's "
        "transactionData) is not served"
    )
    assert isinstance(express, list) and express[0] == 501 and "event.express" in str(express[2])
    assert isinstance(leak, list) and leak[0] == 501 and "privatePasswordLeakVerification" in str(leak[2])
    assert (
        isinstance(unknown, list) and unknown[:2] == [400, "INVALID_ARGUMENT"] and "favouriteColour" in str(unknown[2])
    )
    assert recaptcha.assessments() == [], "a refused assessment is no assessment"


async def test_either_spelling_of_a_field_is_read_and_the_answer_is_in_lower_camel_case(recaptcha: Recaptcha) -> None:
    client = await recaptcha.client(
        f"""
answered = session.post("https://recaptchaenterprise.googleapis.com/v1/projects/{PROJECT}/assessments",
                        json={{"event": {{"token": "03AFcWeA-human", "site_key": "6Lc-site", "expected_action": "checkout"}}}})
say(status=answered.status_code, text=answered.text)
"""
    )
    heard = await client.heard()
    await client.finished()
    assert heard["status"] == 200
    answered = json.loads(str(heard["text"]))
    assert answered["event"] == {"token": "03AFcWeA-human", "siteKey": "6Lc-site", "expectedAction": "checkout"}


async def test_an_unseeded_verdict_with_no_action_answers_the_action_each_assessment_expects(tmp_path: Path) -> None:
    given = SEED.model_copy(update={"unseeded": UnseededVerdict(score=0.9, hostname="shop.example.com")})
    async for recaptcha in opened(tmp_path, given):
        client = await recaptcha.client(
            """
say(signup=assess(token="03AFcWeA-a", expectedAction="signup")["tokenProperties"],
    login=assess(token="03AFcWeA-b", expectedAction="login")["tokenProperties"],
    none=assess(token="03AFcWeA-c")["tokenProperties"],
    seeded=assess(token="03AFcWeA-human", expectedAction="login")["tokenProperties"])
"""
        )
        heard = await client.heard()
        await client.finished()
        stamp = {"valid": True, "createTime": "2026-09-14T08:30:00Z", "hostname": "shop.example.com"}
        assert heard["signup"] == {**stamp, "action": "signup"}
        assert heard["login"] == {**stamp, "action": "login"}
        assert heard["none"] == stamp, "no expected action, no action: proto3 JSON leaves the empty string out"
        assert heard["seeded"] == {**stamp, "action": "checkout"}, "a seeded token keeps the action it was minted for"


def test_a_seeded_sign_in_on_recaptcha_is_refused_at_seeding(tmp_path: Path) -> None:
    signed = scenario(SEED).model_copy(
        update={"sign_ins": [SignIn(provider=MANIFEST.key, credential="ya29.ines", person="ines")]}
    )
    with pytest.raises(ValueError, match=r"a seeded sign_in is on recaptcha_enterprise, which has no sign-in"):
        build().seed(signed, SqliteStore(tmp_path / "world.db", "run", RunClock(START)))


def test_a_seed_naming_a_token_twice_or_a_score_between_levels_is_refused() -> None:
    with pytest.raises(ValidationError, match="a token is seeded twice: t1"):
        RecaptchaSeed(tokens=[SeededToken(token="t1", score=0.5), SeededToken(token="t1", score=0.7)])
    with pytest.raises(ValidationError, match=r"score 0\.55 is none of reCAPTCHA's 11 levels"):
        SeededToken(token="t2", score=0.55)
