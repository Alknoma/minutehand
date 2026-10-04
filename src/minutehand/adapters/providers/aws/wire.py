"""AWS's own formats, read here and nowhere else.

Three things cross from AWS's wire into the provider: an EventBridge Scheduler
request (rest-json over `scheduler.<region>.amazonaws.com/schedules/<name>`), the
schedule expression inside it (`at(...)`, `rate(...)`, `cron(...)`), and the ARN
of the schedule's target. Each is parsed into a typed model, or refused.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import parse_qs, unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, ValidationError

from minutehand.domain.scenario import Model


class Refusal(Exception):
    """A request AWS itself would refuse. Answered as AWS answers it, without reaching moto."""

    def __init__(self, error_type: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.status = status

    def body(self) -> bytes:
        return json.dumps({"message": self.message}).encode()


class NotImplementedByProvider(Refusal):
    """Something real AWS accepts that this provider does not reproduce. Loud, never a silent approximation."""

    def __init__(self, message: str) -> None:
        super().__init__("NotImplemented", f"minutehand's aws provider does not implement {message}", status=501)


class _Wire(Model):
    """AWS's own field names; every documented field is declared, so an undocumented one is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ScheduleState(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class ActionAfterCompletion(StrEnum):
    NONE = "NONE"
    DELETE = "DELETE"


class FlexibleWindowMode(StrEnum):
    OFF = "OFF"
    FLEXIBLE = "FLEXIBLE"


class FlexibleTimeWindow(_Wire):
    mode: FlexibleWindowMode = Field(alias="Mode")
    maximum_window_in_minutes: int | None = Field(default=None, alias="MaximumWindowInMinutes")


class SqsParameters(_Wire):
    message_group_id: str | None = Field(default=None, alias="MessageGroupId")


class Target(_Wire):
    arn: str = Field(alias="Arn")
    role_arn: str = Field(alias="RoleArn")
    input: str | None = Field(default=None, alias="Input")
    sqs_parameters: SqsParameters | None = Field(default=None, alias="SqsParameters")
    # Declared so they are accepted; no target that reads them is implemented.
    dead_letter_config: JsonValue | None = Field(default=None, alias="DeadLetterConfig")
    ecs_parameters: JsonValue | None = Field(default=None, alias="EcsParameters")
    event_bridge_parameters: JsonValue | None = Field(default=None, alias="EventBridgeParameters")
    kinesis_parameters: JsonValue | None = Field(default=None, alias="KinesisParameters")
    retry_policy: JsonValue | None = Field(default=None, alias="RetryPolicy")
    sage_maker_pipeline_parameters: JsonValue | None = Field(default=None, alias="SageMakerPipelineParameters")


class ScheduleRequest(_Wire):
    """The body of CreateSchedule and of UpdateSchedule, which replaces the whole schedule."""

    action_after_completion: ActionAfterCompletion = Field(
        default=ActionAfterCompletion.NONE, alias="ActionAfterCompletion"
    )
    client_token: str | None = Field(default=None, alias="ClientToken")
    description: str | None = Field(default=None, alias="Description")
    end_date: AwareDatetime | None = Field(default=None, alias="EndDate")
    flexible_time_window: FlexibleTimeWindow = Field(alias="FlexibleTimeWindow")
    group_name: str | None = Field(default=None, alias="GroupName")
    kms_key_arn: str | None = Field(default=None, alias="KmsKeyArn")
    schedule_expression: str = Field(alias="ScheduleExpression")
    schedule_expression_timezone: str = Field(default="UTC", alias="ScheduleExpressionTimezone")
    start_date: AwareDatetime | None = Field(default=None, alias="StartDate")
    state: ScheduleState = Field(default=ScheduleState.ENABLED, alias="State")
    target: Target = Field(alias="Target")


class CallKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ScheduleCall(Model):
    """One call that changes a schedule. `request` is None for a delete."""

    kind: CallKind
    region: str
    group: str
    name: str
    request: ScheduleRequest | None = None


_SCHEDULER_HOST = re.compile(r"scheduler\.([a-z0-9-]+)\.amazonaws\.com(?::\d+)?")
_SCHEDULE_PATH = re.compile(r"/schedules/([^/?]+)")


def schedule_call(method: str, host: str, path: str, query: str, body: bytes) -> ScheduleCall | None:
    """The schedule change this request asks for, or None when it changes no schedule."""
    on_host = _SCHEDULER_HOST.fullmatch(host)
    on_path = _SCHEDULE_PATH.fullmatch(path)
    if on_host is None or on_path is None:
        return None
    region, name = on_host.group(1), unquote(on_path.group(1))
    if method == "DELETE":
        group = parse_qs(query).get("groupName", ["default"])[0]
        return ScheduleCall(kind=CallKind.DELETE, region=region, group=group, name=name)
    if method not in ("POST", "PUT"):
        return None
    try:
        request = ScheduleRequest.model_validate_json(body)
    except ValidationError as e:
        raise Refusal("ValidationException", f"invalid request: {e.errors(include_url=False)}") from e
    timezone_of(request.schedule_expression_timezone)
    expression(request.schedule_expression)
    kind = CallKind.CREATE if method == "POST" else CallKind.UPDATE
    return ScheduleCall(kind=kind, region=region, group=request.group_name or "default", name=name, request=request)


def schedule_arn(region: str, account: str, group: str, name: str) -> str:
    return f"arn:aws:scheduler:{region}:{account}:schedule/{group}/{name}"


def timezone_of(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise Refusal("ValidationException", f"Invalid ScheduleExpressionTimezone: {name}") from e


# --- schedule expressions -------------------------------------------------------------------


class At(Model):
    """A one-time schedule. `local` is a wall time in the schedule's timezone."""

    kind: Literal["at"] = "at"
    local: datetime


class Rate(Model):
    kind: Literal["rate"] = "rate"
    every: timedelta


class Cron(Model):
    """AWS's six-field cron. Days of week are AWS's 1-7 with 1 = Sunday; None is AWS's `?`."""

    kind: Literal["cron"] = "cron"
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int] | None
    months: frozenset[int]
    days_of_week: frozenset[int] | None
    years: frozenset[int]


Expression = Annotated[At | Rate | Cron, Field(discriminator="kind")]

_AT = re.compile(r"at\((\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\)")
_RATE = re.compile(r"rate\((\d+) (minute|minutes|hour|hours|day|days)\)")
_CRON = re.compile(r"cron\((.+)\)")
_UNITS = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1)}
_MONTHS = {
    m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])
}
_WEEKDAYS = {d: i + 1 for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}


def expression(text: str) -> At | Rate | Cron:
    """Parse a ScheduleExpression, refusing what AWS refuses."""
    if (m := _AT.fullmatch(text)) is not None:
        return At(local=datetime.fromisoformat(m.group(1)))
    if (m := _RATE.fullmatch(text)) is not None:
        value, unit = int(m.group(1)), m.group(2)
        if value < 1 or (value == 1) != (not unit.endswith("s")):
            raise Refusal("ValidationException", f"Invalid Schedule Expression {text}.")
        return Rate(every=value * _UNITS[unit.rstrip("s")])
    if (m := _CRON.fullmatch(text)) is not None:
        return _cron(text, m.group(1).split())
    raise Refusal("ValidationException", f"Invalid Schedule Expression {text}.")


def _cron(text: str, fields: list[str]) -> Cron:
    if len(fields) != 6:
        raise Refusal("ValidationException", f"Invalid Schedule Expression {text}.")
    minute, hour, dom, month, dow, year = fields
    if (dom == "?") == (dow == "?"):
        raise Refusal(
            "ValidationException",
            f"Invalid Schedule Expression {text}: exactly one of day-of-month and day-of-week must be '?'.",
        )
    return Cron(
        minutes=_field(text, minute, 0, 59, {}),
        hours=_field(text, hour, 0, 23, {}),
        days_of_month=None if dom == "?" else _field(text, dom, 1, 31, {}),
        months=_field(text, month, 1, 12, _MONTHS),
        days_of_week=None if dow == "?" else _field(text, dow, 1, 7, _WEEKDAYS),
        years=_field(text, year, 1970, 2199, {}),
    )


def _field(text: str, field: str, low: int, high: int, names: dict[str, int]) -> frozenset[int]:
    if re.search(r"[LW#]", field):
        raise NotImplementedByProvider(f"the L, W or # cron operators (in {text})")
    values: set[int] = set()
    for part in field.split(","):
        span, _, step_text = part.partition("/")
        step = _number(text, step_text, names) if step_text else 1
        if span == "*":
            first, last = low, high
        elif "-" in span:
            a, b = span.split("-", 1)
            first, last = _number(text, a, names), _number(text, b, names)
        else:
            first = _number(text, span, names)
            last = high if step_text else first
        if not (low <= first <= high and low <= last <= high and first <= last and step >= 1):
            raise Refusal("ValidationException", f"Invalid Schedule Expression {text}.")
        values.update(range(first, last + 1, step))
    return frozenset(values)


def _number(text: str, token: str, names: dict[str, int]) -> int:
    if token.upper() in names:
        return names[token.upper()]
    if not token.isdigit():
        raise Refusal("ValidationException", f"Invalid Schedule Expression {text}.")
    return int(token)


# --- target ARNs ----------------------------------------------------------------------------


class SqsQueue(Model):
    region: str
    account: str
    queue: str

    @property
    def arn(self) -> str:
        return f"arn:aws:sqs:{self.region}:{self.account}:{self.queue}"


class UnsupportedTarget(Exception):
    """The schedule's target is a service whose delivery is not implemented. Raised, never dropped."""

    def __init__(self, service: str, arn: str) -> None:
        super().__init__(f"cannot deliver to a {service} target ({arn}): only SQS targets are implemented")
        self.service = service
        self.arn = arn


_ARN = re.compile(r"arn:aws[a-z-]*:([a-z0-9-]+):([a-z0-9-]*):(\d*):(.+)")


def sqs_target(arn: str) -> SqsQueue:
    """The queue a target ARN names, or UnsupportedTarget naming the service it names instead."""
    m = _ARN.fullmatch(arn)
    if m is None:
        raise UnsupportedTarget("unrecognised", arn)
    service, region, account, resource = m.groups()
    if service != "sqs":
        raise UnsupportedTarget(resource.split(":")[0] if service == "scheduler" else service, arn)
    return SqsQueue(region=region, account=account, queue=resource)
