"""AWS's own formats, read here and nowhere else.

Four things cross from AWS's wire into the provider: which operation of which service a request calls (the
surface is botocore's service models for `scheduler` and `sqs`; every operation of both is either served or
refused by name, and every other AWS service is refused), an EventBridge Scheduler request (rest-json over
`scheduler.<region>.amazonaws.com/schedules/<name>`), the schedule expression inside it (`at(...)`, `rate(...)`,
`cron(...)`), and the ARN of the schedule's target. Each is parsed into a typed model, or refused. Where each rule
comes from is in `CLAIMS.md`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import parse_qs, unquote
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, ValidationError

from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model


class Refusal(ServiceRefusal):
    """A request AWS itself would refuse. Answered as AWS answers it, without reaching moto."""

    def __init__(self, error_type: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.status = status

    def answer(self, query_protocol: bool) -> tuple[list[tuple[bytes, bytes]], bytes]:
        """The headers and body AWS refuses with: JSON (`x-amzn-errortype`, `{"__type": …, "message": …}`) for the JSON
        protocols, and the query protocol's `ErrorResponse` XML for a form-encoded request (STS, SQS's older
        protocol), each of which botocore raises as `ClientError` with `error_type` as its code."""
        if not query_protocol:
            return [
                (b"x-amzn-errortype", self.error_type.encode()),
                (b"content-type", b"application/json"),
            ], json.dumps({"__type": self.error_type, "message": self.message}).encode()
        body = (
            f"<ErrorResponse><Error><Type>Sender</Type><Code>{xml_escape(self.error_type)}</Code>"
            f"<Message>{xml_escape(self.message)}</Message></Error></ErrorResponse>"
        )
        return [(b"content-type", b"text/xml")], body.encode()

    def render(self, asked: Asked) -> Rendered:
        """AWS's JSON error: the type in `x-amzn-errortype`, `{"message": …}` in the body."""
        return error(self.status, self.error_type, self.message)


def error(status: int, error_type: str, message: str) -> Rendered:
    """An error as AWS's JSON protocols answer one, which botocore raises as `ClientError` with `error_type` as its
    code and `message` as its message."""
    return Rendered(
        status=status,
        content_type="application/json",
        body=json.dumps({"__type": error_type, "message": message}).encode(),
        headers=[("x-amzn-errortype", error_type)],
    )


class NotImplementedByProvider(Refusal):
    """Something real AWS accepts that this provider does not reproduce. Loud, never a silent approximation."""

    def __init__(self, message: str) -> None:
        super().__init__("NotImplemented", f"minutehand's aws provider does not implement {message}", status=501)


# --- which operation a request calls --------------------------------------------------------------


class Service(StrEnum):
    """The AWS services this provider answers: botocore's `scheduler` (rest-json) and `sqs` (json, and the older
    query protocol)."""

    SCHEDULER = "scheduler"
    SQS = "sqs"


SCHEDULER_ROUTES: dict[str, tuple[str, re.Pattern[str]]] = {
    "CreateSchedule": ("POST", re.compile(r"/schedules/[^/]+")),
    "CreateScheduleGroup": ("POST", re.compile(r"/schedule-groups/[^/]+")),
    "DeleteSchedule": ("DELETE", re.compile(r"/schedules/[^/]+")),
    "DeleteScheduleGroup": ("DELETE", re.compile(r"/schedule-groups/[^/]+")),
    "GetSchedule": ("GET", re.compile(r"/schedules/[^/]+")),
    "GetScheduleGroup": ("GET", re.compile(r"/schedule-groups/[^/]+")),
    "ListScheduleGroups": ("GET", re.compile(r"/schedule-groups")),
    "ListSchedules": ("GET", re.compile(r"/schedules")),
    "ListTagsForResource": ("GET", re.compile(r"/tags/.+")),
    "TagResource": ("POST", re.compile(r"/tags/.+")),
    "UntagResource": ("DELETE", re.compile(r"/tags/.+")),
    "UpdateSchedule": ("PUT", re.compile(r"/schedules/[^/]+")),
}
"""Every operation of botocore's `scheduler` model (2021-06-30), by its `http.method` and `http.requestUri`."""

SQS_OPERATIONS = frozenset(
    {
        "AddPermission",
        "CancelMessageMoveTask",
        "ChangeMessageVisibility",
        "ChangeMessageVisibilityBatch",
        "CreateQueue",
        "DeleteMessage",
        "DeleteMessageBatch",
        "DeleteQueue",
        "GetQueueAttributes",
        "GetQueueUrl",
        "ListDeadLetterSourceQueues",
        "ListMessageMoveTasks",
        "ListQueueTags",
        "ListQueues",
        "PurgeQueue",
        "ReceiveMessage",
        "RemovePermission",
        "SendMessage",
        "SendMessageBatch",
        "SetQueueAttributes",
        "StartMessageMoveTask",
        "TagQueue",
        "UntagQueue",
    }
)
"""Every operation of botocore's `sqs` model (2012-11-05)."""

SERVED: dict[Service, frozenset[str]] = {
    Service.SCHEDULER: frozenset(
        {"CreateSchedule", "UpdateSchedule", "DeleteSchedule", "GetSchedule", "ListSchedules"}
    ),
    Service.SQS: frozenset(
        {
            "CreateQueue",
            "GetQueueUrl",
            "GetQueueAttributes",
            "ListQueues",
            "SendMessage",
            "ReceiveMessage",
            "DeleteMessage",
            "DeleteMessageBatch",
            "ChangeMessageVisibility",
        }
    ),
}
"""What is answered, by moto made faithful where `CLAIMS.md` says; every other operation of either model is refused
501 `NotImplemented`, naming it."""

REFUSED_BECAUSE: dict[str, str] = {
    "CreateScheduleGroup": "schedule groups other than `default` are not served",
    "DeleteScheduleGroup": "schedule groups other than `default` are not served",
    "GetScheduleGroup": "schedule groups other than `default` are not served",
    "ListScheduleGroups": "schedule groups other than `default` are not served",
    "ListTagsForResource": "tags are not served",
    "TagResource": "tags are not served",
    "UntagResource": "tags are not served",
    "DeleteQueue": "a deleted queue would take a delivered message the run is waiting on with it",
    "PurgeQueue": "a purge would take a delivered message the run is waiting on with it",
    "SetQueueAttributes": "queue attributes are set only when the queue is created",
    "SendMessageBatch": "batch sends are not served",
    "ChangeMessageVisibilityBatch": "batch visibility changes are not served",
    "AddPermission": "queue permissions are not served (Minutehand enforces no credentials)",
    "RemovePermission": "queue permissions are not served (Minutehand enforces no credentials)",
    "ListQueueTags": "tags are not served",
    "TagQueue": "tags are not served",
    "UntagQueue": "tags are not served",
    "ListDeadLetterSourceQueues": "dead-letter source listing is not served",
    "StartMessageMoveTask": "message move tasks are not served",
    "CancelMessageMoveTask": "message move tasks are not served",
    "ListMessageMoveTasks": "message move tasks are not served",
}

_SERVICE_NAMES = {Service.SCHEDULER: "EventBridge Scheduler", Service.SQS: "SQS"}


class AwsCall(Model):
    """The service, region and operation a request calls."""

    service: Service
    region: str
    operation: str


def aws_call(method: str, host: str, path: str, query: str, target: str, body: bytes) -> AwsCall:
    """The operation this request calls, or a `NotImplementedByProvider` naming what it asked for instead: another
    AWS service, an operation this provider does not serve, or moto's own management API (`/moto-api`), which is
    no part of AWS."""
    if path.startswith("/moto-api"):
        raise NotImplementedByProvider(f"{path}: moto's management API is no part of AWS")
    if (on_scheduler := _SCHEDULER_HOST.fullmatch(host)) is not None:
        region = on_scheduler.group(1)
        found = next((op for op, (m, p) in SCHEDULER_ROUTES.items() if m == method and p.fullmatch(path)), None)
        if found is None:
            raise NotImplementedByProvider(f"EventBridge Scheduler {method} {path}: no operation has that route")
        return _served(AwsCall(service=Service.SCHEDULER, region=region, operation=found))
    region = _sqs_region(host)
    if region is None:
        raise NotImplementedByProvider(f"{host}: only EventBridge Scheduler and SQS are served")
    operation = target.removeprefix("AmazonSQS.") if target.startswith("AmazonSQS.") else _query_action(query, body)
    if operation not in SQS_OPERATIONS:
        raise NotImplementedByProvider(f"SQS {operation or 'a request naming no operation'}")
    return _served(AwsCall(service=Service.SQS, region=region, operation=operation))


def _served(call: AwsCall) -> AwsCall:
    if call.operation not in SERVED[call.service]:
        why = REFUSED_BECAUSE[call.operation]
        raise NotImplementedByProvider(f"{_SERVICE_NAMES[call.service]} {call.operation}: {why}")
    return call


def _query_action(query: str, body: bytes) -> str:
    """SQS's query protocol names its operation in `Action`, in the form body or the query string."""
    for source in (body.decode("utf-8", errors="replace"), query):
        found = parse_qs(source)
        if "Action" in found:
            return found["Action"][0]
    return ""


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

    action_after_completion: ActionAfterCompletion | None = Field(default=None, alias="ActionAfterCompletion")
    client_token: str | None = Field(default=None, alias="ClientToken")
    description: str | None = Field(default=None, alias="Description")
    end_date: AwareDatetime | None = Field(default=None, alias="EndDate")
    flexible_time_window: FlexibleTimeWindow = Field(alias="FlexibleTimeWindow")
    group_name: str | None = Field(default=None, alias="GroupName")
    kms_key_arn: str | None = Field(default=None, alias="KmsKeyArn")
    schedule_expression: str = Field(alias="ScheduleExpression")
    schedule_expression_timezone: str | None = Field(default=None, alias="ScheduleExpressionTimezone")
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


DEFAULT_GROUP = "default"
NOT_FOUND = "The request references a resource which does not exist."
"""`ResourceNotFoundException`'s description in each EventBridge Scheduler operation's Errors section."""


def schedule_call(method: str, host: str, path: str, query: str, body: bytes) -> ScheduleCall | None:
    """The schedule change this request asks for, or None when it changes no schedule.

    A schedule in a group other than `default` is refused `ResourceNotFoundException`: no other group can exist,
    since creating one is refused (`REFUSED_BECAUSE`)."""
    on_host = _SCHEDULER_HOST.fullmatch(host)
    on_path = _SCHEDULE_PATH.fullmatch(path)
    if on_host is None or on_path is None:
        return None
    region, name = on_host.group(1), unquote(on_path.group(1))
    if method in ("GET", "DELETE"):
        group = parse_qs(query).get("groupName", [DEFAULT_GROUP])[0]
        if group != DEFAULT_GROUP:
            raise Refusal("ResourceNotFoundException", NOT_FOUND, status=404)
        return ScheduleCall(kind=CallKind.DELETE, region=region, group=group, name=name) if method == "DELETE" else None
    if method not in ("POST", "PUT"):
        return None
    try:
        request = ScheduleRequest.model_validate_json(body)
    except ValidationError as e:
        raise Refusal("ValidationException", f"invalid request: {e.errors(include_url=False)}") from e
    if (request.group_name or DEFAULT_GROUP) != DEFAULT_GROUP:
        raise Refusal("ResourceNotFoundException", NOT_FOUND, status=404)
    if request.schedule_expression_timezone is not None:
        timezone_of(request.schedule_expression_timezone)
    expression(request.schedule_expression)
    window = request.flexible_time_window
    if window.mode is FlexibleWindowMode.FLEXIBLE and window.maximum_window_in_minutes is None:
        raise Refusal(
            "ValidationException",
            "FlexibleTimeWindow.Mode FLEXIBLE needs MaximumWindowInMinutes: if you do set the value to FLEXIBLE, "
            "you must then specify a maximum window of time during which your schedule will run.",
        )
    if window.maximum_window_in_minutes is not None and not 1 <= window.maximum_window_in_minutes <= 1440:
        raise Refusal("ValidationException", "FlexibleTimeWindow.MaximumWindowInMinutes: valid range 1 to 1440.")
    kind = CallKind.CREATE if method == "POST" else CallKind.UPDATE
    return ScheduleCall(kind=kind, region=region, group=DEFAULT_GROUP, name=name, request=request)


def deliverable(request: ScheduleRequest, account: str) -> SqsQueue:
    """The SQS queue in this run's account that the schedule's target names, or a refusal naming what is not
    served: a target that is not an SQS queue, a queue in another account (another run's), or no `Input` (AWS then
    "delivers a default notification to the target", whose form no reference gives)."""
    target = request.target
    try:
        queue = sqs_target(target.arn)
    except UnsupportedTarget as e:
        raise NotImplementedByProvider(f"delivery to {e.arn}: only SQS queue targets are served") from e
    if queue.account != account:
        raise NotImplementedByProvider(
            f"delivery to {queue.arn}: a queue in another account than this run's ({account}) is not served"
        )
    if target.input is None:
        raise NotImplementedByProvider(
            "a schedule with no Target.Input: AWS then delivers a default notification whose form is not documented"
        )
    return queue


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
        # "a value as a positive integer, and a unit with the following options: minute | minutes | hour | hours |
        # day | days" (CreateSchedule, ScheduleExpression): any unit with any positive value.
        value, unit = int(m.group(1)), m.group(2)
        if value < 1:
            raise Refusal("ValidationException", f"Invalid Schedule Expression {text}.")
        return Rate(every=value * _UNITS[unit.rstrip("s")])
    if (m := _CRON.fullmatch(text)) is not None:
        return _cron(text, m.group(1).split())
    raise Refusal("ValidationException", f"Invalid Schedule Expression {text}.")


def _cron(text: str, fields: list[str]) -> Cron:
    if len(fields) != 6:
        raise Refusal("ValidationException", f"Invalid Schedule Expression {text}.")
    minute, hour, dom, month, dow, year = fields
    if dom == "?" and dow == "?":
        raise NotImplementedByProvider(f"'?' in both day-of-month and day-of-week (in {text}): no reference says")
    if dom != "?" and dow != "?":
        if "*" in (dom, dow):
            raise Refusal(
                "ValidationException",
                f"Invalid Schedule Expression {text}: you can't use * in both the Day-of-month and Day-of-week "
                "fields. If you use it in one, you must use ? in the other.",
            )
        raise NotImplementedByProvider(f"values in both day-of-month and day-of-week (in {text}): no reference says")
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


class SqsDelete(Model):
    """A DeleteMessage or DeleteMessageBatch: the agent saying it is done with these messages."""

    region: str
    queue: str
    receipt_handles: list[str]


class _QueueUrl(_Wire):
    queue_url: str = Field(alias="QueueUrl")


class _DeleteMessage(_QueueUrl):
    receipt_handle: str = Field(alias="ReceiptHandle")


class _DeleteEntry(_Wire):
    id: str = Field(alias="Id")
    receipt_handle: str = Field(alias="ReceiptHandle")


class _DeleteMessageBatch(_QueueUrl):
    entries: list[_DeleteEntry] = Field(alias="Entries")


_SQS_HOST = re.compile(r"(?:sqs\.([a-z0-9-]+)|([a-z0-9-]+)\.queue|queue)\.amazonaws\.com(?::\d+)?")
_BATCH_HANDLE = re.compile(r"DeleteMessageBatchRequestEntry\.\d+\.ReceiptHandle")
_DELETE_TARGETS = ("AmazonSQS.DeleteMessage", "AmazonSQS.DeleteMessageBatch")


def sqs_delete(host: str, target: str, body: bytes) -> SqsDelete | None:
    """The messages a request to SQS deletes, or None when it deletes none.

    Both of SQS's protocols are read: JSON (`X-Amz-Target: AmazonSQS.DeleteMessage`, what boto3 sends) and the
    older query protocol (`Action=DeleteMessage` in a form body, what older SDKs send). A body neither reads is
    left to moto, which answers it as AWS would."""
    if _sqs_region(host) is None:
        return None
    if target in _DELETE_TARGETS:
        try:
            if target == _DELETE_TARGETS[0]:
                single = _DeleteMessage.model_validate_json(body)
                return _deleting(host, single.queue_url, [single.receipt_handle])
            batch = _DeleteMessageBatch.model_validate_json(body)
            return _deleting(host, batch.queue_url, [e.receipt_handle for e in batch.entries])
        except ValidationError:
            return None
    form = parse_qs(body.decode("utf-8", errors="replace"))
    action = form["Action"][0] if "Action" in form else None
    if "QueueUrl" not in form:
        return None
    url = form["QueueUrl"][0]
    if action == "DeleteMessage" and "ReceiptHandle" in form:
        return _deleting(host, url, form["ReceiptHandle"])
    if action == "DeleteMessageBatch":
        return _deleting(host, url, [v for k, vs in form.items() if _BATCH_HANDLE.fullmatch(k) for v in vs])
    return None


def _sqs_region(host: str) -> str | None:
    m = _SQS_HOST.fullmatch(host)
    if m is None:
        return None
    return m.group(1) or m.group(2) or "us-east-1"


def _deleting(host: str, queue_url: str, handles: list[str]) -> SqsDelete | None:
    """The queue a QueueUrl names (its last path segment), in the region its own host names, else the request's."""
    url_host, _, path = queue_url.split("://", 1)[-1].partition("/")
    region = _sqs_region(url_host) or _sqs_region(host)
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if region is None or not name or not handles:
        return None
    return SqsDelete(region=region, queue=name, receipt_handles=handles)


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


# --- SQS queue attributes, as sent, and what moto adds to a received message ---------------------------------


class QueueNamed(Model):
    """The queue a CreateQueue or GetQueueAttributes names, in its region."""

    region: str
    queue: str


class QueueCreated(QueueNamed):
    """A CreateQueue: the queue's name and the attributes the caller set, as it wrote them."""

    attributes: dict[str, str]


class _CreateQueue(_Wire):
    queue_name: str = Field(alias="QueueName")
    attributes: dict[str, str] = Field(default={}, alias="Attributes")
    tags: dict[str, str] = Field(default={}, alias="tags")


class _GetQueueAttributes(_QueueUrl):
    attribute_names: list[str] = Field(default=[], alias="AttributeNames")


_FORM_ATTRIBUTE = re.compile(r"Attribute\.(\d+)\.(Name|Value)")
_FORM_ATTRIBUTE_NAME = re.compile(r"AttributeName\.\d+")


def _form(body: bytes) -> dict[str, list[str]]:
    return parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)


def queue_created(host: str, target: str, body: bytes) -> QueueCreated:
    """The CreateQueue this request is (JSON or query protocol)."""
    region = _sqs_region(host) or "us-east-1"
    if target:
        try:
            asked = _CreateQueue.model_validate_json(body)
        except ValidationError as e:
            raise Refusal("InvalidParameterValue", f"invalid CreateQueue: {e.errors(include_url=False)}") from e
        return QueueCreated(region=region, queue=asked.queue_name, attributes=asked.attributes)
    form = _form(body)
    pairs: dict[str, dict[str, str]] = {}
    for key, values in form.items():
        if (m := _FORM_ATTRIBUTE.fullmatch(key)) is not None:
            pairs.setdefault(m.group(1), {})[m.group(2)] = values[0]
    attributes = {p["Name"]: p["Value"] for p in pairs.values() if "Name" in p and "Value" in p}
    return QueueCreated(region=region, queue=form["QueueName"][0] if "QueueName" in form else "", attributes=attributes)


def attributes_asked(host: str, target: str, body: bytes) -> tuple[QueueNamed, list[str]] | None:
    """The queue a GetQueueAttributes reads, and the attribute names it asks for, or None when unreadable (moto
    answers that as AWS would)."""
    if target:
        try:
            asked = _GetQueueAttributes.model_validate_json(body)
        except ValidationError:
            return None
        url, names = asked.queue_url, asked.attribute_names
    else:
        form = _form(body)
        if "QueueUrl" not in form:
            return None
        url = form["QueueUrl"][0]
        names = [v for k, vs in form.items() if _FORM_ATTRIBUTE_NAME.fullmatch(k) for v in vs]
    found = _deleting(host, url, ["-"])
    if found is None:
        return None
    return QueueNamed(region=found.region, queue=found.queue), names


def with_attributes_as_sent(answer: bytes, sent: dict[str, str], asked: list[str]) -> bytes:
    """GetQueueAttributes' answer with every attribute the caller set when it created the queue answered as it wrote
    it, where moto rewrites it (`RedrivePolicy` re-serialised, `maxReceiveCount` made a number) or drops it (a
    `Policy` with no `Statement`)."""
    wanted = {k: v for k, v in sent.items() if "All" in asked or k in asked}
    if not wanted:
        return answer
    if answer.lstrip().startswith(b"{"):
        parsed: JsonValue = json.loads(answer)
        if not isinstance(parsed, dict):
            return answer
        held = parsed["Attributes"] if "Attributes" in parsed else {}
        attributes: dict[str, JsonValue] = dict(held) if isinstance(held, dict) else {}
        attributes.update(wanted)
        parsed["Attributes"] = attributes
        return json.dumps(parsed).encode()
    text = answer.decode()
    for name, value in wanted.items():
        element = f"<Attribute><Name>{xml_escape(name)}</Name><Value>{xml_escape(value)}</Value></Attribute>"
        pattern = re.compile(
            rf"<Attribute>\s*<Name>{re.escape(xml_escape(name))}</Name>\s*<Value>.*?</Value>\s*</Attribute>", re.S
        )
        if pattern.search(text) is not None:
            found = pattern.search(text)
            assert found is not None
            text = text[: found.start()] + element + text[found.end() :]
        else:
            text = text.replace("</GetQueueAttributesResult>", element + "</GetQueueAttributesResult>", 1)
    return text.encode()


_SENDER_XML = re.compile(r"<Attribute>\s*<Name>SenderId</Name>\s*<Value>.*?</Value>\s*</Attribute>", re.S)


def without_sender_id(answer: bytes) -> bytes:
    """ReceiveMessage's answer without the `SenderId` moto makes up for every message (`AIDAIT2UOQQY3AUEKVGXU`):
    AWS answers the sending principal's id there, and Minutehand, which enforces no credentials, knows no
    principal."""
    if not answer.lstrip().startswith(b"{"):
        return _SENDER_XML.sub("", answer.decode()).encode()
    parsed: JsonValue = json.loads(answer)
    if not isinstance(parsed, dict) or "Messages" not in parsed or not isinstance(parsed["Messages"], list):
        return answer
    for message in parsed["Messages"]:
        if isinstance(message, dict) and "Attributes" in message and isinstance(message["Attributes"], dict):
            message["Attributes"] = {k: v for k, v in message["Attributes"].items() if k != "SenderId"}
    return json.dumps(parsed).encode()
