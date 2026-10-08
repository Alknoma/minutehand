"""The AWS provider's manifest. Data only: loading it imports neither moto nor the provider.

`FINISHED` covers exactly what its tests exercise, and every other operation of AWS's own surface for the two
services (botocore's `scheduler` and `sqs` models) is refused 501 by name, as is every other AWS service:

- EventBridge Scheduler CreateSchedule, UpdateSchedule, DeleteSchedule, GetSchedule and ListSchedules, with
  `at(...)`, `rate(...)` and `cron(...)` expressions (no `L`, `W` or `#`), a ScheduleExpressionTimezone,
  StartDate, EndDate, State and ActionAfterCompletion, in the `default` group;
- delivery of a fired schedule to an SQS queue target in the run's account, with SqsParameters.MessageGroupId;
- SQS CreateQueue, GetQueueUrl, GetQueueAttributes, ListQueues, SendMessage, ReceiveMessage, DeleteMessage,
  DeleteMessageBatch and ChangeMessageVisibility, on the run's clock.

Where each behaviour comes from is in `CLAIMS.md`.
"""

from __future__ import annotations

from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="aws",
    tier=Tier.FINISHED,
    hosts=["*.amazonaws.com"],
    kinds=[EntityKind.RECORD],
    books_wakes=True,
    state_outside_log="its queues, their messages and its copy of each schedule, in moto's memory of the process "
    "that played the run, under that run's own account",
)
