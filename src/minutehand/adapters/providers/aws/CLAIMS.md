# AWS: where each behaviour comes from

AWS is answered by moto (`moto[server]` 5.2.3, mounted in the process) for Amazon EventBridge Scheduler and Amazon
SQS, and by nothing else. Minutehand adds what moto cannot know (the run's clock, the run's own account, a schedule
firing into its queue) and corrects moto where it answers otherwise than AWS. **Documented** means AWS's public
reference says so, at the page given; **observed** means it rests on botocore's service models as installed
(botocore 1.43.108); **Minutehand's** means it is Minutehand's own rule for a simulated run, not a claim about AWS.

Tests are in `tests/providers/aws/`: `test_aws_provider.py` (P), `test_aws_fidelity.py` (F), `test_aws_coverage.py`
(C), `test_aws_base_url.py`, and the whole run `tests/e2e/test_booked_on_aws.py`. Every test drives stock boto3
through the proxy.

Pages: Scheduler API = https://docs.aws.amazon.com/scheduler/latest/APIReference/, Scheduler guide =
https://docs.aws.amazon.com/scheduler/latest/UserGuide/, SQS API =
https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/, SQS guide =
https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/.

**Minutehand deliberately does not enforce credentials.** Any access key, any SigV4 signature, a request signed for
another service, or an unsigned request is answered. moto can check signatures and IAM policies
(`moto.core.authorization`, switched on by the `INITIAL_NO_AUTH_ACTION_COUNT` environment variable, by
`set_initial_no_auth_action_count` or `enable_iam_authentication` in the process, or by moto's
`/moto-api/reset-auth`); the provider sets the count back to "never" before every call into moto, refuses
`/moto-api`, and hands moto a credential scope of its own naming the service and region the request's host names,
so nothing the caller signed with is read.

## The surface

| Claim | Class | Test | Source |
|---|---|---|---|
| The surface is botocore's `scheduler` model (2021-06-30, 12 operations) and `sqs` model (2012-11-05, 23 operations); each operation is served or refused by name, and the provider's route table is the model's `http.method` and `http.requestUri` | observed | C `test_every_operation_in_the_models_is_served_or_refused_by_name_and_nothing_else_is_listed`, `test_the_scheduler_route_table_is_the_models` | `botocore/data/scheduler/2021-06-30/service-2.json`, `botocore/data/sqs/2012-11-05/service-2.json` |
| Served: Scheduler `CreateSchedule`, `UpdateSchedule`, `DeleteSchedule`, `GetSchedule`, `ListSchedules`; SQS `CreateQueue`, `GetQueueUrl`, `GetQueueAttributes`, `ListQueues`, `SendMessage`, `ReceiveMessage`, `DeleteMessage`, `DeleteMessageBatch`, `ChangeMessageVisibility` | Minutehand's | C `test_every_served_operation_is_answered_by_the_service_not_refused_as_unserved` | — |
| Every other operation of either model answers 501 `NotImplemented`, naming the operation and why (`wire.REFUSED_BECAUSE`), in JSON for the JSON protocols and in the query protocol's `ErrorResponse` XML for a form-encoded request | Minutehand's | C `test_a_refused_operation_called_by_boto3_is_refused_501_naming_it` (17 cases), `test_a_refused_operation_in_sqss_query_protocol_is_refused_in_its_xml` | — |
| Any other AWS host (STS, Lambda, ...) answers 501 naming the host; moto's `/moto-api` answers 501, as no part of AWS | Minutehand's | F `test_another_aws_service_is_refused_naming_its_host`, `test_motos_management_api_is_refused_as_no_part_of_aws` | — |
| Any credential, or none, is answered, even when moto is told to check IAM | Minutehand's | F `test_any_credential_or_none_is_answered_even_when_moto_is_told_to_check_iam` | — |
| Each run is an AWS account of its own (a random twelve-digit id), so runs in one process never share a queue | Minutehand's | P `test_two_provider_instances_do_not_see_each_others_aws` | — |

## EventBridge Scheduler

| Claim | Class | Test | Source |
|---|---|---|---|
| `at(yyyy-mm-ddThh:mm:ss)` fires once at that wall time in `ScheduleExpressionTimezone` (UTC when none), even when already past | documented | P `test_a_one_time_schedule_books_its_instant_in_its_own_timezone`, `test_a_schedule_already_past_the_simulated_now_fires_at_once` | Scheduler guide, schedule-types.html (One-time schedules); Scheduler API, API_CreateSchedule.html (ScheduleExpression) |
| `rate(value unit)`: a positive integer and any of `minute`, `minutes`, `hour`, `hours`, `day`, `days`; a day is 24 hours | documented | F `test_a_rate_takes_any_of_its_six_units_with_any_positive_value`; P `test_an_invalid_expression_is_refused_and_books_nothing` | Scheduler API, API_CreateSchedule.html (ScheduleExpression); Scheduler guide, schedule-types.html (Daylight savings time) |
| A rate schedule with no `StartDate` invokes its target at once, then every interval; with one, first at `StartDate` | documented | P `test_a_rate_schedule_with_no_start_date_fires_at_once_and_rebooks_after_firing` | Scheduler guide, schedule-types.html ("If you do not provide a StartDate for a rate-based schedule, your schedule starts invoking the target immediately") |
| `cron(minutes hours day-of-month month day-of-week year)` with `,` `-` `*` `/` `?`, `JAN-DEC`, `SUN-SAT` (1 = Sunday), years 1970-2199, evaluated in its timezone | documented | P `test_a_cron_schedule_books_its_next_occurrence_in_its_timezone_and_rebooks_after_firing` | Scheduler guide, schedule-types.html (Cron-based schedules) |
| `*` in both day fields is `ValidationException` | documented | F `test_cron_day_fields_the_reference_forbids_or_leaves_open_are_refused` | Scheduler guide, schedule-types.html ("You can't use * in both the Day-of-month and Day-of-week fields") |
| A cron time daylight saving skips is skipped; one it repeats runs once | documented | F `test_a_cron_time_that_daylight_saving_skips_is_skipped` | Scheduler guide, schedule-types.html (Daylight savings time) |
| `StartDate` and `EndDate` bound a recurring schedule, with no limit on how far back `StartDate` may be; one-time schedules ignore both | documented | P `test_a_recurring_schedule_stops_at_its_end_date`; F `test_a_cron_schedule_whose_start_date_is_past_is_accepted_as_sent` | Scheduler API, API_CreateSchedule.html (StartDate, EndDate) |
| `State` `DISABLED` books nothing; a schedule is `ENABLED` by default, also after an `UpdateSchedule` that leaves `State` out | documented | P `test_a_disabled_schedule_books_nothing_until_it_is_enabled`; F `test_an_update_that_leaves_fields_out_sets_them_to_their_defaults` | Scheduler guide, getting-started.html ("By default, the EventBridge Scheduler enables your schedule") |
| `UpdateSchedule` replaces the whole schedule: a field it leaves out takes its default (`ActionAfterCompletion` none, `State` ENABLED) | documented | F `test_an_update_that_leaves_fields_out_sets_them_to_their_defaults` | Scheduler API, API_UpdateSchedule.html |
| `ActionAfterCompletion` `DELETE` deletes a one-time schedule after it fires, and a recurring one after its last invocation before `EndDate` | documented | P `test_action_after_completion_delete_removes_a_one_time_schedule_once_it_fires` | Scheduler guide, managing-schedule-delete.html |
| `FlexibleTimeWindow` `FLEXIBLE` needs `MaximumWindowInMinutes` (1 to 1440); the target is invoked at the scheduled time, which is within the window | documented | F `test_a_flexible_window_without_its_maximum_is_refused` | Scheduler guide, managing-schedule-flexible-time-windows.html; Scheduler API, API_FlexibleTimeWindow.html |
| A schedule is read back with the `Target` and `ScheduleExpressionTimezone` it was sent with, nothing added | documented | F `test_a_schedule_is_read_back_with_the_target_and_timezone_it_was_sent_with_and_nothing_added` | Scheduler API, API_GetSchedule.html |
| A `CreateSchedule` repeated with its `ClientToken` and the same request answers the same `ScheduleArn` | documented | F `test_a_create_repeated_with_its_client_token_answers_the_same_schedule` | Scheduler API, API_CreateSchedule.html (ClientToken: "to ensure the idempotency of the request") |
| A schedule in a group other than `default` is `ResourceNotFoundException`, "The request references a resource which does not exist." (no other group can be created here) | documented | F `test_a_schedule_in_a_group_other_than_default_is_refused_not_found` | Scheduler API, API_CreateSchedule.html, API_GetSchedule.html (Errors) |
| The schedule's ARN, `CreationDate` and `LastModificationDate` are assigned, the dates from the run's clock | documented | F `test_message_and_schedule_timestamps_are_the_runs_time` | Scheduler API, API_GetSchedule.html |
| A schedule's next occurrence is a wake on the run's clock; when it fires, its `Target.Input` is put on its SQS queue (with `SqsParameters.MessageGroupId` for FIFO), as the agent's own `ReceiveMessage` finds it | Minutehand's (delivery on the run's clock) | P `test_fire_puts_the_input_where_receive_message_finds_it_and_nothing_is_there_before`, `test_a_fifo_target_gets_the_message_group_id` | Scheduler guide, schedule-types.html (the SQS target examples) |
| A delivery is taken (`ConfirmsDelivery`) when the agent deletes its message, not when it receives it | Minutehand's | P `test_a_delivery_is_taken_when_the_agent_deletes_its_message_and_not_when_it_receives_it` | — |

## SQS

| Claim | Class | Test | Source |
|---|---|---|---|
| Every time moto reads (message `SentTimestamp`, visibility, `DelaySeconds`, queue timestamps, schedule dates, a long poll's end) is the run's clock | Minutehand's | F `test_message_and_schedule_timestamps_are_the_runs_time` | — |
| A received message is invisible for the queue's visibility timeout (30 seconds by default) of the run's time, then received again | documented | F `test_a_received_message_comes_back_once_the_runs_clock_passes_the_default_visibility_timeout` | SQS API, API_ReceiveMessage.html (VisibilityTimeout) |
| `ChangeMessageVisibility` hides a received message for its new timeout of the run's time | documented | F `test_a_visibility_change_runs_on_the_runs_clock` | SQS API, API_ChangeMessageVisibility.html |
| `DelaySeconds` hides a message until the run's clock passes it | documented | F `test_a_delayed_message_appears_when_the_runs_clock_reaches_its_delay` | SQS API, API_SendMessage.html (DelaySeconds) |
| A long poll (`WaitTimeSeconds`, or the queue's `ReceiveMessageWaitTimeSeconds`) that finds a visible message answers it at once | documented | F `test_a_long_poll_that_finds_a_message_answers_at_once` | SQS API, API_ReceiveMessage.html ("If a message is available, the call returns sooner than WaitTimeSeconds") |
| A long poll that finds none is refused 501 by name, without waiting: it would have to wait on the run's clock, which does not move inside a call. Serving it needs the orchestrator to hold the call and move the run's clock to the earlier of the wait's end and the next due wake, then answer the call there | Minutehand's | F `test_a_long_poll_that_would_wait_is_refused_by_name_and_never_waits` | — |
| `MaxNumberOfMessages` is 1 to 10, else `InvalidParameterValue` (moto's check) | documented | F `test_receive_takes_one_to_ten_messages_and_refuses_more` | SQS API, API_ReceiveMessage.html |
| `DeleteMessage` takes the receipt handle of a receive; an old one may still delete (moto: it does) | documented | P `test_a_delivery_is_taken_when_the_agent_deletes_its_message_and_not_when_it_receives_it` | SQS API, API_DeleteMessage.html ("the request will succeed, but the message might not be deleted") |
| Queue attributes the caller set are answered as it wrote them | documented (data as sent) | F `test_queue_attributes_are_answered_as_they_were_sent` | SQS API, API_GetQueueAttributes.html |
| A message's body and attributes come back as sent; `MD5OfMessageBody` and `MD5OfMessageAttributes` are AWS's algorithm | documented | F `test_md5_digests_are_awss_own` | SQS guide, sqs-message-metadata.html (Calculating the MD5 message digest for message attributes) |
| `SenderId` is left out of a received message's attributes: AWS answers the sender's principal there, and Minutehand, which enforces no credentials, knows none | Minutehand's | F `test_a_received_message_carries_no_made_up_sender_id` | SQS API, API_ReceiveMessage.html (SenderId) |
| `MessageId`, receipt handles and queue URLs are assigned (moto's forms) | documented | P, F (every SQS test) | SQS API, API_SendMessage.html, API_ReceiveMessage.html |

## moto 5.2.3 divergences, and what the provider does about each

| moto does | AWS does | Here |
|---|---|---|
| checks SigV4 signatures and IAM policies once told to | (Minutehand enforces no credentials) | count reset before every call, `/moto-api` refused |
| picks the service from the signature's credential scope, and guesses for an unsigned request (an unsigned JSON SQS call is answered 200 by another service and the message is lost) | routes by host | moto is handed a credential scope for the host's service and region |
| reads the machine's clock for every timestamp, visibility, delay and long poll | (the run's clock) | `aws/clock.py` |
| writes `RetryPolicy` `{86400, 185}` into a target sent without one | answers the target as sent | restored as sent |
| answers `ScheduleExpressionTimezone` "UTC" for a schedule sent without one | not documented | restored as sent |
| `UpdateSchedule` keeps the old `ActionAfterCompletion` and nulls a left-out `State` | sets left-out fields to defaults | set from the request |
| ignores `ClientToken`, so a retried create is `ConflictException` | idempotent | answered from the run's record |
| a schedule in a group that does not exist is a `KeyError` (500) | `ResourceNotFoundException` | refused before moto |
| re-serialises `RedrivePolicy` (`maxReceiveCount` becomes a number) and drops a `Policy` with no `Statement` | answers attributes | answered as sent |
| answers `SenderId` `AIDAIT2UOQQY3AUEKVGXU` for every message | the sender's principal | left out |
| answers JSON `null` for absent members (`Description: null`) and extra members in `ListSchedules` summaries | not documented | not changed: botocore drops them; listed here |
| refuses a cron schedule whose `StartDate` is more than 5 minutes before now ("The StartDate you specify cannot be earlier than 5 minutes ago.") | no page sets a limit: StartDate is "The date, in UTC, after which the schedule can begin invoking its target" | moto's check replaced; the date kept as sent (F `test_a_cron_schedule_whose_start_date_is_past_is_accepted_as_sent`) |
| waits for a long poll on its own clock | (the run's clock) | refused by name when it would wait |

## What it does not do

- **Any target but an SQS queue in the run's own account**, and a schedule with no `Target.Input` (AWS then sends
  "a default notification" whose form no page gives), are refused 501 at `CreateSchedule` and `UpdateSchedule`.
- **Delivery failures.** A target queue that does not exist when the schedule fires raises, loudly; AWS's
  `RetryPolicy` and `DeadLetterConfig` are kept as sent and not acted on.
- **Cron `L`, `W` and `#`**, `?` in both day fields, and values in both day fields are refused 501 by name.
- **Schedule groups, tags, permissions, batch sends and visibility changes, purges, queue deletes, message moves**
  are refused 501 by name.
- **A flexible window** is honoured at its start, the scheduled time, every time; AWS picks a time within it.
- **State outside the log.** Queues, messages and moto's copy of each schedule live in moto's memory of the process
  that played the run (`Manifest.state_outside_log`), so a fork after the first AWS call is refused.
