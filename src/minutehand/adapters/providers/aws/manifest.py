"""The AWS provider's manifest. Data only: loading it imports neither moto nor the provider.

`FINISHED` covers exactly the surface its tests exercise, and nothing else of AWS:

- EventBridge Scheduler CreateSchedule, UpdateSchedule and DeleteSchedule, with
  `at(...)`, `rate(...)` and `cron(...)` expressions (no `L`, `W` or `#`), a
  ScheduleExpressionTimezone, StartDate, EndDate, State and ActionAfterCompletion;
- delivery of a fired schedule to an SQS target, with SqsParameters.MessageGroupId;
- SQS CreateQueue, GetQueueAttributes and ReceiveMessage as moto answers them.

Every other AWS call is answered by moto as moto answers it, which is `GENERATED`
in all but name; a target other than SQS raises when its schedule fires.
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
