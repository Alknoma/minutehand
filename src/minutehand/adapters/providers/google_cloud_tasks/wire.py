"""Cloud Tasks v2 as its REST API puts it on the wire: proto3 JSON, field names in camelCase, durations as
`"3.5s"`, timestamps as RFC 3339, bytes as base64, and Google's error envelope.

A gRPC request is read in the same shapes, as the proto3 JSON its message prints to: the REST call carries the same
fields with the resource names in its path, the gRPC request carries them whole (`Named`, `Parented`, ...).

https://cloud.google.com/tasks/docs/reference/rest
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from pydantic import ConfigDict, Field, JsonValue

from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model
from minutehand.domain.world import GrpcCode

JSON = "application/json; charset=UTF-8"

DOCS = "https://docs.cloud.google.com/tasks/docs"

DEFAULT_MIN_BACKOFF = timedelta(seconds=0.1)
DEFAULT_MAX_BACKOFF = timedelta(hours=1)
DEFAULT_MAX_DOUBLINGS = 16
DEFAULT_MAX_ATTEMPTS = 100
DEFAULT_MAX_DISPATCHES_PER_SECOND = 500.0
DEFAULT_MAX_CONCURRENT_DISPATCHES = 1000
DEFAULT_MAX_BURST_SIZE = 100
"""A queue's defaults: "If unspecified when the queue is created, Cloud Tasks will pick the default"
(RetryConfig, RateLimits), and the values it picks are the ones `gcloud tasks queues describe` shows for a queue
created with none (Configure Cloud Tasks queues, `https://docs.cloud.google.com/tasks/docs/configuring-queues`)."""
DEFAULT_DISPATCH_DEADLINE = timedelta(minutes=10)
DISPATCH_DEADLINE_RANGE = (timedelta(seconds=15), timedelta(minutes=30))
"""HTTP tasks: "the default is 10 minutes. The deadline must be in the interval [15 seconds, 30 minutes]" (Task)."""
NAME_REUSE_AFTER = timedelta(hours=24)
"""How long a task's name stays taken once the task was deleted or ran: "It can take up to 24 hours ... for the task
ID to be released and made available again" (tasks.create). The reference gives only the bound; it is held."""
QUEUE_TOMBSTONE = timedelta(days=3)
"""After queues.delete, "you may be prevented from creating a new queue with the same name ... for a tombstone
window of up to 3 days. During this window, the queues.create operation may appear to recreate the queue"
(queues.delete): what create does then is not said, so it is refused by name for those 3 days."""
TASK_RETENTION = timedelta(days=31)
"""Maximum task retention (Quotas and limits): a task retried with unlimited attempts and duration is given up then."""
MAX_QUEUE_PAGE = 9800
MAX_TASK_PAGE = 1000
"""The largest page queues.list and tasks.list answer; "Fewer ... than requested might be returned" (each method)."""

# Each refusal's message is the reference's own sentence for the rule it refuses by (its page beside it): Google's
# wording of these errors is in no reference, and a client acts on the status, which the reference does give.
QUEUE_MUST_EXIST = "The queue must already exist."
"""CreateTaskRequest.parent, ListTasksRequest.parent (RPC reference, `https://docs.cloud.google.com/tasks/docs/reference/rpc/google.cloud.tasks.v2`)."""
NOT_FOUND = "the requested resource or parent does not exist"
"""AIP-193 (https://google.aip.dev/193): "the service must error with NOT_FOUND (HTTP 404)"."""
TASK_EXISTS = (
    "If a task's ID is identical to that of an existing task or a task that was deleted or executed recently then "
    "the call will fail with ALREADY_EXISTS."
)
"""tasks.create."""
QUEUE_EXISTS = (
    "If the user tries to create a resource with an ID that would result in a duplicate resource name, the service "
    "must error with ALREADY_EXISTS."
)
"""AIP-133 (https://google.aip.dev/133)."""
INVALID_ARGUMENT = "The client specified an invalid argument."
"""google.rpc.Code INVALID_ARGUMENT (https://github.com/googleapis/googleapis/blob/master/google/rpc/code.proto)."""
QUEUE_NAME_FORMAT = (
    "The queue name must have the following format: projects/PROJECT_ID/locations/LOCATION_ID/queues/QUEUE_ID. "
    "QUEUE_ID can contain letters ([A-Za-z]), numbers ([0-9]), or hyphens (-). The maximum length is 100 characters."
)
"""Queue.name (queues)."""
TASK_NAME_FORMAT = (
    "The task name must have the following format: projects/PROJECT_ID/locations/LOCATION_ID/queues/QUEUE_ID/tasks/"
    "TASK_ID. TASK_ID can contain only letters ([A-Za-z]), numbers ([0-9]), hyphens (-), or underscores (_). The "
    "maximum length is 500 characters."
)
"""Task.name (tasks)."""
LOCATION_NAME_FORMAT = "Required. The location name. For example: projects/PROJECT_ID/locations/LOCATION_ID"
"""ListQueuesRequest.parent, CreateQueueRequest.parent (RPC reference)."""
URL_RULE = 'This string must begin with either "http://" or "https://".'
"""HttpRequest.url (tasks)."""
BODY_RULE = "A request body is allowed only if the HTTP method is POST, PUT, or PATCH."
"""HttpRequest.body (tasks)."""
DEADLINE_RULE = "The deadline must be in the interval [15 seconds, 30 minutes]."
"""Task.dispatchDeadline, for HTTP tasks (tasks)."""
MESSAGE_RULE = "Required. The message to send to the worker."
"""Task, union field message_type (tasks)."""

_STATUS_WORD = {
    400: "INVALID_ARGUMENT",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ALREADY_EXISTS",
    412: "FAILED_PRECONDITION",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
}


class Wire(Model):
    """A body as Google sends or reads it: camelCase, and unknown fields refused as Google refuses them."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class RetryConfig(Wire):
    maxAttempts: int | None = None
    maxRetryDuration: str | None = None
    minBackoff: str | None = None
    maxBackoff: str | None = None
    maxDoublings: int | None = None


class RateLimits(Wire):
    maxDispatchesPerSecond: float | None = None
    maxBurstSize: int | None = Field(default=None, description="Output only: read and ignored, as Google ignores it")
    maxConcurrentDispatches: int | None = None


class QueueIn(Wire):
    """A Queue as `queues.create` reads one. `state` and `purgeTime` are output only, and ignored on input."""

    name: str
    rateLimits: RateLimits | None = None
    retryConfig: RetryConfig | None = None
    state: str | int | None = None
    purgeTime: str | None = None


class OidcToken(Wire):
    serviceAccountEmail: str
    audience: str | None = None


class OAuthToken(Wire):
    serviceAccountEmail: str
    scope: str | None = None


HTTP_METHODS = ("HTTP_METHOD_UNSPECIFIED", "POST", "GET", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS")
"""`HttpMethod` by its number: Google's clients ask for enums as numbers (`$alt=json;enum-encoding=int`)."""
VIEWS = ("VIEW_UNSPECIFIED", "BASIC", "FULL")


def enum_name(value: str | int, names: tuple[str, ...], field: str) -> str:
    """An enum as proto3 JSON carries it, by name or by number."""
    if isinstance(value, int):
        if not 0 <= value < len(names):
            raise TasksRefusal(400, f"{INVALID_ARGUMENT} {field}: {value} is none of {', '.join(names)}")
        return names[value]
    if value not in names:
        raise TasksRefusal(400, f"{INVALID_ARGUMENT} {field}: {value!r} is none of {', '.join(names)}")
    return value


class HttpRequestIn(Wire):
    url: str
    httpMethod: str | int = "POST"
    headers: dict[str, str] = {}
    body: str | None = Field(default=None, description="Base64")
    oidcToken: OidcToken | None = None
    oauthToken: OAuthToken | None = None


class TaskIn(Wire):
    name: str | None = None
    scheduleTime: str | None = None
    dispatchDeadline: str | None = None
    httpRequest: HttpRequestIn | None = None
    appEngineHttpRequest: JsonValue = None


class CreateTaskIn(Wire):
    task: TaskIn
    responseView: str | int | None = None


class TaskCreated(Wire):
    """`CreateTaskRequest`, as gRPC carries it: the REST body and the queue it names in its path."""

    parent: str
    task: TaskIn
    responseView: str | int | None = None

    def as_rest(self) -> CreateTaskIn:
        return CreateTaskIn(task=self.task, responseView=self.responseView)


class QueueCreated(Wire):
    """`CreateQueueRequest`, as gRPC carries it."""

    parent: str
    queue: QueueIn


class Named(Wire):
    """`GetQueueRequest`, `DeleteQueueRequest`, `DeleteTaskRequest`: the resource a REST call names in its path."""

    name: str


class TaskNamed(Wire):
    """`GetTaskRequest`."""

    name: str
    responseView: str | int | None = None


class Parented(Wire):
    """`ListQueuesRequest`: paged; a `filter` or `readMask` is refused by name."""

    parent: str
    filter: str | None = None
    pageSize: int | None = None
    pageToken: str | None = None
    readMask: str | None = None


class TasksListed(Wire):
    """`ListTasksRequest`: paged."""

    parent: str
    responseView: str | int | None = None
    pageSize: int | None = None
    pageToken: str | None = None


class ErrorBody(Wire):
    code: int
    message: str
    status: str


class GoogleError(Wire):
    error: ErrorBody


class TasksRefusal(ServiceRefusal):
    """Cloud Tasks would refuse this request: its status, Google's status word and its message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def render(self, asked: Asked) -> Rendered:
        return error_answer(self.code, _STATUS_WORD.get(self.code, "UNKNOWN"), self.message)

    def grpc_code(self) -> GrpcCode:
        """The status a gRPC call is refused with: the one Google's REST error names (`status`)."""
        return GrpcCode(_STATUS_WORD.get(self.code, "UNKNOWN"))


def error_answer(status: int, word: str, message: str) -> Rendered:
    """Google's error envelope, which `google.api_core` reads its exception type from (`status`)."""
    body = GoogleError(error=ErrorBody(code=status, message=message, status=word))
    return Rendered(status=status, content_type=JSON, body=body.model_dump_json().encode())


def duration(text: str) -> timedelta:
    """`"3.5s"` as proto3 JSON writes a Duration: "The value must be given as a string that indicates the length of
    time (in seconds) followed by `s`" (each Duration field of the reference)."""
    found = re.fullmatch(r"(-?\d+(?:\.\d+)?)s", text)
    if found is None:
        raise TasksRefusal(
            400,
            f"{INVALID_ARGUMENT} {text!r}: the value must be given as a string that indicates "
            "the length of time (in seconds) followed by s",
        )
    return timedelta(seconds=float(found.group(1)))


def seconds(span: timedelta) -> str:
    total = span.total_seconds()
    return f"{total:.9f}".rstrip("0").rstrip(".") + "s"


def moment(text: str) -> datetime:
    """An RFC 3339 timestamp, as proto3 JSON writes one."""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise TasksRefusal(400, f"{INVALID_ARGUMENT} {text!r}: a timestamp uses RFC 3339") from e
    if parsed.tzinfo is None:
        raise TasksRefusal(400, f"{INVALID_ARGUMENT} {text!r}: a timestamp uses RFC 3339, with an offset")
    return parsed.astimezone(UTC)


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


# --- the API's surface ------------------------------------------------------------------------------------------

_V2 = "/v2/projects/[^/]+/locations/[^/]+"
REST_METHODS: dict[str, tuple[str, re.Pattern[str]]] = {
    "cloudtasks.projects.locations.list": ("GET", re.compile(r"/v2/projects/[^/]+/locations")),
    "cloudtasks.projects.locations.get": ("GET", re.compile(_V2)),
    "cloudtasks.projects.locations.getCmekConfig": ("GET", re.compile(_V2 + "/cmekConfig")),
    "cloudtasks.projects.locations.updateCmekConfig": ("PATCH", re.compile(_V2 + "/cmekConfig")),
    "cloudtasks.projects.locations.operations.get": ("GET", re.compile(_V2 + "/operations/[^/]+")),
    "cloudtasks.projects.locations.queues.create": ("POST", re.compile(_V2 + "/queues")),
    "cloudtasks.projects.locations.queues.list": ("GET", re.compile(_V2 + "/queues")),
    "cloudtasks.projects.locations.queues.get": ("GET", re.compile(_V2 + "/queues/[^/:]+")),
    "cloudtasks.projects.locations.queues.delete": ("DELETE", re.compile(_V2 + "/queues/[^/:]+")),
    "cloudtasks.projects.locations.queues.patch": ("PATCH", re.compile(_V2 + "/queues/[^/:]+")),
    "cloudtasks.projects.locations.queues.purge": ("POST", re.compile(_V2 + "/queues/[^/:]+:purge")),
    "cloudtasks.projects.locations.queues.pause": ("POST", re.compile(_V2 + "/queues/[^/:]+:pause")),
    "cloudtasks.projects.locations.queues.resume": ("POST", re.compile(_V2 + "/queues/[^/:]+:resume")),
    "cloudtasks.projects.locations.queues.getIamPolicy": ("POST", re.compile(_V2 + "/queues/[^/:]+:getIamPolicy")),
    "cloudtasks.projects.locations.queues.setIamPolicy": ("POST", re.compile(_V2 + "/queues/[^/:]+:setIamPolicy")),
    "cloudtasks.projects.locations.queues.testIamPermissions": (
        "POST",
        re.compile(_V2 + "/queues/[^/:]+:testIamPermissions"),
    ),
    "cloudtasks.projects.locations.queues.tasks.create": ("POST", re.compile(_V2 + "/queues/[^/:]+/tasks")),
    "cloudtasks.projects.locations.queues.tasks.list": ("GET", re.compile(_V2 + "/queues/[^/:]+/tasks")),
    "cloudtasks.projects.locations.queues.tasks.get": ("GET", re.compile(_V2 + "/queues/[^/:]+/tasks/[^/:]+")),
    "cloudtasks.projects.locations.queues.tasks.delete": ("DELETE", re.compile(_V2 + "/queues/[^/:]+/tasks/[^/:]+")),
    "cloudtasks.projects.locations.queues.tasks.run": ("POST", re.compile(_V2 + "/queues/[^/:]+/tasks/[^/:]+:run")),
    "cloudtasks.projects.locations.queues.tasks.buffer": (
        "POST",
        re.compile(_V2 + "/queues/[^/:]+/tasks/[^/:]+:buffer"),
    ),
    "cloudtasks.projects.locations.queues.tasks.batchCreate": (
        "POST",
        re.compile(_V2 + "/queues/[^/:]+/tasks:batchCreate"),
    ),
    "cloudtasks.projects.locations.queues.tasks.batchDelete": (
        "POST",
        re.compile(_V2 + "/queues/[^/:]+/tasks:batchDelete"),
    ),
}
"""Every method of Cloud Tasks' discovery document (v2), by its `httpMethod` and `flatPath`."""

REST_SERVED = frozenset(
    {
        "cloudtasks.projects.locations.queues.create",
        "cloudtasks.projects.locations.queues.list",
        "cloudtasks.projects.locations.queues.get",
        "cloudtasks.projects.locations.queues.delete",
        "cloudtasks.projects.locations.queues.tasks.create",
        "cloudtasks.projects.locations.queues.tasks.list",
        "cloudtasks.projects.locations.queues.tasks.get",
        "cloudtasks.projects.locations.queues.tasks.delete",
    }
)

GRPC_METHODS = (
    "CreateQueue",
    "ListQueues",
    "GetQueue",
    "DeleteQueue",
    "UpdateQueue",
    "PurgeQueue",
    "PauseQueue",
    "ResumeQueue",
    "GetIamPolicy",
    "SetIamPolicy",
    "TestIamPermissions",
    "CreateTask",
    "ListTasks",
    "GetTask",
    "DeleteTask",
    "RunTask",
    "BatchCreateTasks",
    "BatchDeleteTasks",
    "GetCmekConfig",
    "UpdateCmekConfig",
)
"""Every method of `google.cloud.tasks.v2.CloudTasks`, as the installed client's `gapic_metadata.json` lists it."""

GRPC_SERVED = frozenset(
    {"CreateQueue", "ListQueues", "GetQueue", "DeleteQueue", "CreateTask", "ListTasks", "GetTask", "DeleteTask"}
)

_LOCATIONS = "locations are not served"
_CMEK = "customer-managed encryption keys are not served"
_UPDATE = "changing a queue after it is created is not served"
_PURGE = "purging a queue is not served"
_PAUSE = "pausing and resuming a queue are not served"
_IAM = "IAM is not served: Minutehand enforces no credentials"
_RUN = "forcing a task to run now is not served"
_BATCH = "batch calls are not served"

REST_REFUSED: dict[str, str] = {
    "cloudtasks.projects.locations.list": _LOCATIONS,
    "cloudtasks.projects.locations.get": _LOCATIONS,
    "cloudtasks.projects.locations.getCmekConfig": _CMEK,
    "cloudtasks.projects.locations.updateCmekConfig": _CMEK,
    "cloudtasks.projects.locations.operations.get": "long-running operations are not served",
    "cloudtasks.projects.locations.queues.patch": _UPDATE,
    "cloudtasks.projects.locations.queues.purge": _PURGE,
    "cloudtasks.projects.locations.queues.pause": _PAUSE,
    "cloudtasks.projects.locations.queues.resume": _PAUSE,
    "cloudtasks.projects.locations.queues.getIamPolicy": _IAM,
    "cloudtasks.projects.locations.queues.setIamPolicy": _IAM,
    "cloudtasks.projects.locations.queues.testIamPermissions": _IAM,
    "cloudtasks.projects.locations.queues.tasks.run": _RUN,
    "cloudtasks.projects.locations.queues.tasks.buffer": "buffering a task is not served",
    "cloudtasks.projects.locations.queues.tasks.batchCreate": _BATCH,
    "cloudtasks.projects.locations.queues.tasks.batchDelete": _BATCH,
}
"""Why each REST method not served is refused."""

GRPC_REFUSED: dict[str, str] = {
    "UpdateQueue": _UPDATE,
    "PurgeQueue": _PURGE,
    "PauseQueue": _PAUSE,
    "ResumeQueue": _PAUSE,
    "GetIamPolicy": _IAM,
    "SetIamPolicy": _IAM,
    "TestIamPermissions": _IAM,
    "RunTask": _RUN,
    "BatchCreateTasks": _BATCH,
    "BatchDeleteTasks": _BATCH,
    "GetCmekConfig": _CMEK,
    "UpdateCmekConfig": _CMEK,
}
"""Why each gRPC method not served is refused."""


def rest_method(method: str, path: str) -> str | None:
    """The discovery method id a REST request calls, or None when no method has that route."""
    return next((m for m, (verb, route) in REST_METHODS.items() if verb == method and route.fullmatch(path)), None)
