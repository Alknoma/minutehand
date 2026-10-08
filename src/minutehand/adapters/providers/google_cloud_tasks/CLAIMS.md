# Google Cloud Tasks: where each behaviour comes from

**Documented** means Google's public reference says so (linked; the reference moved to `docs.cloud.google.com`,
abbreviated here as `tasks/` for https://docs.cloud.google.com/tasks/docs/reference/rest/v2/ and `rpc` for
https://docs.cloud.google.com/tasks/docs/reference/rpc/google.cloud.tasks.v2). **Observed** means it rests on
Google's machine-readable surface or its own client library as installed (`google-cloud-tasks` 2.26, its REST
transport and its default gRPC one), read or exercised in the tests. **Minutehand's** means it is Minutehand's own
rule for a simulated run, not a claim about Google. Nothing here is a choice the reference leaves open: where the
reference is silent, the call is refused 501 (UNIMPLEMENTED), naming what is not served.

Tests are in `tests/providers/google_cloud_tasks/`: `test_cloud_tasks.py` (R), `test_cloud_tasks_grpc.py` (G),
`test_cloud_tasks_fidelity.py` (F), `test_cloud_tasks_coverage.py` (C), and the whole runs
`tests/e2e/test_cloud_tasks_run.py`.

**Minutehand deliberately does not enforce credentials.** No access token, OIDC token or API key is read: any
`Authorization`, or none, is answered (C `test_any_credential_or_none_is_answered`). IAM methods are refused by name.

**Refusal messages.** Google's own wording of these errors is in no reference, so each refusal's message is the
reference's sentence for the rule it refuses by (`wire.py` names each sentence's page), after the resource it
concerns; the status, which a client acts on, is the reference's. Where the rule is a field's format, the message
starts with google.rpc.Code's own description of INVALID_ARGUMENT, "The client specified an invalid argument."
(https://github.com/googleapis/googleapis/blob/master/google/rpc/code.proto).

## The surface

| Claim | Class | Test | Source |
|---|---|---|---|
| REST: the 24 methods of the v2 discovery document (revision 20260930, committed as `tests/data/google_cloud_tasks/cloudtasks_v2_discovery.json`); 8 served (`queues.create`, `list`, `get`, `delete`; `queues.tasks.create`, `list`, `get`, `delete`) and 16 refused 501 naming the method id and why | observed | C `test_every_rest_method_is_served_or_refused_by_name_on_its_own_route`, `test_each_refused_rest_method_called_through_the_proxy_answers_501_naming_it` | https://cloudtasks.googleapis.com/$discovery/rest?version=v2 |
| gRPC: the 20 methods of `google.cloud.tasks.v2.CloudTasks` (the installed client's `gapic_metadata.json`); the same 8 served from the operations the REST routes use, over the same world; 12 refused UNIMPLEMENTED naming the method | observed | C `test_every_grpc_method_is_served_or_refused_by_name`, `test_each_refused_grpc_method_called_by_googles_client_answers_unimplemented_naming_it`; G every test | `google/cloud/tasks_v2/gapic_metadata.json` |
| The client asks for enums as numbers (`$alt=json;enum-encoding=int`): `httpMethod: 1` is POST | observed | R `test_a_task_created_by_googles_client_is_booked_at_its_schedule_time_and_read_back` | the client's REST transport |
| A refusal over gRPC ends with the status Google's REST error names (`NOT_FOUND`, `ALREADY_EXISTS`, `INVALID_ARGUMENT`, `UNIMPLEMENTED`) and the same message | documented | G `test_grpc_refusals_carry_the_status_cloud_tasks_refuses_with_and_are_recorded_refused` | https://google.aip.dev/193 |
| A resource name a gRPC request carries that is not of its documented format is INVALID_ARGUMENT, with the format's sentence | documented | G the same | `tasks/projects.locations.queues.tasks` (Task.name); code.proto (INVALID_ARGUMENT: "a malformed file name") |

## Queues

| Claim | Class | Test | Source |
|---|---|---|---|
| A queue id is letters, digits or hyphens, at most 100 characters | documented | F `test_names_outside_the_documented_id_formats_are_refused` | `tasks/projects.locations.queues.tasks` (Task.name, QUEUE_ID) |
| A queue name already in use is ALREADY_EXISTS | documented | G `test_queues_over_grpc_are_created_read_listed_and_deleted_with_their_tasks` | https://google.aip.dev/133 |
| A missing queue is NOT_FOUND, "The queue must already exist." | documented | R `test_a_task_in_a_queue_that_does_not_exist_is_refused_404` | `rpc` (CreateTaskRequest.parent) |
| Defaults: 100 attempts, 0.1 s to 3600 s, 16 doublings; 500 dispatches a second, 1000 concurrent, burst 100 | documented | F `test_a_queues_rate_limits_are_read_back_as_sent_or_as_googles_defaults` | https://docs.cloud.google.com/tasks/docs/configuring-queues (the `describe` output) |
| `rateLimits` and `retryConfig` are read back as sent; `maxBurstSize` is output only, and left out for a rate the caller set (the system's figure for it is not documented) | documented | F the same | `tasks/projects.locations.queues` (RateLimits) |
| `maxAttempts` below -1 is INVALID_ARGUMENT | documented | F `test_names_outside_the_documented_id_formats_are_refused` | `tasks/RetryConfig` ("Must be greater than or equal to -1") |
| `state` is output only: ignored on input, `RUNNING` on output | documented | G the queue test | `tasks/projects.locations.queues` (Queue.state) |
| Deleting a queue deletes its tasks | documented | G the queue test | `rpc` (DeleteQueue: "This command will delete the queue even if it has tasks in it") |
| Re-creating a queue within 3 days of deleting it is refused 501, naming the tombstone window: the reference says only that create "may appear to recreate the queue" then | documented (the window), Minutehand's (the refusal) | F `test_a_queue_recreated_within_its_tombstone_window_is_refused_by_name` | `tasks/projects.locations.queues/delete` |
| `queues.list` and `tasks.list` page: `pageSize` up to 9800 and 1000, the maximum when left out or exceeded, `nextPageToken` when more remain | documented | F `test_tasks_are_listed_a_page_at_a_time_and_a_filter_is_refused_by_name` | `tasks/projects.locations.queues/list`, `tasks/projects.locations.queues.tasks/list` |
| A `filter` or `readMask` on `queues.list` is refused 501 by name | Minutehand's | F the same | — |

## Tasks

| Claim | Class | Test | Source |
|---|---|---|---|
| A task names its queue followed by `/tasks/<id>`; an id is letters, digits, hyphens or underscores, at most 500 characters | documented | F `test_names_outside_the_documented_id_formats_are_refused` | `tasks/projects.locations.queues.tasks` (Task.name) |
| A task created without a name is given "a random unique task id": here 20 digits drawn from the queue and the log position, unique in the run and the same on replay | documented (random, unique), Minutehand's (drawn from the log, for replay) | F `test_a_task_created_without_a_name_is_given_an_id_of_digits_that_does_not_run_in_sequence` | `tasks/projects.locations.queues.tasks/create` |
| A name in use, or used by a task deleted or run within 24 hours, is ALREADY_EXISTS | documented (the reference's bound, "up to 24 hours") | R `test_a_deleted_task_is_cancelled_and_its_name_stays_taken_for_a_day_is_refused` | `tasks/projects.locations.queues.tasks/create` |
| `scheduleTime` in the past, or left out, dispatches at once | documented | R `test_a_task_created_by_googles_client_is_booked_at_its_schedule_time_and_read_back` | `tasks/projects.locations.queues.tasks/create` |
| `createTime` is whole seconds | documented | F `test_create_time_is_whole_seconds_and_the_first_attempt_keeps_only_its_dispatch_time` | `tasks/projects.locations.queues.tasks` (Task.createTime) |
| `dispatchDeadline` defaults to 10 minutes and must be in [15 seconds, 30 minutes] | documented | F `test_a_dispatch_deadline_outside_fifteen_seconds_to_thirty_minutes_is_refused` | `tasks/projects.locations.queues.tasks` (Task.dispatchDeadline) |
| A body only with POST, PUT or PATCH | documented | F `test_a_body_on_a_get_task_is_refused_invalid_argument` | `tasks/projects.locations.queues.tasks` (HttpRequest.body) |
| The URL must start `http://` or `https://` | documented | (validated on every create) | `tasks/projects.locations.queues.tasks` (HttpRequest.url) |
| The `BASIC` view leaves out the body and headers ("fields which can be large or can contain sensitive data"); `FULL` carries them | documented | R `test_a_task_created_by_googles_client_is_booked_at_its_schedule_time_and_read_back` | `tasks/projects.locations.queues.tasks` (View) |
| `firstAttempt` carries only `dispatchTime`; `lastAttempt` carries `scheduleTime`, `dispatchTime` and, once answered, `responseTime` | documented | F `test_create_time_is_whole_seconds_and_the_first_attempt_keeps_only_its_dispatch_time` | `tasks/projects.locations.queues.tasks` (Task.firstAttempt, Attempt) |
| An attempt's `responseStatus` is left out: how the handler's HTTP status becomes a google.rpc.Status is in no reference | Minutehand's | F the same | — |

## Delivery

| Claim | Class | Test | Source |
|---|---|---|---|
| A task's `scheduleTime` is a wake on the run's clock; it is delivered as an HTTP request to its URL, with its method, headers and body as sent | Minutehand's (the clock), documented (the request) | R `test_a_task_is_delivered_to_its_handler_as_cloud_tasks_delivers_it_and_a_2xx_completes_it` | https://docs.cloud.google.com/tasks/docs/creating-http-target-tasks |
| `X-CloudTasks-QueueName`, `-TaskName`, `-TaskRetryCount` (attempts before this one), `-TaskETA` (the attempt's schedule time, epoch seconds) on every delivery | documented | R the same; F `test_delivery_headers_count_retries_and_executions_and_name_the_previous_response` | creating-http-target-tasks (handler headers) |
| `X-CloudTasks-TaskExecutionCount` counts the handler's answers but 5XX ones; `X-CloudTasks-TaskPreviousResponse` is the previous attempt's HTTP status | documented | F `test_delivery_headers_count_retries_and_executions_and_name_the_previous_response` | creating-http-target-tasks |
| `User-Agent` is `Google-Cloud-Tasks`; `Host` and `Content-Length` are computed; `X-Google-*` and `X-AppEngine-*` the task carries are not sent; no `Content-Type` is added | documented | F `test_google_only_headers_a_task_carries_are_not_sent_and_its_own_are`, `test_a_task_body_is_delivered_with_no_content_type_cloud_tasks_did_not_set` | `tasks/projects.locations.queues.tasks` (HttpRequest.headers: "Content-Type won't be set by Cloud Tasks") |
| The handler has the task's `dispatchDeadline` of real time to answer, then the attempt fails | documented | none (a deadline is at least 15 s) | `tasks/projects.locations.queues.tasks` (Task.dispatchDeadline) |
| A 2xx completes the task; any other answer, or none, is retried | documented | R `test_a_handler_that_fails_is_retried_with_the_queues_backoff_until_its_attempts_run_out` | creating-http-target-tasks |
| Backoff starts at `minBackoff`, doubles `maxDoublings` times, then grows linearly by 2^maxDoublings × minBackoff, and holds at `maxBackoff` | documented | F `test_backoff_doubles_then_grows_linearly_then_holds_at_its_maximum` | `tasks/RetryConfig` (maxDoublings: "10s, 20s, 40s, 80s, 160s, 240s, 300s, 300s") |
| A task is given up only once both `maxAttempts` and `maxRetryDuration` are spent (an unlimited one counts as spent); with both unlimited, at the 31-day retention limit | documented | F `test_a_task_is_given_up_only_once_both_its_attempts_and_its_retry_duration_are_spent`, `test_unlimited_attempts_and_duration_retry_until_the_retention_limit` | `tasks/RetryConfig` (maxAttempts); configuring-queues; https://docs.cloud.google.com/tasks/docs/quotas |
| A delivery is taken (`ConfirmsDelivery`) once the handler answers, whatever it answers | Minutehand's | R `test_a_delivery_answered_503_is_taken_as_soon_as_it_is_answered` | — |

## What it does not do

- **gRPC without `minutehand[grpc]`.** The gRPC server and Google's messages come with the extra; without it a gRPC
  call is answered UNIMPLEMENTED, saying so.
- **Signed tokens.** A task with `oidcToken` or `oauthToken` answers 501: nothing here holds Google's keys, and a
  handler that verifies its token would refuse anything else.
- **App Engine tasks**, `tasks.run`, `tasks.buffer`, batch calls, pausing, resuming, purging and updating queues,
  locations, operations, IAM and CMEK answer 501 by name.
- **Rate limits** are kept and read back, and not enforced.
- **`X-CloudTasks-TaskRetryReason`** is not sent: its values are not documented.
- **A task URL elsewhere than this machine** is never called: the attempt fails, saying so, and is retried
  (Minutehand's: a run reaches nothing outside it).
