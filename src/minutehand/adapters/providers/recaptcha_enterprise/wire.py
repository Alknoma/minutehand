"""reCAPTCHA Enterprise v1 as its REST API puts it on the wire: proto3 JSON, field names in lowerCamelCase on the way
out and either spelling on the way in, fields at their default value left out, timestamps as RFC 3339, and Google's
error envelope.

https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AliasChoices, ConfigDict, Field, JsonValue

from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

JSON = "application/json; charset=UTF-8"

TOKEN_LIFETIME_SECONDS = 120
""""Each token can be used only once, and it expires after two minutes"
(https://cloud.google.com/recaptcha/docs/create-assessment-website)."""

INVALID_ARGUMENT = "The client specified an invalid argument."
"""google.rpc.Code INVALID_ARGUMENT (https://github.com/googleapis/googleapis/blob/master/google/rpc/code.proto)."""


class InvalidReason(StrEnum):
    """`TokenProperties.InvalidReason`: the ones the world can decide, of the documented enum."""

    MALFORMED = "MALFORMED"
    EXPIRED = "EXPIRED"
    DUPE = "DUPE"
    MISSING = "MISSING"


class ClassificationReason(StrEnum):
    """`RiskAnalysis.ClassificationReason`, the documented values not marked deprecated."""

    AUTOMATION = "AUTOMATION"
    UNEXPECTED_ENVIRONMENT = "UNEXPECTED_ENVIRONMENT"
    TOO_MUCH_TRAFFIC = "TOO_MUCH_TRAFFIC"
    UNEXPECTED_USAGE_PATTERNS = "UNEXPECTED_USAGE_PATTERNS"
    LOW_CONFIDENCE_SCORE = "LOW_CONFIDENCE_SCORE"


class Wire(Model):
    """A body as Google reads it: unknown fields refused, and either spelling of a field's name accepted, as proto3
    JSON parsers accept "both the lowerCamelCase name and the proto field name"
    (https://protobuf.dev/programming-guides/json/)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


ECHOED = (
    "token",
    "siteKey",
    "userAgent",
    "userIpAddress",
    "expectedAction",
    "hashedAccountId",
    "requestedUri",
    "ja3",
    "ja4",
)
"""The string fields of an `Event` echoed in the answer, in the order the reference lists them."""


def _both(camel: str, snake: str) -> AliasChoices:
    return AliasChoices(camel, snake)


class EventIn(Wire):
    """`Event`, as `assessments.create` reads it. The fields that only feed Google's model are kept and echoed; the
    ones that ask for an answer beyond a token's verdict are refused by name (`provider.REFUSED_EVENT_FIELDS`)."""

    token: str = ""
    siteKey: str = Field(default="", validation_alias=_both("siteKey", "site_key"))
    userAgent: str = Field(default="", validation_alias=_both("userAgent", "user_agent"))
    userIpAddress: str = Field(default="", validation_alias=_both("userIpAddress", "user_ip_address"))
    expectedAction: str = Field(default="", validation_alias=_both("expectedAction", "expected_action"))
    hashedAccountId: str = Field(default="", validation_alias=_both("hashedAccountId", "hashed_account_id"))
    requestedUri: str = Field(default="", validation_alias=_both("requestedUri", "requested_uri"))
    ja3: str = ""
    ja4: str = ""
    headers: list[str] = []
    express: bool = False
    wafTokenAssessment: bool = Field(
        default=False, validation_alias=_both("wafTokenAssessment", "waf_token_assessment")
    )
    firewallPolicyEvaluation: bool = Field(
        default=False, validation_alias=_both("firewallPolicyEvaluation", "firewall_policy_evaluation")
    )
    transactionData: JsonValue = Field(default=None, validation_alias=_both("transactionData", "transaction_data"))
    userInfo: JsonValue = Field(default=None, validation_alias=_both("userInfo", "user_info"))
    fraudPrevention: str = Field(default="", validation_alias=_both("fraudPrevention", "fraud_prevention"))

    def echoed(self) -> dict[str, JsonValue]:
        """The event as the answer carries it: lowerCamelCase, and a field at its default value left out."""
        found: dict[str, JsonValue] = {}
        for name in ECHOED:
            value = getattr(self, name)
            if value:
                found[name] = value
        if self.headers:
            found["headers"] = list(self.headers)
        return found


class AssessmentIn(Wire):
    """`Assessment`, as `assessments.create` reads it. Output-only fields are accepted and ignored, as Google ignores
    them on input; the input fields that ask for more than a token's verdict are refused by name."""

    name: str = ""
    event: EventIn | None = None
    assessmentEnvironment: JsonValue = Field(
        default=None, validation_alias=_both("assessmentEnvironment", "assessment_environment")
    )
    accountVerification: JsonValue = Field(
        default=None, validation_alias=_both("accountVerification", "account_verification")
    )
    privatePasswordLeakVerification: JsonValue = Field(
        default=None, validation_alias=_both("privatePasswordLeakVerification", "private_password_leak_verification")
    )
    riskAnalysis: JsonValue = Field(default=None, validation_alias=_both("riskAnalysis", "risk_analysis"))
    tokenProperties: JsonValue = Field(default=None, validation_alias=_both("tokenProperties", "token_properties"))
    accountDefenderAssessment: JsonValue = Field(
        default=None, validation_alias=_both("accountDefenderAssessment", "account_defender_assessment")
    )
    firewallPolicyAssessment: JsonValue = Field(
        default=None, validation_alias=_both("firewallPolicyAssessment", "firewall_policy_assessment")
    )
    fraudPreventionAssessment: JsonValue = Field(
        default=None, validation_alias=_both("fraudPreventionAssessment", "fraud_prevention_assessment")
    )
    fraudSignals: JsonValue = Field(default=None, validation_alias=_both("fraudSignals", "fraud_signals"))
    phoneFraudAssessment: JsonValue = Field(
        default=None, validation_alias=_both("phoneFraudAssessment", "phone_fraud_assessment")
    )
    policyEvaluation: JsonValue = Field(default=None, validation_alias=_both("policyEvaluation", "policy_evaluation"))


class TokenPropertiesOut(Model):
    valid: bool
    invalid_reason: InvalidReason | None
    create_time: datetime | None
    hostname: str
    action: str

    def wire(self) -> dict[str, JsonValue]:
        """proto3 JSON: a field at its default value (false, the empty string, an unset enum) is left out."""
        found: dict[str, JsonValue] = {}
        if self.valid:
            found["valid"] = True
        if self.invalid_reason is not None:
            found["invalidReason"] = self.invalid_reason.value
        if self.create_time is not None:
            found["createTime"] = stamp(self.create_time)
        if self.hostname:
            found["hostname"] = self.hostname
        if self.action:
            found["action"] = self.action
        return found


def risk_wire(score: float, reasons: list[ClassificationReason]) -> dict[str, JsonValue]:
    """`RiskAnalysis` in proto3 JSON: a score of 0 and no reasons are left out."""
    found: dict[str, JsonValue] = {}
    if score:
        found["score"] = score
    if reasons:
        found["reasons"] = [r.value for r in reasons]
    return found


def assessment_answer(
    name: str, event: EventIn, properties: TokenPropertiesOut, score: float, reasons: list[ClassificationReason]
) -> str:
    """The `Assessment` `assessments.create` answers, as proto3 JSON text: its name, the event echoed, the risk
    analysis and the token's properties (https://cloud.google.com/recaptcha/docs/interpret-assessment-website)."""
    answer: dict[str, JsonValue] = {
        "name": name,
        "event": event.echoed(),
        "riskAnalysis": risk_wire(score, reasons),
        "tokenProperties": properties.wire(),
    }
    return json.dumps(answer)


def stamp(when: datetime) -> str:
    """A Timestamp as proto3 JSON writes one: "Z-normalized", with 0, 3, 6 or 9 fractional digits."""
    moment = when.astimezone(UTC)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


_STATUS_WORD = {400: "INVALID_ARGUMENT", 404: "NOT_FOUND", 500: "INTERNAL", 501: "UNIMPLEMENTED"}


class ErrorBody(Model):
    code: int
    message: str
    status: str


class GoogleError(Model):
    error: ErrorBody


class RecaptchaRefusal(ServiceRefusal):
    """reCAPTCHA Enterprise would refuse this request: its status and message, in Google's error envelope."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def render(self, asked: Asked) -> Rendered:
        return error_answer(self.code, _STATUS_WORD[self.code], self.message)


def error_answer(status: int, word: str, message: str) -> Rendered:
    """Google's error envelope (https://cloud.google.com/apis/design/errors#http_mapping), which every Google client
    reads its error type from."""
    body = GoogleError(error=ErrorBody(code=status, message=message, status=word))
    return Rendered(status=status, content_type=JSON, body=body.model_dump_json().encode())


# --- the API's surface ------------------------------------------------------------------------------------------

_P = "/v1/projects/[^/:]+"
REST_METHODS: dict[str, tuple[str, re.Pattern[str]]] = {
    "recaptchaenterprise.projects.assessments.annotate": ("POST", re.compile(_P + "/assessments/[^/:]+:annotate")),
    "recaptchaenterprise.projects.assessments.create": ("POST", re.compile(_P + "/assessments")),
    "recaptchaenterprise.projects.firewallpolicies.create": ("POST", re.compile(_P + "/firewallpolicies")),
    "recaptchaenterprise.projects.firewallpolicies.delete": ("DELETE", re.compile(_P + "/firewallpolicies/[^/:]+")),
    "recaptchaenterprise.projects.firewallpolicies.get": ("GET", re.compile(_P + "/firewallpolicies/[^/:]+")),
    "recaptchaenterprise.projects.firewallpolicies.list": ("GET", re.compile(_P + "/firewallpolicies")),
    "recaptchaenterprise.projects.firewallpolicies.patch": ("PATCH", re.compile(_P + "/firewallpolicies/[^/:]+")),
    "recaptchaenterprise.projects.firewallpolicies.reorder": ("POST", re.compile(_P + "/firewallpolicies:reorder")),
    "recaptchaenterprise.projects.keys.addIpOverride": ("POST", re.compile(_P + "/keys/[^/:]+:addIpOverride")),
    "recaptchaenterprise.projects.keys.create": ("POST", re.compile(_P + "/keys")),
    "recaptchaenterprise.projects.keys.delete": ("DELETE", re.compile(_P + "/keys/[^/:]+")),
    "recaptchaenterprise.projects.keys.get": ("GET", re.compile(_P + "/keys/[^/:]+")),
    "recaptchaenterprise.projects.keys.getMetrics": ("GET", re.compile(_P + "/keys/[^/:]+/metrics")),
    "recaptchaenterprise.projects.keys.getPolicy": ("GET", re.compile(_P + "/keys/[^/:]+/policy")),
    "recaptchaenterprise.projects.keys.list": ("GET", re.compile(_P + "/keys")),
    "recaptchaenterprise.projects.keys.listIpOverrides": ("GET", re.compile(_P + "/keys/[^/:]+:listIpOverrides")),
    "recaptchaenterprise.projects.keys.migrate": ("POST", re.compile(_P + "/keys/[^/:]+:migrate")),
    "recaptchaenterprise.projects.keys.patch": ("PATCH", re.compile(_P + "/keys/[^/:]+")),
    "recaptchaenterprise.projects.keys.removeIpOverride": ("POST", re.compile(_P + "/keys/[^/:]+:removeIpOverride")),
    "recaptchaenterprise.projects.keys.retrieveLegacySecretKey": (
        "GET",
        re.compile(_P + "/keys/[^/:]+:retrieveLegacySecretKey"),
    ),
    "recaptchaenterprise.projects.keys.updatePolicy": ("PATCH", re.compile(_P + "/keys/[^/:]+/policy")),
    "recaptchaenterprise.projects.relatedaccountgroupmemberships.search": (
        "POST",
        re.compile(_P + "/relatedaccountgroupmemberships:search"),
    ),
    "recaptchaenterprise.projects.relatedaccountgroups.list": ("GET", re.compile(_P + "/relatedaccountgroups")),
    "recaptchaenterprise.projects.relatedaccountgroups.memberships.list": (
        "GET",
        re.compile(_P + "/relatedaccountgroups/[^/:]+/memberships"),
    ),
}
"""Every method of reCAPTCHA Enterprise's discovery document (v1), by its `httpMethod` and `flatPath`."""

REST_SERVED = frozenset({"recaptchaenterprise.projects.assessments.create"})

_KEYS = "site keys are not served: a world's tokens are seeded with their verdicts, whatever key minted them"
_FIREWALL = "firewall policies are not served"
_ACCOUNTS = "account defender's related account groups are not served"

REST_REFUSED: dict[str, str] = {
    "recaptchaenterprise.projects.assessments.annotate": "annotating an assessment is not served",
    "recaptchaenterprise.projects.firewallpolicies.create": _FIREWALL,
    "recaptchaenterprise.projects.firewallpolicies.delete": _FIREWALL,
    "recaptchaenterprise.projects.firewallpolicies.get": _FIREWALL,
    "recaptchaenterprise.projects.firewallpolicies.list": _FIREWALL,
    "recaptchaenterprise.projects.firewallpolicies.patch": _FIREWALL,
    "recaptchaenterprise.projects.firewallpolicies.reorder": _FIREWALL,
    "recaptchaenterprise.projects.keys.addIpOverride": _KEYS,
    "recaptchaenterprise.projects.keys.create": _KEYS,
    "recaptchaenterprise.projects.keys.delete": _KEYS,
    "recaptchaenterprise.projects.keys.get": _KEYS,
    "recaptchaenterprise.projects.keys.getMetrics": _KEYS,
    "recaptchaenterprise.projects.keys.getPolicy": _KEYS,
    "recaptchaenterprise.projects.keys.list": _KEYS,
    "recaptchaenterprise.projects.keys.listIpOverrides": _KEYS,
    "recaptchaenterprise.projects.keys.migrate": _KEYS,
    "recaptchaenterprise.projects.keys.patch": _KEYS,
    "recaptchaenterprise.projects.keys.removeIpOverride": _KEYS,
    "recaptchaenterprise.projects.keys.retrieveLegacySecretKey": _KEYS,
    "recaptchaenterprise.projects.keys.updatePolicy": _KEYS,
    "recaptchaenterprise.projects.relatedaccountgroupmemberships.search": _ACCOUNTS,
    "recaptchaenterprise.projects.relatedaccountgroups.list": _ACCOUNTS,
    "recaptchaenterprise.projects.relatedaccountgroups.memberships.list": _ACCOUNTS,
}
"""Why each REST method not served is refused."""


def rest_method(method: str, path: str) -> str | None:
    """The discovery method id a REST request calls, or None when no method has that route."""
    return next((m for m, (verb, route) in REST_METHODS.items() if verb == method and route.fullmatch(path)), None)
