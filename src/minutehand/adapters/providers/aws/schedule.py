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
    client_token: str | None = None
    request: ScheduleRequest
    """The CreateSchedule or UpdateSchedule body, as sent."""
    anchored_at: AwareDatetime
    next_at: AwareDatetime | None

    @classmethod
    def of(
        cls, request: ScheduleRequest, *, arn: str, name: str, group: str, region: str, account: str, now: datetime
    ) -> ScheduleRecord:
        """The schedule a create or update asks for, booked for its first occurrence relative to `now`."""
        record = cls(
            arn=arn,
            name=name,
            group=group,
            region=region,
            account=account,
            expression_text=request.schedule_expression,
            expression=expression(request.schedule_expression),
            timezone=request.schedule_expression_timezone or "UTC",
            start_date=request.start_date,
            end_date=request.end_date,
            state=request.state,
            action_after_completion=request.action_after_completion or ActionAfterCompletion.NONE,
            request=request,
            target_arn=request.target.arn,
            target_input=request.target.input,
            message_group_id=request.target.sqs_parameters.message_group_id if request.target.sqs_parameters else None,
            client_token=request.client_token,
            anchored_at=now,
            next_at=None,
        )
        first = record.first_occurrence(now) if record.state is ScheduleState.ENABLED else None
        return record.model_copy(update={"next_at": first})

    def text(self) -> str:
        return " ".join(
            s for s in (self.name, self.expression_text, self.timezone, self.target_arn, self.target_input) if s
        )

    def first_occurrence(self, now: datetime) -> datetime | None:
        """A one-time schedule fires at its time even when that is already past. A rate schedule with no
        StartDate "starts invoking the target immediately" (EventBridge Scheduler User Guide, Schedule types); with
        one, at its StartDate. A cron schedule, at its first time after `now`."""
        match self.expression:
            case At(local=local):
                return local.replace(tzinfo=timezone_of(self.timezone)).astimezone(UTC)
            case Rate() if self.start_date is None:
                return now if self.end_date is None or now <= self.end_date else None
            case Rate() | Cron():
                return self.occurrence_after(now)

    def occurrence_after(self, now: datetime) -> datetime | None:
        """The next recurrence strictly after `now`, or None once the schedule is over."""
        match self.expression:
            case At():
                return None
            case Rate(every=every):
                found = _next_rate(self.start_date or self.anchored_at, every, now)
            case Cron() as cron:
                floor = max(now, self.start_date - timedelta(microseconds=1)) if self.start_date else now
                found = _next_cron(cron, timezone_of(self.timezone), floor)
        if found is None or (self.end_date is not None and found > self.end_date):
            return None
        return found


def _next_rate(anchor: datetime, every: timedelta, now: datetime) -> datetime:
    """anchor + k * every for the smallest k >= 0 after now."""
    if now < anchor:
        return anchor
    k = (now - anchor) // every + 1
    return anchor + k * every


def _next_cron(cron: Cron, zone: ZoneInfo, now: datetime) -> datetime | None:
    """The first minute after `now` that the expression names, read as wall time in `zone`. A wall time the zone
    skips (spring forward) is skipped, and one it repeats (fall back) runs once, at its first instance: "if a cron
    expression falls on a non-existent date and time, your schedule invocation is skipped ... your schedule runs
    only once and does not repeat its invocation" (User Guide, Daylight savings time on EventBridge Scheduler)."""
    local_now = now.astimezone(zone)
    day = local_now.date()
    last = (local_now + _HORIZON).date()
    hours, minutes = sorted(cron.hours), sorted(cron.minutes)
    while day <= last:
        aws_weekday = (day.weekday() + 1) % 7 + 1
        if (
            day.year in cron.years
            and day.month in cron.months
            and (cron.days_of_month is None or day.day in cron.days_of_month)
            and (cron.days_of_week is None or aws_weekday in cron.days_of_week)
        ):
            for hour in hours:
                for minute in minutes:
                    wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                    candidate = wall.astimezone(UTC)
                    if candidate.astimezone(zone).replace(tzinfo=None) != wall.replace(tzinfo=None):
                        continue
                    if candidate > now:
                        return candidate
        day += timedelta(days=1)
    return None
