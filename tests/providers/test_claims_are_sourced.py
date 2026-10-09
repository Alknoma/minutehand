"""Every behaviour a fake keeps is its vendor's: each row of a provider's `CLAIMS.md` is **documented**, citing the
vendor's page, or **observed**, citing the recorded evidence of the real service (a public report holding the real
answer, or a capture committed under `tests/data/`). A test of the fake itself is not evidence of the service, so a
row whose only pointer is its own test is unsourced. A row of any other class (chosen, simplified, a mix), a table
of behaviours with no class, a bullet under a heading that says "unsourced", and the words "not verified" are
unsourced too.

`OPEN` is the ratchet: the unsourced rows found today, provider by provider, each to be fixed to match the vendor or
turned into an explicit refusal. It may only shrink: a row fixed and still listed fails, and so does a new one."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = ROOT / "src" / "minutehand" / "adapters" / "providers"
URL = re.compile(r"https?://[^\s|)>\]]+")
CAPTURE = re.compile(r"`?(tests/data/[^\s`|]+)`?")
NAMED_PAGE = re.compile(r"^- ([^:]+): (https?://\S+)\s*$")
NOT_KEPT = ("not carried over",)
"""A section listing an older stand-in's behaviours that were dropped describes nothing the fake does."""


@dataclass(frozen=True)
class Row:
    provider: str
    claim: str
    why: str


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split(" | ")]


def _sourced(cells: list[str], pages: dict[str, str]) -> bool:
    """Whether the row names a vendor page, a recorded capture that is in the repository, or a page its file names."""
    text = " ".join(cells)
    if URL.search(text):
        return True
    if any((ROOT / path).is_file() for path in CAPTURE.findall(text)):
        return True
    return any(cell in pages for cell in cells)


def unsourced(claims: Path) -> list[Row]:
    """Each row of `claims` that is neither documented with its page nor observed with its evidence."""
    provider = claims.parent.name
    lines = claims.read_text(encoding="utf-8").splitlines()
    pages = {m.group(1).strip(): m.group(2) for line in lines if (m := NAMED_PAGE.match(line))}
    found: list[Row] = []
    section = ""
    header: list[str] | None = None
    for line in lines:
        if line.startswith("#"):
            section, header = line.lstrip("#").strip().lower(), None
            continue
        if "not verified" in line.lower():
            found.append(Row(provider, line.strip()[:120], "says it is not verified"))
        elif "unsourced" in section and line.startswith("- "):
            found.append(Row(provider, line[2:].strip()[:120], "listed as unsourced"))
        if not line.startswith("|"):
            header = None
            continue
        if header is None:
            header = _cells(line)
            continue
        if set(line) <= set("|-: "):
            continue
        if any(section.startswith(s) for s in NOT_KEPT):
            continue
        cells = _cells(line)
        if "Class" not in header:
            found.append(Row(provider, cells[0], "a behaviour in a table with no class"))
            continue
        kind = cells[header.index("Class")]
        claim = cells[header.index("Claim")]
        if kind not in ("documented", "observed"):
            found.append(Row(provider, claim, f"class {kind!r}"))
        elif not _sourced(cells, pages):
            found.append(Row(provider, claim, f"{kind} with no page or recorded evidence"))
    return found


OPEN: dict[str, tuple[str, ...]] = {
    "asana": (
        "A call with no token, or any token, is answered, as the agent unless the token is a seeded or minted one",
        "A field `opt_fields` names that this provider never answers (one Asana has and is not served, one Asana's document does not give the resource, a path into a value) is refused by name",
        "A task's name and notes, and a custom field's number, come back as they were sent",
        "`due_on` of a task given `due_at`, and `date` of a date field given `date_time`, is that moment's date in UTC",
        'A required field missing is "<field>: Missing input"; a task created naming no workspace, project or parent "You should specify one of workspace, parent, projects"; a project in an organization with no team "Missing required team field"; a membership with no project "memberships: [n]: project: Missing required field"',
        'A due date that is not one is "due_on: Date must be in ISO-8601 (yyyy-mm-dd) format, not: <value>"',
        "Completing a task changes no section and no custom field",
        "`completed` must be a JSON boolean",
        "Every case no page, recording or report gives Asana's answer to is refused by name: a parent that is the task or its subtask; a comment of nothing but spaces; a task put in a section of a project it is not in; a custom field setting already on (or not on) the project; teams of a workspace that is not an organization; a write body with no `data` object; a value of the wrong JSON type with no reported words (`name`, `completed`, `projects` as a string, ...); a limit, count or boolean flag out of range; an offset without a limit; an unparseable `due_at`; `workspace`, `memberships` or `parent` written on an update; both `projects` and `memberships` on a create; enum and people values naming nothing; the OAuth code grant",
        "A full record leaves out what Asana's OpenAPI document marks [Opt In] (`num_subtasks`, `dependencies`, `dependents`; a team's `description`; a project membership's `parent` and `project`) until `opt_fields` names it",
        "A task's `dependencies` and `dependents`, asked for, are empty",
        "A full task holds its custom fields full; a listed custom field is compact, without `resource_subtype` or `precision`",
        "`html_notes` is refused by name, written or read",
        'A new project has one "Untitled section" and no custom fields',
    ),
    "aws": (
        "The surface is botocore's `scheduler` model (2021-06-30, 12 operations) and `sqs` model (2012-11-05, 23 operations); each operation is served or refused by name, and the provider's route table is the model's `http.method` and `http.requestUri`",
        "Served: Scheduler `CreateSchedule`, `UpdateSchedule`, `DeleteSchedule`, `GetSchedule`, `ListSchedules`; SQS `CreateQueue`, `GetQueueUrl`, `GetQueueAttributes`, `ListQueues`, `SendMessage`, `ReceiveMessage`, `DeleteMessage`, `DeleteMessageBatch`, `ChangeMessageVisibility`",
        "Every other operation of either model answers 501 `NotImplemented`, naming the operation and why (`wire.REFUSED_BECAUSE`), in JSON for the JSON protocols and in the query protocol's `ErrorResponse` XML for a form-encoded request",
        "Any other AWS host (STS, Lambda, ...) answers 501 naming the host; moto's `/moto-api` answers 501, as no part of AWS",
        "Any credential, or none, is answered, even when moto is told to check IAM",
        "Each run is an AWS account of its own (a random twelve-digit id), so runs in one process never share a queue",
        "`at(yyyy-mm-ddThh:mm:ss)` fires once at that wall time in `ScheduleExpressionTimezone` (UTC when none), even when already past",
        "`rate(value unit)`: a positive integer and any of `minute`, `minutes`, `hour`, `hours`, `day`, `days`; a day is 24 hours",
        "A rate schedule with no `StartDate` invokes its target at once, then every interval; with one, first at `StartDate`",
        "`cron(minutes hours day-of-month month day-of-week year)` with `,` `-` `*` `/` `?`, `JAN-DEC`, `SUN-SAT` (1 = Sunday), years 1970-2199, evaluated in its timezone",
        "`*` in both day fields is `ValidationException`",
        "A cron time daylight saving skips is skipped; one it repeats runs once",
        "`StartDate` and `EndDate` bound a recurring schedule, with no limit on how far back `StartDate` may be; one-time schedules ignore both",
        "`State` `DISABLED` books nothing; a schedule is `ENABLED` by default, also after an `UpdateSchedule` that leaves `State` out",
        "`UpdateSchedule` replaces the whole schedule: a field it leaves out takes its default (`ActionAfterCompletion` none, `State` ENABLED)",
        "`ActionAfterCompletion` `DELETE` deletes a one-time schedule after it fires, and a recurring one after its last invocation before `EndDate`",
        "`FlexibleTimeWindow` `FLEXIBLE` needs `MaximumWindowInMinutes` (1 to 1440); the target is invoked at the scheduled time, which is within the window",
        "A schedule is read back with the `Target` and `ScheduleExpressionTimezone` it was sent with, nothing added",
        "A `CreateSchedule` repeated with its `ClientToken` and the same request answers the same `ScheduleArn`",
        'A schedule in a group other than `default` is `ResourceNotFoundException`, "The request references a resource which does not exist." (no other group can be created here)',
        "The schedule's ARN, `CreationDate` and `LastModificationDate` are assigned, the dates from the run's clock",
        "A schedule's next occurrence is a wake on the run's clock; when it fires, its `Target.Input` is put on its SQS queue (with `SqsParameters.MessageGroupId` for FIFO), as the agent's own `ReceiveMessage` finds it",
        "A delivery is taken (`ConfirmsDelivery`) when the agent deletes its message, not when it receives it",
        "Every time moto reads (message `SentTimestamp`, visibility, `DelaySeconds`, queue timestamps, schedule dates, a long poll's end) is the run's clock",
        "A received message is invisible for the queue's visibility timeout (30 seconds by default) of the run's time, then received again",
        "`ChangeMessageVisibility` hides a received message for its new timeout of the run's time",
        "`DelaySeconds` hides a message until the run's clock passes it",
        "A long poll (`WaitTimeSeconds`, or the queue's `ReceiveMessageWaitTimeSeconds`) that finds a visible message answers it at once",
        "A long poll that finds none waits on the run's clock: held by the proxy, it is answered when the run's clock reaches the end of its wait (with nothing), or sooner with a message once one is visible: sent by another call, delivered by a schedule, or visible again as a delay or a visibility timeout runs out (moto reads a message visible a millisecond after its timeout); no poll waits in real time. In a world whose clock nothing moves while a call waits (`minutehand serve`) it is refused 501 by name, without waiting",
        "`MaxNumberOfMessages` is 1 to 10, else `InvalidParameterValue` (moto's check)",
        "`DeleteMessage` takes the receipt handle of a receive; an old one may still delete (moto: it does)",
        "Queue attributes the caller set are answered as it wrote them",
        "A message's body and attributes come back as sent; `MD5OfMessageBody` and `MD5OfMessageAttributes` are AWS's algorithm",
        "`SenderId` is left out of a received message's attributes: AWS answers the sender's principal there, and Minutehand, which enforces no credentials, knows none",
        "`MessageId`, receipt handles and queue URLs are assigned (moto's forms)",
        "checks SigV4 signatures and IAM policies once told to",
        "picks the service from the signature's credential scope, and guesses for an unsigned request (an unsigned JSON SQS call is answered 200 by another service and the message is lost)",
        "reads the machine's clock for every timestamp, visibility, delay and long poll",
        "writes `RetryPolicy` `{86400, 185}` into a target sent without one",
        'answers `ScheduleExpressionTimezone` "UTC" for a schedule sent without one',
        "`UpdateSchedule` keeps the old `ActionAfterCompletion` and nulls a left-out `State`",
        "ignores `ClientToken`, so a retried create is `ConflictException`",
        "a schedule in a group that does not exist is a `KeyError` (500)",
        "re-serialises `RedrivePolicy` (`maxReceiveCount` becomes a number) and drops a `Policy` with no `Statement`",
        "answers `SenderId` `AIDAIT2UOQQY3AUEKVGXU` for every message",
        "answers JSON `null` for absent members (`Description: null`) and extra members in `ListSchedules` summaries",
        'refuses a cron schedule whose `StartDate` is more than 5 minutes before now ("The StartDate you specify cannot be earlier than 5 minutes ago.")',
        "waits for a long poll on its own clock",
    ),
    "github": (
        "…its `documentation_url` is `https://docs.github.com/rest`",
        "An error body is `message`, `documentation_url` and `status` (a string), with `errors` when there are some",
        'An unknown `X-GitHub-Api-Version` is 400 "Bad Request", the reason a sentence in `errors`',
        "…a 304 to an authorized call spends nothing; without a credential it spends",
        '…the `ETag` is weak, `W/"` and 64 hex digits',
        "A repository's URL templates, `git_url`, `ssh_url`, `clone_url`, `svn_url`, `mirror_url: null`",
        "`has_pages` false, `has_downloads` true",
        "A commit's author and committer names are the accounts' declared names (their own or their person's); an account with none refuses the seed, naming the commit. Emails are the declared ones, else GitHub's no-reply address",
        "A repository listing's `Link` points at `/repositories/{id}/…`, which answers as `/repos/{owner}/{repo}/…`",
        "No file outside a commit: a seed repository with files and no commit is refused, naming it, and so is a file no commit's `paths` names, in a seed or in a fragment added to an open world",
        "`/languages` leaves out prose and vendored paths",
        'Contents at a ref naming no commit is 404 "No commit found for the ref …", pointing at `https://docs.github.com/v3/repos/contents/`',
        'Commits from a sha naming nothing are 404 "Not Found" (corrected: the old emulator said "No commit found for SHA: …")',
        "A listed commit has author email and `html_url`, and no `files`",
        'A blob sha that is not 40 hex digits is 422 "The sha parameter must be exactly 40 characters and contain only [0-9a-f]." with no `errors` (corrected: the old emulator said "Validation Failed")',
        "Search matches whole tokens, never a substring",
        "Every bare term must match",
        "An empty `q` is 422 `missing`",
        "An unresolvable repository is `null` with a NOT_FOUND error",
        "…and the message says a query attribute must be specified",
        "`GET /repos/{owner}/{repo}/commits?sha=`",
        "`GET /repos/{owner}/{repo}/contents/{path}?ref=`",
        "`GET /repos/{owner}/{repo}/git/trees/{tree_sha}`",
        "GraphQL `Repository.object(expression:)`",
        "`GET /repos/{owner}/{repo}/commits/{ref}`, compare (`BASE...HEAD`)",
        'An unknown or revoked token, 401 "Bad credentials"',
        "An app's JWT on a route that needs a user or an installation",
        "`Basic` with a password, and `Bearer` with nothing after it, 401",
        "A classic token without the `repo` scope reaches no private repository",
        "A fine-grained token reaches only the repositories it selected",
        "An installation-token exchange for an app that is not installed, or with a bad JWT",
    ),
    "google_cloud_tasks": (
        "gRPC: the 20 methods of `google.cloud.tasks.v2.CloudTasks` (the installed client's `gapic_metadata.json`); the same 8 served from the operations the REST routes use, over the same world; 12 refused UNIMPLEMENTED naming the method",
        "The client asks for enums as numbers (`$alt=json;enum-encoding=int`): `httpMethod: 1` is POST",
        "A resource name a gRPC request carries that is not of its documented format is INVALID_ARGUMENT, with the format's sentence",
        "A queue id is letters, digits or hyphens, at most 100 characters",
        'A missing queue is NOT_FOUND, "The queue must already exist."',
        "`rateLimits` and `retryConfig` are read back as sent; `maxBurstSize` is output only, and left out for a rate the caller set (the system's figure for it is not documented)",
        "`maxAttempts` below -1 is INVALID_ARGUMENT",
        "`state` is output only: ignored on input, `RUNNING` on output",
        "Deleting a queue deletes its tasks",
        'Re-creating a queue within 3 days of deleting it is refused 501, naming the tombstone window: the reference says only that create "may appear to recreate the queue" then',
        "`queues.list` and `tasks.list` page: `pageSize` up to 9800 and 1000, the maximum when left out or exceeded, `nextPageToken` when more remain",
        "A `filter` or `readMask` on `queues.list` is refused 501 by name, over REST and gRPC",
        "A task names its queue followed by `/tasks/<id>`; an id is letters, digits, hyphens or underscores, at most 500 characters",
        'A task created without a name is given "a random unique task id". That, TASK_ID\'s characters and length, and that ids should be "approximately uniform" rather than sequential are all Google documents about a generated id; no reference or public recording shows one. The id here keeps to exactly that: unique in the run, of TASK_ID\'s characters, not sequential (decimal digits of a hash of the queue and the log position, so a replay assigns the same); nothing about its form is claimed, and a client must not parse it',
        "A name in use, or used by a task deleted or run within 24 hours, is ALREADY_EXISTS",
        "`scheduleTime` in the past, or left out, dispatches at once",
        "`createTime` is whole seconds",
        "`dispatchDeadline` defaults to 10 minutes and must be in [15 seconds, 30 minutes]",
        "A body only with POST, PUT or PATCH",
        'The `BASIC` view leaves out the body and headers ("fields which can be large or can contain sensitive data"); `FULL` carries them',
        "`firstAttempt` carries only `dispatchTime`; `lastAttempt` carries `scheduleTime`, `dispatchTime` and, once answered, `responseTime`",
        "An attempt's `responseStatus` is left out: how the handler's HTTP status becomes a google.rpc.Status is in no reference",
        "A task's `scheduleTime` is a wake on the run's clock; it is delivered as an HTTP request to its URL, with its method, headers and body as sent",
        "`X-CloudTasks-QueueName`, `-TaskName`, `-TaskRetryCount` (attempts before this one), `-TaskETA` (the attempt's schedule time, epoch seconds) on every delivery",
        "`X-CloudTasks-TaskExecutionCount` counts the handler's answers but 5XX ones; `X-CloudTasks-TaskPreviousResponse` is the previous attempt's HTTP status",
        "`User-Agent` is `Google-Cloud-Tasks`; `Host` and `Content-Length` are computed; `X-Google-*` and `X-AppEngine-*` the task carries are not sent; no `Content-Type` is added",
        "The handler has the task's `dispatchDeadline` of real time to answer, then the attempt fails `DEADLINE_EXCEEDED` and is retried",
        "A 2xx completes the task; any other answer, or none, is retried",
        "Backoff starts at `minBackoff`, doubles `maxDoublings` times, then grows linearly by 2^maxDoublings \xd7 minBackoff, and holds at `maxBackoff`",
        "A delivery is taken (`ConfirmsDelivery`) once the handler answers, whatever it answers",
    ),
    "google_workspace": (
        "A Doc's plain-text export and its Docs body hold the same characters",
        "A plain-text export opens with a byte-order mark (observed); it ends lines CRLF (seen only indirectly)",
        "A Doc created with no text is a section break and one newline ending at 2, without its name in it",
        "A chunk that does not start where the received bytes end, or names a total other than the one declared, is a 400 `badContent`",
        "A new presentation holds one slide and its title",
        "A `From` that is not the account's own address is replaced (observed); a send without `Date` or `Message-ID` is given them (unsourced)",
        "That refusal's status and reason (400 `channelIdNotUnique`, kept from the Drive fake)",
        "Stopping a channel already stopped is that 404 too; a Drive channel named to Calendar's `channels.stop`, or the reverse, is not found",
        'A push answered 500, 502, 503 or 504 is retried "with exponential backoff"; this fake records it and does not retry, since no schedule is documented or recorded',
        "`sendUpdates` and `acl.insert`'s `sendNotifications` are served without writing the emails Google sends to guests",
        "A history id never expires. Gmail answers a `startHistoryId` outside the range it keeps with a 404 and documents",
        "A part's bytes are served inline in `body.data`, never as an attachment id.",
    ),
    "jira": (
        "A body missing `key`, `name` or the lead is a 400 naming each, keyed `projectKey`, `projectName`, `leadAccountId`",
        "An empty body names every missing field in one 400",
        "With neither a type nor a template, the 400 names `projectTypeKey`",
        "A template with no type is accepted, and the project takes the template's type",
        "A template of another type is a 400 on `projectTemplateKey`",
        "Every template in `projectTemplateKey`'s enum is accepted; any other is a 400",
        "A key that is lowercase, starts with a digit, holds `_` or `-`, is one letter, or is longer than ten is a 400 on `projectKey`",
        "A key already held is a 400 on `projectKey`",
        "A name already held (any case) is a 400 on `projectName`",
        "An email address as `leadAccountId`, or any id naming no account, is a 400 on that field; the caller's own account is a valid lead",
        "`assigneeType` is kept as sent and read back; a value outside `PROJECT_LEAD`/`UNASSIGNED` is a 400",
        "Any other documented property (`categoryId`, the schemes, `avatarId`, `url`, `lead`) is refused by name and creates nothing",
        "Exactly the keys asked for come back, each with `havePermission`",
        "A permission the caller lacks is answered 200 with `havePermission: false` (here only a project permission where it sees no project)",
        "No `permissions`, an unknown key, a lowercase or spaced key: a 400 for the whole call",
        "A project read, its statuses, or `mypermissions?projectKey=` naming a malformed or unseen key is a 404",
        "An issue create naming a malformed project key is a 400 on `project`",
        "An edit refused for one field is a 400 and writes none of its fields",
        "`GET`/`POST /rest/api/3/search` answer 410 pointing at `/search/jql` and write nothing",
        "Comments come 100 a page by default; `orderBy` other than `created` is a 400; `-created` is newest first",
        "`user/search` with both `query` and `accountId` is a 400",
        "`user/assignable/search` with neither `query` nor `accountId`, or both, is a 400; `accountId` alone finds that account",
        "`users/search` pages 50 by default, at most 1000",
        "`project/search` orders by key by default, takes `orderBy=key` or `name` (`-` reverses), `keys`, `id` and `typeKey`, at most 100 a page, links `nextPage`, and lists description, lead, issue types and keys only when expanded; `GET /project/{key}` always includes the first three",
        "A role read takes `excludeInactiveUsers`; a role's description is the world's (empty unless seeded), not a sentence the fake writes; a group added to a role is refused by name",
        "`search/jql`: ids only by default; `-x` alone is the navigable fields less `x`; `fields` may repeat; 50 a page by default, at most 5000; `nextPageToken` on all but the last page; `names` beside the issues",
        "A query with no restriction is a 400; `maxResults` outside 1 to 5000 is a 400",
        "A function the field does not take: \"A value provided by the function 'name' is invalid for the field 'f'.\"",
        "An unknown `ORDER BY` field: \"Field 'x' does not exist or you do not have permission to view it.\"",
        '`IS` with a value other than `EMPTY`, a text query holding no word (`summary ~ "?"`), an increment that is no period (`startOfDay(-1x)`): 400 with the search reference\'s words, "Returned if the search request is invalid"',
        "`GET /issue`: every field by default; `-x` alone is every field less `x`; `expand=changelog` is most recent first (`GET /changelog` oldest first); an expansion not served is refused by name",
        "`PUT /issue?returnIssue=true` answers 200 with the issue; an `update` operation other than `set` (and a label's `add`/`remove`) is refused by name",
        '`PUT /assignee`: `"-1"` gives the project\'s default assignee, `null` unassigns, no `accountId` is a 400',
        "`GET /transitions?transitionId=` answers that transition alone",
        "A link whose comment is not an Atlassian document is a 400 and links nothing; an unknown link type is a 404",
        "`statuscategorychangedate` is when the status last changed category",
        "Boards filter by `name`, matching part of it",
        "JQL: an increment without a unit is in the function's own period; `M` and `y` are calendar months and years; `endOfWeek()`, `endOfMonth()`, `startOfYear()`, `endOfYear()` and `futureSprints()` are served",
        "JQL: a documented field (`watcher`, `component`…), function (`membersOf()`…) or operator (`WAS`, `CHANGED`) not served is refused by name (the public site answers `WAS` with no issues for an anonymous caller, `jql_was.http`; the fake does not search history)",
    ),
    "microsoft": (
        "A list of messages, a folder's included, without `$orderby` lists newest received first (`receivedDateTime desc`)",
        "A list of events, and the calendar view, without `$orderby` lists by start, earliest first",
        "A folder's children without `$orderby` list in the order the items were made",
        "Users without `$orderby` list in the directory's own order",
        "A team's channels list General first, then in the order they were made",
        "A user's chats, and `/chats`, list in the order the chats were made",
        "`/sites?search=` lists matching sites in the order they were made",
        "A team, channel or chat the world does not hold is 404 `NotFound`",
        "A user the directory does not hold is 404 `Request_ResourceNotFound`",
        "A user's token reaching another user's OneDrive, or renaming, moving or deleting a drive's root, is 403 `accessDenied`",
        "A name already taken in a folder is 409 `nameAlreadyExists`",
        "A drive, site, upload session, copy monitor or permission the world does not hold is 404 `itemNotFound`",
        "An upload fragment out of order or mismatched is 416 or 400 `invalidRange`; simple upload content over 250 MB is 413 `requestTooLarge`",
        "A drive or Teams `$skiptoken` or delta token Minutehand never gave, a folder used as a file, a missing name or role, an unknown link type or scope, and an unreadable files body are 400 `invalidRequest`",
    ),
    "notion": (
        "A call with no bearer token, or one that names no integration, is answered as the agent's integration",
        "No capability refuses a call",
        "No client secret, code, redirect URI or refresh token is refused at `/v1/oauth/token`",
        "A version other than 2022-06-28 is 501 `invalid_request` naming it",
        "Something Notion takes and this fake does not build (an operation, a version, a block type, a property type, a mention, an image not `external`, a date mention with a time or an end) is the shared not-served refusal, 501 `invalid_request` naming it",
        "A code block takes every language Notion lists",
        'A schema refusal is "<body\\|query\\|path> failed validation: <path> should be <expected>, instead was \\`<value>\\`." (an optional field\'s expectation ending "or \\`undefined\\`", a value cut at 55 characters)',
        'Search refuses a filter value other than `page`/`database`, a sort timestamp other than `last_edited_time` and an unknown direction in the recorded words; a cursor it did not issue is refused as recorded (users and database queries "The start_cursor provided is invalid: ..."; children, comments and search a non-uuid cursor as a validation failure); a page size of 0 is the default page, and comments refuse 0 and 101',
        "A seed that declares no integration is refused at seeding",
        "An update that carries another block type's body is a 400, and the block keeps its type",
        "An append whose `after` names a grandchild, not a direct child, is refused by name, and nothing is written",
        "Every other refusal no page, recording or report gives words for (a boolean or required number of the wrong type, a status option that does not exist, a read-only property written, a value of another property type, a page at the workspace's top from an internal integration, most filter shapes, a page size over 100 or below 0 outside comments, a cursor that is a uuid Notion did not issue, a comment with both or neither of parent and discussion, ...) is refused by name",
    ),
    "slack": (
        'A name Slack has no method by is HTTP 200 `{"ok":false,"error":"unknown_method","req_method":…}`',
        "A call with no token",
        "A token it never issued, of any shape",
        "A token the scenario's declared Slack sign-in does not name",
        "An app-level (`xapp-`) token on any method but `apps.connections.open`, and a bot token there",
        "A Socket Mode URL whose ticket was used, or never issued",
        "`oauth.v2.access` with no client id or secret, or a code already exchanged",
        "A file's `url_private` with no token",
        "A `response_url` whose secret part is wrong",
        "A token without the method's scope",
        "`text`, `blocks` and `attachments` read back exactly as posted or updated; a reaction's `name` exactly as sent",
        "A view's fields (`private_metadata`, `callback_id`, `external_id`, `submit_disabled`, …) read back as published",
        '`as_user=true` is refused by name: the page says it "Can only be used by classic apps" and lists `as_user_not_supported` "with workspace apps", and says nothing of a granular bot token',
        'A `thread_ts` naming a reply, or naming no message, is refused 501 by name: the page says only "Avoid using a reply\'s ts value" and lists no error for either',
        "A person's absence with a reason shows that reason as `status_text` until it ends (`status_expiration`), `users.getPresence` `away`, `dnd.info` snoozed until its end; nothing else is written into the status, and `dnd.info` for someone never away carries no schedule",
    ),
    "youtrack": (
        "No bearer token",
        "A Basic credential",
        "An unknown token once tokens are seeded (`tokensRequired`)",
        "An issued token past its hour",
        "A banned user's token",
        "Hub `oauth2/token` with a wrong secret or an unknown client",
        "Read Issue or Read Project Basic withheld",
        "Update Issue, Delete Issue or Create Issue withheld",
        "Create Project withheld",
        "Update Project on a project made through the API, or withheld",
        "Hub `/projects` listing only what the token may read",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
        "13",
        "14",
        "15",
        "16",
        "17",
        "18",
        "19",
        "20",
        "21",
        "22",
        "23",
        "24",
        "25",
        "26",
        "27",
        "28",
        "29",
        "30",
        "31",
        "32",
        "33",
        "34",
        "35",
        "36",
        "37",
        "38",
        "39",
        "40",
        "41",
        "42",
        "43",
        "44",
        "45",
        "46",
        "47",
        "48",
        "49",
        "50",
        "51",
        "52",
        "53",
        "54",
        "55",
        "56",
        "57",
        "58",
        "59",
        "60",
        "61\u201365",
        "66",
        "Every operation of the claimed resources is served or a 501 naming it (184 described: 55 served, 129 refused); a path outside them is a 501 naming it",
        "A method the description does not list for a path is a 405",
        'A path outside `/api` and `/hub/api/rest` is 404 `{"error": "Not Found", "error_description": "HTTP 404 Not Found"}`',
        'A required field left empty on a create is `{"error": "Field required", "error_description": "<Field> is required", "error_field": "<Field>"}`',
        'A numeric `shortName` is `invalid_properties` "Project ID cannot be numeric" with its `error_children`; the letters-digits-underscore rule the stand-in took from the web UI is dropped',
        "`GET /users/{login}` reads the user as `/users/{id}` does (it was a 400)",
        "`GET /users?query=` is a 501: the description's `GET /users` takes no `query` (it was filtered here)",
        "`GET /issues?customFields=<name>`, repeated, narrows the custom fields answered",
        "`wikifiedDescription`, `textPreview` and `ParsedCommand.description` are a 501 naming them: YouTrack renders or words them, and the fake answered raw text or its own words",
        "An Issue's `fields` attribute is gone: neither the Issue page nor its schema has one",
        "Names are stored as sent (project, tag, field and bundle value names were trimmed); summaries, descriptions and comments round-trip byte for byte, under any token",
    ),
}


def test_every_claim_cites_the_vendors_page_or_recorded_evidence_of_the_real_service() -> None:
    found: dict[str, list[str]] = {}
    for claims in sorted(PROVIDERS.glob("*/CLAIMS.md")):
        for row in unsourced(claims):
            found.setdefault(row.provider, []).append(row.claim)
    stale = {p: [c for c in OPEN[p] if c not in found.get(p, [])] for p in OPEN}
    new = {p: [c for c in claims if c not in OPEN.get(p, ())] for p, claims in found.items()}
    assert not {p: c for p, c in new.items() if c} and not {p: c for p, c in stale.items() if c}, (
        f"unsourced and not listed: {new}; listed but now sourced (drop them from OPEN): {stale}"
    )


def test_a_row_with_only_its_own_test_as_evidence_is_unsourced(tmp_path: Path) -> None:
    claims = tmp_path / "fake" / "CLAIMS.md"
    claims.parent.mkdir()
    claims.write_text(
        "| Claim | Class | Test | Source |\n|---|---|---|---|\n"
        "| A thing seen | observed | `test_a_thing_seen` | |\n"
        "| A thing read | documented | `test_a_thing_read` | https://vendor.example/page |\n"
        "| A thing picked | chosen | `test_a_thing_picked` | it seemed better |\n"
        "| A thing captured | observed | `test_x` | `tests/data/google_calendar_v3/discovery-retrieved-2026-10-08.json` |\n"
        "| A thing captured elsewhere | observed | `test_x` | `tests/data/nowhere.json` |\n",
        encoding="utf-8",
    )
    assert [(r.claim, r.why) for r in unsourced(claims)] == [
        ("A thing seen", "observed with no page or recorded evidence"),
        ("A thing picked", "class 'chosen'"),
        ("A thing captured elsewhere", "observed with no page or recorded evidence"),
    ]
