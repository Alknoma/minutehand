"""What the agent's own telemetry says, as Minutehand received it during a run.

An agent exports OpenTelemetry to Minutehand's receiver for the length of a run; each span is kept with the
run, stamped with the wake that was in progress and the simulated time when it ARRIVED. Its own start and end
are the real times the agent's SDK gave it: a span is never moved onto the simulated clock.

An attribute keeps the value kinds OTLP has (`AnyValue`): a string, a bool, an int, a double, bytes, an array
of values, or a list of key/value pairs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from minutehand.domain.scenario import Model

TRACE_ID = r"^[0-9a-f]{32}$"
SPAN_ID = r"^[0-9a-f]{16}$"


class StringValue(Model):
    kind: Literal["string"] = "string"
    value: str


class BoolValue(Model):
    kind: Literal["bool"] = "bool"
    value: bool


class IntValue(Model):
    kind: Literal["int"] = "int"
    value: int


class DoubleValue(Model):
    kind: Literal["double"] = "double"
    value: float


class BytesValue(Model):
    kind: Literal["bytes"] = "bytes"
    value: str = Field(description="The bytes, hex-encoded")


class ArrayValue(Model):
    kind: Literal["array"] = "array"
    values: list[AttributeValue]


class MapValue(Model):
    kind: Literal["map"] = "map"
    values: list[Attribute]


AttributeValue = Annotated[
    StringValue | BoolValue | IntValue | DoubleValue | BytesValue | ArrayValue | MapValue,
    Field(discriminator="kind"),
]


class Attribute(Model):
    key: str
    value: AttributeValue | None = Field(default=None, description="None: OTLP sent the key with an empty value")


class SpanStatus(StrEnum):
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


class ReceivedSpan(Model):
    """One span as the agent's SDK exported it."""

    trace_id: str = Field(pattern=TRACE_ID)
    span_id: str = Field(pattern=SPAN_ID)
    parent_span_id: str | None = Field(default=None, pattern=SPAN_ID)
    name: str
    start: AwareDatetime = Field(description="Real time, as the agent's SDK stamped it")
    end: AwareDatetime = Field(description="Real time, as the agent's SDK stamped it")
    status: SpanStatus = SpanStatus.UNSET
    status_message: str | None = None
    attributes: list[Attribute] = []
    service_name: str | None = Field(default=None, description="The resource's `service.name`")

    def attribute(self, key: str) -> AttributeValue | None:
        """The value of the first attribute named `key`; None when there is none or it is empty."""
        return next((a.value for a in self.attributes if a.key == key), None)


class SpanSource(StrEnum):
    RECEIVED = "received"  # the agent's own SDK exported it to the receiver
    WIRE = "wire"  # Minutehand recorded a model call it saw on the wire (`--record-model-calls`)


class StoredSpan(Model):
    """A span as the run's store keeps it: what arrived, and when in the run it arrived."""

    span: ReceivedSpan
    run_id: str = Field(description="The run it arrived in; a fork lists its parent's spans up to the fork")
    source: SpanSource
    wake: int = Field(ge=0, description="The wake in progress when it arrived; 0 is setup")
    sim_time: AwareDatetime = Field(description="Simulated time when it arrived")
    after_seq: int = Field(ge=0, description="The head of the world's log when it arrived")


class Signal(StrEnum):
    TRACES = "traces"
    LOGS = "logs"
    METRICS = "metrics"


class ForwardFailure(Model):
    """A payload the agent exported that could not be passed on to where the agent's environment sent it."""

    signal: Signal
    endpoint: str
    reason: str
    wake: int = Field(ge=0)
    sim_time: AwareDatetime


ArrayValue.model_rebuild()
MapValue.model_rebuild()
