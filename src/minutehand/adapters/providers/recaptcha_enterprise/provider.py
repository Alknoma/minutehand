"""reCAPTCHA Enterprise, as the backend of a page protected by it sees it.

A page's grecaptcha mints a token in the browser; the page's backend sends it to `projects.assessments.create` and
acts on the verdict. There is no browser in a run, so the tokens are the scenario's: each seeded token is one a
browser minted, with the verdict reCAPTCHA gives it (the action it was minted for, the score, the page's hostname)
and when it was minted. The world decides what reCAPTCHA documents it decides from the token alone: a token never
minted is MALFORMED, an event without one MISSING, a token assessed before DUPE, one minted more than two minutes
before the assessment EXPIRED. `RecaptchaSeed.unseeded` gives the verdict for any token the seed does not name,
which stands for a fresh token minted for each assessment, so it is never DUPE or EXPIRED.

What the world log holds: every seeded token, the unseeded verdict and every assessment are RECORD entities
(`RecordSnapshot`, `resource` "token", "unseeded" or "assessment"); an assessment is the agent's, listed under the
token it assessed, so a token's earlier assessment is read from the log. Nothing lives outside it.

What it cannot do, loudly: every other method of the v1 API, and an assessment asking for more than a token's
verdict (account defender, fraud prevention, firewall policies, password leaks, express and WAF assessments),
answers 501, naming it. No credential is ever checked. Where each behaviour comes from is in `CLAIMS.md`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from pydantic import Field, ValidationError, field_validator, model_validator
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Router

from minutehand.adapters.providers.recaptcha_enterprise import wire
from minutehand.adapters.providers.recaptcha_enterprise.manifest import MANIFEST
from minutehand.domain.errors import NotServed, Rendered
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

TOKEN_LIFETIME = timedelta(seconds=wire.TOKEN_LIFETIME_SECONDS)


class Verdict(Model):
    """What reCAPTCHA says of a valid token: the action it was minted for, its score and the page's hostname."""

    action: str = Field(default="", description="The action the page passed to grecaptcha.enterprise.execute")
    score: float = Field(ge=0.0, le=1.0, description="0.0 to 1.0 in steps of 0.1: reCAPTCHA's 11 levels")
    hostname: str = Field(default="", description="The hostname of the page the token was minted on")
    reasons: list[wire.ClassificationReason] = []

    @field_validator("score")
    @classmethod
    def _one_of_eleven_levels(cls, score: float) -> float:
        if abs(score * 10 - round(score * 10)) > 1e-9:
            raise ValueError(f"score {score} is none of reCAPTCHA's 11 levels, 0.0 to 1.0 in steps of 0.1")
        return round(score, 1)


class UnseededVerdict(Verdict):
    """The verdict for a token the seed does not name: a fresh token a browser minted for this assessment."""

    action: str | None = Field(  # type: ignore[assignment]
        default=None,
        description="None: the action the assessment's event expects, as a browser on that page would have minted "
        "the token for",
    )


class SeededToken(Verdict):
    """A token a browser's grecaptcha minted, and the verdict reCAPTCHA gives it."""

    token: str = Field(min_length=1)
    minted_at: datetime | None = Field(default=None, description="None: the run's start")


class RecaptchaSeed(Model):
    """`ProviderSeed.body` for `recaptcha_enterprise`."""

    tokens: list[SeededToken] = []
    unseeded: UnseededVerdict | None = Field(
        default=None,
        description="The verdict for a token the seed does not name: a fresh token minted for each assessment. "
        "None: such a token is MALFORMED",
    )

    @model_validator(mode="after")
    def _each_token_once(self) -> RecaptchaSeed:
        given = [t.token for t in self.tokens]
        twice = sorted({t for t in given if given.count(t) > 1})
        if twice:
            raise ValueError(f"a token is seeded twice: {', '.join(twice)}")
        return self


class TokenRecord(Model):
    token: str
    action: str
    score: float
    hostname: str
    reasons: list[wire.ClassificationReason]
    minted_at: datetime


class AssessmentRecord(Model):
    name: str
    project: str
    event: str = Field(description="The event as the answer echoes it, as JSON text")
    token: str | None
    expected_action: str
    valid: bool
    invalid_reason: wire.InvalidReason | None
    action: str
    hostname: str
    score: float
    reasons: list[wire.ClassificationReason]
    create_time: datetime | None = Field(description="When the token was minted, as reCAPTCHA answers it")


UNSEEDED = EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id="unseeded")


def _token_ref(token: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=_token_id(token))


def _token_id(token: str) -> str:
    return f"token/{token}"


def _assessment_ref(name: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=name)


class Tokens:
    """The seeded tokens, the unseeded verdict and the assessments, as the world log holds them."""

    def __init__(self, world: Store) -> None:
        self.world = world

    def token(self, token: str) -> TokenRecord | None:
        stored = self.world.get(_token_ref(token))
        return TokenRecord.model_validate_json(stored.body) if stored is not None else None

    def unseeded(self) -> UnseededVerdict | None:
        stored = self.world.get(UNSEEDED)
        return UnseededVerdict.model_validate_json(stored.body) if stored is not None else None

    def assessed_before(self, token: str) -> bool:
        return bool(self.world.children(MANIFEST.key, EntityKind.RECORD, _token_id(token), limit=1))

    def put_token(self, record: TokenRecord) -> None:
        self.world.apply(
            Change(
                entity=_token_ref(record.token),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=record.model_dump_json(),
                after=RecordSnapshot(resource="token", text=f"{record.token} {record.action} {record.hostname}"),
            )
        )

    def put_unseeded(self, verdict: UnseededVerdict) -> None:
        self.world.apply(
            Change(
                entity=UNSEEDED,
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=verdict.model_dump_json(),
                after=RecordSnapshot(resource="unseeded", text=f"{verdict.action or ''} {verdict.hostname}".strip()),
            )
        )

    def put_assessment(self, record: AssessmentRecord) -> None:
        verdict = record.invalid_reason.value if record.invalid_reason is not None else f"valid {record.score}"
        self.world.apply(
            Change(
                entity=_assessment_ref(record.name),
                operation=Operation.CREATE,
                actor=Actor.AGENT,
                body=record.model_dump_json(),
                parent=_token_id(record.token) if record.token else record.project,
                after=RecordSnapshot(
                    resource="assessment",
                    text=" ".join(t for t in (record.token, record.expected_action, record.action, verdict) if t),
                ),
            )
        )


REFUSED_EVENT_FIELDS = {
    "express": "an express assessment, without a token, is not served: a world's verdicts are its seeded tokens'",
    "wafTokenAssessment": "WAF token assessments are not served",
    "firewallPolicyEvaluation": "firewall policy evaluation is not served",
    "transactionData": "fraud prevention (an event's transactionData) is not served",
    "userInfo": "account defender (an event's userInfo) is not served",
    "fraudPrevention": "fraud prevention settings are not served",
}
"""The fields of an `Event` that ask for an answer beyond a token's verdict, and why each is refused."""

REFUSED_ASSESSMENT_FIELDS = {
    "accountVerification": "account verification is not served",
    "privatePasswordLeakVerification": "private password leak verification is not served",
}


def _refused_event(event: wire.EventIn) -> str | None:
    """The first field of `event` this provider refuses, when it sets one."""
    asked: dict[str, bool] = {
        "express": event.express,
        "wafTokenAssessment": event.wafTokenAssessment,
        "firewallPolicyEvaluation": event.firewallPolicyEvaluation,
        "transactionData": event.transactionData is not None,
        "userInfo": event.userInfo is not None,
        "fraudPrevention": bool(event.fraudPrevention),
    }
    return next((name for name, set_ in asked.items() if set_), None)


def _parsed(raw: bytes) -> wire.AssessmentIn:
    try:
        return wire.AssessmentIn.model_validate_json(raw or b"{}")
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise wire.RecaptchaRefusal(400, f"{wire.INVALID_ARGUMENT} {problems}") from e


def _assessment_id(project: str, head: int) -> str:
    """The id Google gives an assessment: in no reference beyond the name's format, so drawn from the project and
    the log's position, unique in the run and the same when the run is replayed."""
    return hashlib.sha256(f"{project}@{head}".encode()).hexdigest()[:16]


class RecaptchaProvider:
    manifest: Manifest = MANIFEST
    seed_model = RecaptchaSeed

    def error(self, status: int, code: str, message: str) -> Rendered:
        return wire.error_answer(status, "UNIMPLEMENTED" if status == 501 else "INTERNAL", message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        """The tokens `RecaptchaSeed` declares, and the verdict for any other. A `SignIn` on this provider is
        refused: reCAPTCHA Enterprise has no sign-in of its own (a backend brings the access token Google's token
        endpoint mints), so it would be dropped."""
        if any(s.provider == MANIFEST.key for s in scenario.sign_ins):
            raise ValueError(
                f"a seeded sign_in is on {MANIFEST.key}, which has no sign-in of its own: a reCAPTCHA Enterprise "
                "client sends the access token Google's token endpoint mints, and no credential names a person here"
            )
        given = scenario.provider_seed(MANIFEST.key)
        if given is None:
            return
        found = RecaptchaSeed.model_validate_json(given.body)
        tokens = Tokens(world)
        for t in found.tokens:
            tokens.put_token(
                TokenRecord(
                    token=t.token,
                    action=t.action,
                    score=t.score,
                    hostname=t.hostname,
                    reasons=t.reasons,
                    minted_at=t.minted_at if t.minted_at is not None else scenario.starts_at,
                )
            )
        if found.unseeded is not None:
            tokens.put_unseeded(found.unseeded)

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        tokens = Tokens(world)

        async def create_assessment(request: Request) -> Response:
            project = request.path_params["project"]
            record, answer = _assess(tokens, world, clock, project, _parsed(await request.body()))
            tokens.put_assessment(record)
            return Response(answer, media_type=wire.JSON)

        async def unserved(request: Request) -> Response:
            called = wire.rest_method(request.method, request.url.path)
            if called is None:
                raise NotServed(
                    f"no method of reCAPTCHA Enterprise v1 has the route {request.method} {request.url.path}"
                )
            raise NotServed(f"{called}: {wire.REST_REFUSED[called]}")

        return Router(
            routes=[
                Route("/v1/projects/{project}/assessments", create_assessment, methods=["POST"]),
                Route("/{rest:path}", unserved, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
            ]
        )


def _assess(
    tokens: Tokens, world: Store, clock: Clock, project: str, asked: wire.AssessmentIn
) -> tuple[AssessmentRecord, str]:
    """The assessment of `asked`'s event, and its answer: proto3 JSON text."""
    for field, why in REFUSED_ASSESSMENT_FIELDS.items():
        if getattr(asked, field) is not None:
            raise NotServed(f"recaptchaenterprise.projects.assessments.create with {field}: {why}")
    event = asked.event or wire.EventIn()
    refused = _refused_event(event)
    if refused is not None:
        raise NotServed(
            f"recaptchaenterprise.projects.assessments.create with event.{refused}: {REFUSED_EVENT_FIELDS[refused]}"
        )
    now = clock.now()
    verdict: Verdict | None = None
    reason: wire.InvalidReason | None = None
    minted: datetime | None = None
    if not event.token:
        reason = wire.InvalidReason.MISSING
    else:
        seeded = tokens.token(event.token)
        if seeded is not None:
            if tokens.assessed_before(event.token):
                reason = wire.InvalidReason.DUPE
            elif now - seeded.minted_at > TOKEN_LIFETIME:
                reason = wire.InvalidReason.EXPIRED
            else:
                verdict = Verdict(
                    action=seeded.action, score=seeded.score, hostname=seeded.hostname, reasons=seeded.reasons
                )
                minted = seeded.minted_at
        else:
            fresh = tokens.unseeded()
            if fresh is None:
                reason = wire.InvalidReason.MALFORMED
            else:
                verdict = Verdict(
                    action=fresh.action if fresh.action is not None else event.expectedAction,
                    score=fresh.score,
                    hostname=fresh.hostname,
                    reasons=fresh.reasons,
                )
                minted = now
    name = f"projects/{project}/assessments/{_assessment_id(project, world.head())}"
    properties = wire.TokenPropertiesOut(
        valid=verdict is not None,
        invalid_reason=reason,
        create_time=minted,
        hostname=verdict.hostname if verdict is not None else "",
        action=verdict.action if verdict is not None else "",
    )
    score = verdict.score if verdict is not None else 0.0
    reasons = verdict.reasons if verdict is not None else []
    record = AssessmentRecord(
        name=name,
        project=f"projects/{project}",
        event=json.dumps(event.echoed()),
        token=event.token or None,
        expected_action=event.expectedAction,
        valid=verdict is not None,
        invalid_reason=reason,
        action=properties.action,
        hostname=properties.hostname,
        score=score,
        reasons=reasons,
        create_time=minted,
    )
    return record, wire.assessment_answer(name, event, properties, score, reasons)


def build() -> RecaptchaProvider:
    return RecaptchaProvider()
