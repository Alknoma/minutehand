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

from minutehand.domain.provider import Holds, Manifest, Tier
from minutehand.domain.world import EntityKind

MANIFEST = Manifest(
    key="aws",
    tier=Tier.FINISHED,
    hosts=["*.amazonaws.com"],
    kinds=[EntityKind.RECORD],
    books_wakes=True,
    state_outside_log="its queues, their messages and its copy of each schedule, in moto's memory of the process "
    "that played the run, under that run's own account",
    holds=Holds(
        person_without_email=False,
        person_title=False,
        person_guest=False,
        person_deactivated=False,
        person_bot=False,
        person_working_hours=False,
        person_absences=False,
        account_login=False,
        account_id=False,
        account_name=False,
        account_email_hidden=False,
        ticket_key=False,
        ticket_labels=False,
        ticket_comments=False,
        ticket_number=False,
        ticket_id=False,
        document_spreadsheet=False,
        document_presentation=False,
        document_file=False,
        document_folder=False,
        document_owner=False,
        document_space=False,
        document_shared_with=False,
        document_modified_before_start=False,
        document_modified_by=False,
        document_id=False,
        channel_named=False,
        channel_direct=False,
        channel_private=False,
        channel_archived=False,
        channel_topic=False,
        channel_purpose=False,
        channel_without_agent=False,
        channel_history=False,
        channel_threads=False,
        channel_files=False,
        channel_id=False,
        channel_post_id=False,
        space_spaces=False,
        space_id=False,
        ticket_action_moves=False,
        ticket_action_reassigns=False,
        ticket_action_comments=False,
        ticket_action_deletes=False,
        messaging_posts=False,
        messaging_edits=False,
        messaging_deletes=False,
        messaging_reacts=False,
        messaging_joins=False,
        messaging_adds_agent=False,
        messaging_opens_agent=False,
        messaging_commands=False,
        document_change_edited=False,
        document_change_renamed=False,
        document_change_moved=False,
        document_change_shared=False,
        document_change_trashed=False,
        document_change_commented=False,
        document_change_field_set=False,
        people_change_removed=False,
        people_change_deactivated=False,
        people_change_reactivated=False,
        sign_ins=False,
        faults=False,
    ),
)
