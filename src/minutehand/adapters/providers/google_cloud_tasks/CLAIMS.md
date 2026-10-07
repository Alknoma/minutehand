# Google Cloud Tasks: where each behaviour comes from

**Documented** means Google's public reference says so (linked). **Observed** means it rests on Google's own client
library as installed (`google-cloud-tasks` 2.26, its REST transport and its default gRPC one), read or exercised in
the tests. **Chosen** means
the reference leaves it open and this provider picked, saying what.

Tests are in `tests/providers/google_cloud_tasks/test_cloud_tasks.py`, `test_cloud_tasks_grpc.py` and
`tests/e2e/test_cloud_tasks_run.py`.

| Claim | Class | Test | Source |
|---|---|---|---|
| The REST routes: `POST/GET v2/{parent}/queues`, `GET/DELETE v2/{queue}`, `POST/GET v2/{queue}/tasks`, `GET/DELETE v2/{task}` | observed | every client test | the client's `transports/rest_base.py` |
| The client asks for enums as numbers (`$alt=json;enum-encoding=int`): `httpMethod: 1` is POST | observed | `test_a_task_created_by_googles_client_is_booked_at_its_schedule_time_and_read_back` | the client's REST transport |
| A task names its queue's resource name followed by `/tasks/<id>`; a name the caller leaves out is assigned | documented | `test_a_task_created_by_googles_client_is_booked_at_its_schedule_time_and_read_back` | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/create |
| `scheduleTime` in the past, or left out, dispatches at once | documented | the same | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks#Task |
| The `BASIC` view leaves the body and headers out; `FULL` carries them | documented | the same | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks#View |
| A name in use is refused `ALREADY_EXISTS` | documented | `test_a_deleted_task_is_cancelled_and_its_name_stays_taken_for_an_hour_is_refused` | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/create |
| A name whose task was deleted or ran is refused for about an hour after | documented | the same | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/create |
| The exact wording of each refusal message | chosen | the refusal tests | not verified against the service |
| A task in a queue that does not exist is a 404 | documented | `test_a_task_in_a_queue_that_does_not_exist_is_refused_404` | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/create |
| Delivery carries `X-CloudTasks-QueueName`, `-TaskName`, `-TaskRetryCount`, `-TaskExecutionCount`, `-TaskETA` and the `Google-Cloud-Tasks` user agent | documented | `test_a_task_is_delivered_to_its_handler_as_cloud_tasks_delivers_it_and_a_2xx_completes_it` | https://cloud.google.com/tasks/docs/creating-http-target-tasks#handler |
| A 2xx completes the task; any other answer, or none, is retried | documented | `test_a_handler_that_fails_is_retried_with_the_queues_backoff_until_its_attempts_run_out` | https://cloud.google.com/tasks/docs/creating-http-target-tasks |
| Backoff starts at `minBackoff` and doubles up to `maxBackoff`; past `maxDoublings` it doubles no more | documented, simplified | the same | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues#RetryConfig. The service grows linearly after the doublings; this provider holds the last doubled value. |
| A task is given up after `maxAttempts` or `maxRetryDuration` | documented | the same | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues#RetryConfig |
| gRPC: `google.cloud.tasks.v2.CloudTasks` `CreateQueue`, `ListQueues`, `GetQueue`, `DeleteQueue`, `CreateTask`, `ListTasks`, `GetTask`, `DeleteTask`, answered by the operations the REST routes answer by, over the same world | observed | every test in `test_cloud_tasks_grpc.py`; `test_a_task_the_agent_defers_over_grpc_calls_it_back_as_one_deferred_over_rest_does` | the client's `transports/grpc.py` |
| A refusal over gRPC ends with the status Google's REST error names (`NOT_FOUND`, `ALREADY_EXISTS`, `INVALID_ARGUMENT`, `UNIMPLEMENTED`) and the same message | documented | `test_grpc_refusals_carry_the_status_cloud_tasks_refuses_with_and_are_recorded_refused` | https://cloud.google.com/apis/design/errors#handling_errors |
| A resource name a gRPC request carries that is not a location, queue or task name is refused `INVALID_ARGUMENT` | chosen | the same | the wording is not verified against the service |
| Defaults: 100 attempts, 0.1 s to 3600 s, 16 doublings, a 10-minute dispatch deadline | documented | none | https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues#RetryConfig |

## What it does not do

- **gRPC without `minutehand[grpc]`.** The gRPC server and Google's messages come with the extra; without it a gRPC
  call is answered UNIMPLEMENTED, saying so. Every method not listed above, and paging, are UNIMPLEMENTED or
  ignored over gRPC as over REST.
- **Signed tokens.** A task with `oidcToken` or `oauthToken` answers 501: nothing here holds Google's keys, and a
  handler that verifies its token would refuse anything else.
- **App Engine tasks**, `tasks:run`, batch calls, pausing, purging, IAM and CMEK answer 501.
- **Rate limits** (`rateLimits`) are kept and not enforced.
- **A task URL elsewhere than this machine** is never called: the attempt fails, saying so, and is retried.
