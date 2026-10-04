"""A schedule as the world holds it, and when it next fires.

The record is the body of the schedule's RECORD entity in the run's store, so what
is booked, and for when, survives the process and rewinds with the world.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime

from minutehand.adapters.providers.aws.wire import (
    ActionAfterCompletion,
    At,
    Cron,
    Expression,
    Rate,
    ScheduleRequest,
    ScheduleState,
    expression,
    timezone_of,
)
from minutehand.domain.scenario import Model

_HORIZON = timedelta(days=366 * 5)


class ScheduleRecord(Model):
    arn: str
    name: str
    group: str
    region: str
    account: str
    expression_text: str
    expression: Expression
    timezone: str
    start_date: AwareDatetime | None
    end_date: AwareDatetime | None
    state: ScheduleState
    action_after_completion: ActionAfterCompletion
    target_arn: str
    target_input: str | None
    message_group_id: str | None
    anchored_at: AwareDatetime
    next_at: AwareDatetime | None

    @classmethod
    def of(cls, request: ScheduleRequest, *, arn: str, name: str, group: str, region: str, account: str,
           now: datetime) -> ScheduleRecord:
        """The schedule a create or update asks for, booked for its first occurrence relative to `now`."""
        record = cls(
            arn=arn, name=name, group=group, region=region, account=account,
            expression_text=request.schedule_expression, expression=expression(request.schedule_expression),
            timezone=request.schedule_expression_timezone, start_date=request.start_date,
            end_date=request.end_date, state=request.state,
            action_after_completion=request.action_after_completion, target_arn=request.target.arn,
            target_input=request.target.input,
            message_group_id=request.target.sqs_parameters.message_group_id if request.target.sqs_parameters else None,
            anchored_at=now, next_at=None,
        )
        first = record.first_occurrence(now) if record.state is ScheduleState.ENABLED else None
        return record.model_copy(update={"next_at": first})

    def text(self) -> str:
        return " ".join(s for s in (self.name, self.expression_text, self.timezone, self.target_arn, self.target_input) if s)

    def first_occurrence(self, now: datetime) -> datetime | None:
        """A one-time schedule fires at its time even when that is already past; a recurring one, after `now`."""
        match self.expression:
            case At(local=local):
                return local.replace(tzinfo=timezone_of(self.timezone)).astimezone(UTC)
            case Rate() | Cron():
                return self.occurrence_after(now)

    def occurrence_after(self, now: datetime) -> datetime | None:
        """The next recurrence strictly after `now`, or None once the schedule is over."""
        match self.expression:
            case At():
                return None
            case Rate(every=every):
                found = _next_rate(self.start_date or self.anchored_at, every, now,
                                   include_anchor=self.start_date is not None)
            case Cron() as cron:
                floor = max(now, self.start_date - timedelta(microseconds=1)) if self.start_date else now
                found = _next_cron(cron, timezone_of(self.timezone), floor)
        if found is None or (self.end_date is not None and found > self.end_date):
            return None
        return found


def _next_rate(anchor: datetime, every: timedelta, now: datetime, *, include_anchor: bool) -> datetime:
    """anchor + k * every for the smallest k (k >= 1, or >= 0 when anchored at a StartDate) after now."""
    if now < anchor:
        return anchor if include_anchor else anchor + every
    k = (now - anchor) // every + 1
    return anchor + k * every


def _next_cron(cron: Cron, zone: ZoneInfo, now: datetime) -> datetime | None:
    """The first minute after `now` that the expression names, read as wall time in `zone`."""
    local_now = now.astimezone(zone)
    day = local_now.date()
    last = (local_now + _HORIZON).date()
    hours, minutes = sorted(cron.hours), sorted(cron.minutes)
    while day <= last:
        aws_weekday = (day.weekday() + 1) % 7 + 1
        if (
            day.year in cron.years and day.month in cron.months
            and (cron.days_of_month is None or day.day in cron.days_of_month)
            and (cron.days_of_week is None or aws_weekday in cron.days_of_week)
        ):
            for hour in hours:
                for minute in minutes:
                    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(UTC)
                    if candidate > now:
                        return candidate
        day += timedelta(days=1)
    return None
