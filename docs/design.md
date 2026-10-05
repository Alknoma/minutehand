# Minutehand

Reconciled with the code on `integration-main` (`9d86c6c`), 2026-10-04. Every code block below is quoted from `src/minutehand/` as it stands. Each part says whether it is built and tested, built with a known limit, or designed and not built. Results of one-off spike scripts, which are not in this repo, are kept only in "Evidence".

## Abstract

Minutehand is how a team builds its own proactive agent and finds out whether it works. It runs the agent through simulated days of work against fake Slack, trackers and document stores, with people who answer late or not at all, and then measures how well the agent carried the work.

Two pillars, in this order:

1. **Evaluating proactive effectiveness.** Did the work get done, how much time did the agent itself lose, how many follow-ups came late, how often did it wake for nothing, and which known failure did it fall into. Every result names the design that fixes it.
2. **Rewind.** Any run can be restarted from any moment with the prompt, the model, the people or the world changed, and played forward again, with no change to the agent's code.

It is one process: it intercepts the agent's outbound API calls, owns the clock, plays the people and records every change. A coding agent reaches it over MCP, so "simulate it and fix what it finds" is a loop that needs no person in it.

## What exists

Tests are `def test_` functions counted per directory; `uv run pytest -q -n auto` runs every one that needs neither a package index nor Docker, with sockets disabled except to `127.0.0.1`, `::1` and `localhost`. The rest are marked `packaging` or `firestore`.

| Part | What it does | State | Tests | Known limits |
|---|---|---|---|---|
| Contracts: `domain/`, `ports/` | The models and protocols every other part is written against | Built and tested | 16 (`tests/test_scenario.py`, `tests/test_clock.py`) | `HumanAction` and `Inbox` are models nothing reads. No check declares `Needs.COMMITMENTS`. |
| World store: `adapters/store/sqlite.py` | Append-only log of events, entity versions, calls, replies and the agent's spans; every body of 512 bytes or more and every snapshot file kept once, by SHA-256; forks share it; a refused fork is discarded with the bytes only it held; `minutehand runs`, `checkpoints`, `pin`, `gc` | Built and tested | 48 (`tests/test_sqlite_store.py`, `tests/test_store_spans.py`, `tests/test_store_bodies.py`, `tests/test_store_snapshots.py`, `tests/test_housekeeping.py`) | The file carries schema version 6 in `user_version` and refuses any other. Bytes are shared within one world file (a root run and its forks), not across root runs. No catalog of runs. |
| Proxy: `adapters/proxy/` | mitmproxy embedded in the process: answers claimed hosts, tunnels, edits or records model APIs, captures declared outbound hosts, refuses the rest; hands the agent one CA bundle (public roots plus its own CA); remembers the agent's latest call for settling | Built and tested | 38 (`tests/proxy/`) | One proxy per process. No base-URL mode for clients that ignore proxy settings (`minutehand doctor` names them). Model API calls are recorded only with `--record-model-calls`, as spans. A tunnel is relayed as bytes, never decrypted: a request on it is seen and held until bytes come back that are not a TLS 1.3 server's session tickets (`adapters/proxy/tunnel.py`). |
| Outbound capture: `domain/outbound.py`, `adapters/proxy/capture.py`, `application/outbound.py` | Hosts that are not places the agent keeps state, declared per agent or per standing world: `acknowledge` (answered here, never sent), `pass_through` (sent on, both sides kept), `replay` (answered from a run's recording); `--capture-unknown` for a first run; a send read as a message to a person. See `docs/capture.md` | Built and tested | 41 (`tests/capture/` 28, `tests/checks/test_captured_messages.py` 5, `tests/e2e/test_capture_run.py` 3, `tests/e2e/test_example_capture.py` 1, `tests/serve/test_capture_worlds.py` 3, `tests/model/` 1) | People answer a captured send only when its declaration says how (`replies`); the standing mode's `act` cannot answer through one. Replay matches a body by its hash, so an unlisted timestamp or nonce misses. Discovery mode sends for real. |
| Slack provider | 19 Web API methods, `response_url`, `url_private`; every Events API shape its production caller handles, plus edits, deletes, reactions and joins; buttons, person pickers, modals and slash commands pushed as interactivity payloads and the agent's answers applied; seeded channels, history, threads, files, guests, bots and deactivated accounts; scenario-declared faults including `ratelimited` with `Retry-After` | Built and tested | 115 (`tests/providers/slack/`), most through the real proxy with stock `slack_sdk` | Workspaces are per world (`SlackSeed.workspaces`, see its `README.md`); with none declared, one workspace in which any `xoxb-` or `xoxp-` token acts as the bot. `X-Slack-Request-Timestamp` is real time while `ts` and `event_time` are simulated. See "Slack, against its production caller". |
| Asana provider | 54 routes over users, teams, workspaces, projects and their members, sections, custom fields and their settings, tags, tasks, subtasks and stories, and `/-/oauth_token`; a scenario's Asana seed; a declared status source; people acting on seeded tasks | Built and tested | 102 (`tests/providers/asana/`) | With no seeded token, any bearer token acts as the agent. A bare `custom_fields` in `opt_fields` answers each field's compact record with its value (`enum_value`, `display_value`, ...), as Asana's custom fields guide shows, while every other bare nested field answers its gid and resource type. Webhooks answer 501. No system stories (assigned, moved, completed) are written. |
| YouTrack provider | 45 REST routes, each at `/api` and `/youtrack/api`, and 9 Hub routes at `/hub/api/rest`: issues, custom fields of every single-valued type with per-project bundles, defaults and required flags, comments, tags, links, activities read from the log, projects and their fields, the instance's fields and bundles, users, commands, `issuesGetter/count`, Hub projects, groups, permissions and OAuth tokens; the query language every shape a production client builds; people acting on seeded issues (`ActsOnTickets`) | Built and tested | 119 (`tests/providers/youtrack/`, 221 cases with parameters; most through the proxy) | Only a seeded `*.youtrack.cloud` or `*.myjetbrains.com` host is reached: a self-hosted instance's own host cannot be declared. Multi-valued fields (`enum[*]`, `user[*]`, `version[*]`), text fields, work items, attachments, saved searches, agile boards and sprints as boards are not served. Wording of 403, 429 and several 400s, the activity item `$type`s for tags, links and summary, and the default issue order are not verified against the real service. |
| Google Drive provider | Drive v3 (files incl. multipart, media and resumable uploads, export, copy, permissions, comments, about, drives, changes, channels), Docs v1 `documents.get|create|batchUpdate`, Slides v1 `presentations.get|create|batchUpdate`, Google's `/token` and `/revoke`, OAuth2 v2 `userinfo`, the `iamcredentials` boundary lookup; per-user My Drives and shared drives; a person's change to a document at its moment, pushed to a `changes.watch` address; declared faults | Built and tested | 124 (`tests/providers/google_drive/`; 10 run Google's own clients in a process of their own through the proxy) | A credential is matched by name, never by signature. `httplib2` reaches the proxy only when PySocks is installed beside it. No Sheets API (a Sheet is exported as CSV). The agent is not notified of its own changes. Docs has no headers, footers, footnotes or suggestions, and images are never fetched. Content is capped at 5 MiB per file. |
| Microsoft provider | Identity platform sign-in (client credentials, authorization code, refresh) issuing RS256 JWTs from a published key; the Bot Framework connector (10 routes) and its OpenID metadata; Graph `v1.0` for users, teams, channels, chats and messages, SharePoint and OneDrive files (sites, drives, items by id and path, children, 302 downloads, simple and session uploads, folders, move, copy, delete, invite, links, search, `delta`), subscriptions with the validation handshake; people's messages, edits, deletes, reactions, joins, installs (`PersonAddsAgent`) and card presses pushed with a Bot Framework JWT; people's file changes (`ChangesDocuments`) notified to subscriptions (`NotifiesChanges`); `MicrosoftSeed.faults` and `.holds` | Built and tested | 43 (`tests/providers/microsoft/`), through the real proxy with plain `httpx`, PyJWT and `python-docx` | Token lifetimes are real time. Ids derive from the scenario's name. No comments on files and no record-shaped documents. A production client's adapters were run against it once, outside this repo (see Evidence). See the provider's `README.md`. |
| Jira provider | Jira Cloud REST v3 and Agile 1.0 at `<site>.atlassian.net` and `api.atlassian.com/ex/jira/{cloudId}`: issues, transitions through per-project workflows and screens, comments in ADF, changelog, links, JQL search, projects, users, permissions by project role, boards and sprints; OAuth refresh at `auth.atlassian.com`; people's acts (`ActsOnTickets`); `JiraSeed.rate_limits` | Built and tested | 103 (`tests/providers/jira/`) | No webhooks, no authorization-code grant, no API v2. Link direction read from Atlassian's reference, not verified live. See its `README.md`. |
| Notion provider | Notion API `2022-06-28` at `api.notion.com`: pages, blocks, databases and their queries, search, users, comments, OAuth tokens; seeded workspaces, integrations and what is shared with them; people's changes (`ChangesDocuments`); integration webhooks (`NotifiesChanges`), verified and signed; `NotionSeed.faults` | Built and tested | 88 (`tests/providers/notion/`; most through the official SDK) | Webhook signing, verification and payloads are written from the reference, not verified live; events are not aggregated, delayed or retried. A person cannot move or share a page. See its `README.md`. |
| AWS provider | moto in the process; EventBridge Scheduler bookings become wakes delivered to SQS | Built and tested at the provider | 17 (`tests/providers/aws/`) | AWS's own state lives in moto's memory and cannot be rewound; each run's app takes a fresh AWS account, so a fork starts with none of its parent's queues. moto reads the machine clock for delays, visibility and timestamps. A target other than SQS raises when it fires. No whole run with a `Booked` agent is tested. |
| Run loop, fork, scripted people, agent drivers, files: `application/`, `adapters/agent/` | Plays a scenario on the run's clock, with people's acts on seeded tickets at their moments; forks a finished run from a checkpoint | Built and tested | 74 (`tests/orchestrator/`) | A fork starts only at a restorable checkpoint (the end of a wake at which the agent settled). A fork needs `StateHooks`. `PromptPatch` and `ModelSwap` are tested through a whole run only on the reference agent's scratch measurements, not in the suite. A `PersonChange`'s new delay re-times waits on replies decided before the fork. Only `Scripted` and `Silent` people: `Answers` is refused. |
| Rewinding the agent's own state: `application/restore.py`, `examples/state/` | Settles before every checkpoint, restores as a sequence (`stop`, `restore`, `start`, answer), verifies the report against the checkpoint's; recipes for SQLite and a Firestore emulator | Built and tested | 24 in `tests/orchestrator/` (counted above), 4 in `tests/state/` (1 marked `firestore`) | The verify step compares the report and, when the hooks declare `fingerprint`, a digest of the agent's state; what the fingerprint command does not cover it cannot see. An agent with neither a `Reported` source nor a fingerprint is restored unverified, and says so. Without a `busy` command, settling sees only the report and the proxy, and each checkpoint says it is unconfirmed. PostgreSQL is described, not tested. |
| Checks, ledger, scorecard, patterns: `checks/` | 13 checks (`nagged` added), the obligations ledger, `Effectiveness`, 9 patterns | Built and tested | 60 (`tests/checks/` 53, `tests/test_checks_on_reference_run.py` 7) | `repeated_message` measures its window in wall time. |
| Telemetry out: `adapters/telemetry/otel.py` | Spans, a log record per finding, metrics, over OTLP | Built and tested | 18 (`tests/telemetry/test_otel_telemetry.py`) | World-event spans are emitted when a wake ends, not as calls arrive. |
| Telemetry in: `adapters/telemetry/receiver.py`, `otlp.py`, `forward.py`, `application/model_calls.py` | Receives the agent's own OTLP during a run, keeps its spans with the run, passes it on to where it went before, joins a world event to the model call that led to it | Built and tested | 17 (`tests/telemetry/test_receiver.py`, `tests/test_model_call_join.py`, `tests/e2e/test_agent_telemetry.py`) | OTLP over HTTP and, with `minutehand[grpc]`, gRPC on the same port. Metrics are dropped; a log record is kept only when it carries GenAI content. A span is placed in a wake by comparing its SDK's clock with this machine's. |
| Session and CLI: `session.py`, `cli.py`, `doctor.py` | `minutehand run`, `findings`, `fork`, `runs`, `env`, `doctor` (which client libraries would go around the proxy); `--model-host` names a model API besides the three public ones; starts the agent's own command, or reaches one already running through a proxy on a fixed address | Built and tested | 19 (`tests/e2e/`) | Whole runs are tested with the Slack provider only, and with the agent as a local process: an agent in containers is untested. Samples without `StateHooks` are not independent. |
| Standing mode: `serve.py`, `application/standing.py`, `adapters/control/`, `adapters/proxy/worlds.py`, `adapters/proxy/credentials.py`, `minutehand.testing` | `minutehand serve`: one process holding many worlds at once for a test suite; each call routed to the world that claims its host, a world key its URL names (`Manifest.world_keys`), or a credential (including a JSON or form token request's refresh token, code, client id and secret, and client assertion); a control API under `/v1` that also fires a happening, presses a control, declares a provider's own faults and switches, seeds more into an open world, changes people's accounts and permissions, deletes a ticket as a person, mints the credentials a pushed request carries, resets a world in place and shows its raw state; a pytest client and plugin; a composite action for another repository's CI; the image serves by default | Built and tested | 104 (`tests/serve/` 95, `tests/testing/` 3, `tests/e2e/test_cli.py` 2, `tests/test_scenario.py` 3, `tests/packaging/test_stack.py` 1) | Isolation is as fine as the credentials the services carry: one fixed token per stack means one world at a time. No base-URL mode. A call whose only claim is a `common` or `organizations` sign-in path, or a shared host with no credential, still reaches the default world only. A recorded call whose response body is binary is answered but not kept (see Known issues). Booked wakes are never fired. See `docs/serve.md`. |
| Reference agent: `examples/reference_agent/` | Two processes (an API on `requests`, a worker on `httpx`) over a job queue in SQLite or a Firestore emulator, email by a captured channel with replies, a pass-through search, a model API on a local HTTPS server, OpenTelemetry over HTTP or gRPC, five behaviours; `docs/reference-agent.md` | Built and tested | 9 (`tests/architecture/`) | Firestore variant measured by hand, not in a marked test |
| Session and CLI: `session.py`, `cli.py` | `minutehand run`, `findings`, `fork`, `runs`, `env`; starts the agent's own command, or reaches one already running through a proxy on a fixed address | Built and tested | 19 (`tests/e2e/`) | Whole runs are tested with the Slack provider only, and with the agent as a local process: an agent in containers is untested. Samples without `StateHooks` are not independent. |
| Lints: `lints/` | `wall_clock`, `import_boundaries`, `enum_string_comparisons`, `boundary_dicts` | Built and tested | 27 (`tests/lints/`) | The enum-comparison lint judges a field by its name, not its type. |
| MCP tools, control API and viewer, container image, model-written people, judged checks, generated providers, human actions, a faked system clock, hosted | See their sections | Designed, not built | 0 | |

No fake's wire details have been verified against the real service. The providers are tested against the services' own client libraries (`slack_sdk`, `asana`, `google-api-python-client`, `boto3`) and not against the services.

## Positioning

**Claim:** Build your proactive agent yourself.

**The objection it answers:** "A proactive agent is a product somebody sells me. If I build one, I get a chatbot with a cron job, and I will not know what it is missing until it embarrasses me in front of a colleague."

**The answer:** what separates the two is know-how, and Minutehand hands it over. It runs your agent through the situations a proactive agent has to survive, shows where yours breaks, and names the design that fixes each break.

What is sold is the know-how, in three forms:

| Form | What it is | Where it comes from |
|---|---|---|
| **Scenarios** | The situations: a person goes quiet, a person is away, a date moves, an approval is declined, a weekly task recurs, a deadline closes in | 21 field-observation cases from a production agent |
| **Checks** | What going wrong looks like in each | The nine failure categories captured from that agent's runs, and the incidents behind each guard |
| **Patterns** | The design that stops it, with a working implementation to read | How that agent does it |

A finding carries its pattern (`Finding.pattern`), so the coding agent that reads "followed up 33 hours late" is handed "give every wait an expiry and wake on it" in the same answer.

Monitoring is how the know-how is delivered. It is not the product.

## Problem

Measured on the parent repository's field-observation harness, 2026-10-04:

| What | Evidence |
|---|---|
| A run reports no pass or fail | The run summary's terminal reason is the only verdict. Run `f431fc97f427` ended "turns exhausted" while the agent had researched the wrong company, filed two tickets under that name, and sent a reminder 33 hours late. |
| The fakes run on the real clock | `lints/wall_clock.py` finds 72 machine-clock reads across the 9 emulators (Slack 37, Asana 11, Teams 10, Jira 6, YouTrack 3, Graph 2, Drive 1, GitHub 1, Notion 1). The mission clock reached 1 September; every ticket and message was stamped 24 August. |
| The harness is welded to one agent | The harness's runner imports the agent's action selection, its scheduling clock and its issue repository, reads the agent's next-check fields from its database, and writes the simulated time there directly. |
| People's replies skip the world | Replies are posted to an internal bridge endpoint of the agent's own service. No inbound Slack event is ever produced. |
| Production code carries the test stack | 29 switch sites: 10 base-URL swaps, 12 credential bypasses, 7 parallel fake adapters (the fake Drive service alone is 539 lines). |
| Ten containers, state in memory | Five fakes (Teams, Graph, Drive, Notion, GitHub) hold the world in process memory; four (Slack, Jira, Asana, YouTrack) reload and rewrite whole JSON files on mutation. The tenth container is Firebase's own emulator. |
| It cannot gate a change | Field observation is excluded from CI, is manual, and takes 30–90 minutes a run. |

## Solution

One process with five parts:

1. **Proxy.** The agent's process gets `HTTPS_PROXY`. Calls to hosts a provider claims are answered by that provider's fake. Calls to model APIs are tunnelled without being decrypted, or decrypted and edited when a fork changes the prompt or model, or decrypted and recorded as spans with `--record-model-calls`. Anything else is refused with 502 and recorded. Beside it, an OTLP/HTTP receiver keeps the agent's own telemetry with the run (see "Telemetry").
2. **Providers.** One package per service. Its manifest loads at start; its code loads on its first call. Each answers the real API's routes, reads and writes only through `ports.store.Store`, and records every call as `Change`s, which the store turns into `WorldEvent`s.
3. **Clock.** `RunClock` is the only clock in a run. `next_jump()` moves it to the next moment something is due, every provider stamps from it, and the agent is told the time in `WakeRequest.now`.
4. **People.** A scenario's `Person` replies after a simulated delay, through the provider as a real inbound event. Replies are scripted text or silence today.
5. **Checks.** Classes over `RunView` that return `CheckReport`. Deterministic first; anything that needs judgement answers `FindingKind.REVIEW`.

The agent under test declares how it is reached (`AgentUnderTest`): where it takes its goal (`GoalByWake` or `GoalByMessage`), how it comes back to work (`wakes`), and where pushed events reach it (`inbound`). At most it answers `WakeRequest` with `AgentReport`. An agent that takes its goal as a Slack message and books its own wake-ups answers nothing.

## Examples

### A scenario and an agent file

```yaml
name: partner_pipeline_build
goal: Ayven has three signed integration partnership agreements.
owner: owner
starts_at: 2026-08-24T10:50:03Z
deadline_after: P14D
protected_names: [Ayven]
people:
  - {key: owner,  name: Test User,      email: owner@example.com, reply: {kind: silent}}
  - {key: sofia,  name: Sofia Romano,   email: sofia@example.com,
     reply: {kind: scripted, replies: [{to_ask: 1, text: "Signed and sent back."}]}}
  - {key: dania,  name: Dania Kovac,    email: dania@example.com, reply: {kind: silent}}
ticket_fates:
  - {assignee: sofia, becomes: done, after: P3D}
expect:
  - {kind: person_asked,   person: owner}
  - {kind: ticket_created, assignee: dania, by: P5D}
```

`Scenario.model_validate` rejects an unknown field, a duplicate `Person.key`, and any `owner`, `assignee`, `delegate` or expected `person` that names nobody. `Person.reply` defaults to `Answers`, a model-written reply, and `ScriptedReplier` refuses a run in which anyone has it; every person in a runnable scenario says `scripted` or `silent`. YAML is read without implicit timestamps or base-60 numbers (`application/files.py`), so `opens: 09:00` is a time, not 540.

```yaml
name: forgetful_agent
wakes:
  - {kind: reported, wake_url: "http://127.0.0.1:8765/wake", report_url: "http://127.0.0.1:8765/report"}
inbound:
  - {provider: slack, url: "http://127.0.0.1:8765/slack/events",
     secret: {kind: generated, env: AGENT_SLACK_SIGNING_SECRET}}
```

`secret` says where the secret that signs pushed events comes from: `generated` is made per run and handed to the command Minutehand starts in the variable `env`; `from_env` is the agent's own, for an agent already running, and Minutehand reads the same value from its own variable `env` (a run is refused when it is not set). A `Reported` source may also say how long a wake may take:

```yaml
  - kind: reported
    wake_url: http://platform:8025/minutehand/wake
    report_url: http://platform:8025/minutehand/report
    wake_timeout: PT2M            # one call to wake_url or report_url (default 2 minutes)
    report_first_after: PT0.1S    # the first ask for the report; each wait doubles from here
    report_at_most_every: PT10S   # up to this
    working_limit: PT30M          # a wake still WORKING after this stops the run AGENT_FAILED, saying so
```

A scenario file may leave `starts_at` out: the run then starts at the moment it is started, taken once to the second and recorded in the run's `scenario.json` and `RunRecord.started_at`, so every sample and fork of it plays the same instant. The file is read as a `WrittenScenario`; what a run plays and every reader sees is a `Scenario`, whose `starts_at` is always an instant.

### A whole run

The e2e test agent (`tests/e2e/agents/slack_agent.py`, behaviour `forgetful`: asks once on stock `slack_sdk` and never comes back), a scenario where Sofia and the owner are silent, run with the command that `tests/e2e/test_cli.py` drives:

```
$ minutehand run scenario.yaml --agent agent.yaml --state state -- python slack_agent.py serve --port 8765 --state agent.json
run d799b57b7ad2: partner_pricing
  Failed: 2 checks failed; the run stopped because nothing more was due and the agent asked for no wake.
  stopped at 2026-09-07 10:00 UTC (simulated) because nothing more was due and the agent asked for no wake

fail (2)
  expectations: owner asked mentioning ['confirmed']: wanted at least 1, found 0
    pattern honest_closure: Honest closure. Closing is decided from the state of the world, not from the agent's last message.
  no_follow_up: wait on sofia expired 11 days 6 hours before the run ended and the agent never came back to it
    pattern expiry_on_every_wait: An expiry on every wait. Every wait carries an expected-by date and the agent wakes on it.

informational (1)
  expectations: sofia asked: met by the message to Sofia Romano (seq 17): "Could you confirm the partner pricing, please?"

scorecard
  expectations met: 1 of 2
  waits opened: 1, still open at the end: 1
  follow-ups due: 1, made: 0, late: 1
  time the agent lost: 11.2 days
  wakes: 1, of which changed nothing: 0
  messages to people: 1
  failed checks: 2

checkpoints
  seq 14, after wake 0: not restorable: the agent declares no state hooks
  seq 18, after wake 1: not restorable: the agent declares no state hooks
  seq 19, after wake 1: not restorable: the agent declares no state hooks
exit 1
```

The agent stopped after one wake; the clock ran on to the deadline (seq 19 is that checkpoint), so the wait it abandoned was seen to expire.

### The checks on a captured run

`tests/data/partner_pipeline/` is run `f431fc97f427`, converted to this package's models. `tests/test_checks_on_reference_run.py` asserts:

| Check | Result |
|---|---|
| `near_miss_name` | 4 failures, each `wrote "Aiven" where the scenario says "Ayven"`: two tickets and two messages. 0 once the name is corrected. |
| `repeated_message` | 1 review, evidence `[3, 4]`, "18 seconds apart". The two template-alike pairs it once flagged score below `SAME_ASK`. |
| `expectations` | 1 failure: `ticket created for dania: wanted at least 1, found 0`. The legal-review ticket the goal depended on was never filed. |

### The loop a coding agent runs

Built, on the command line:

```
minutehand run scenario.yaml --agent agent.yaml -- <command>   -> run id, verdict, findings, scorecard, checkpoints; exit 0, 1 or 3 by the verdict
minutehand findings <run_id>                                   -> the same report, read back from the state directory
minutehand fork <run_id> --at <seq> --changes fork.yaml -- <command>
minutehand runs
```

Designed, not built: the same loop as MCP tools (see "What a coding agent calls").

## Architecture

### Components

```
src/minutehand/
  domain/             pure: no I/O, no clock reads
    scenario.py       Model, Scenario, Person, Account, Answers, Scripted, ScriptedReply, ScriptedPress, FormInput,
                      Silent, DelayRange, WorkingHours, Absence, SeededTicket, SeededComment, SeededDocument,
                      DocumentKind, Access, SharedSpace, SignIn, SeededChannel, SeededPost, SeededFile,
                      ProviderSeed, Happening (TicketHappening (Moves, Reassigns, Comments, Deletes) |
                      DocumentHappening (Edited, Renamed, Moved, Shared, Trashed, Commented, FieldSet) |
                      MessagingHappening (PersonPosts, PersonEdits, PersonDeletes, PersonReacts, PersonJoins,
                      PersonAddsAgent, PersonOpensAgent, PersonCommands)), TicketFate, Direction, PersonAsked, TicketCreated, TicketDeleted,
                      TicketInState, Relayed, DocumentCreated, DocumentShared; `{{start+P2D}}` in any text (DATED)
    world.py          WorldEvent, Change, Stored, Exchange, Captured, Body, RecordedCall, EntityRef,
                      TicketSnapshot, MessageSnapshot (with MessageAction), DocumentSnapshot, GrantSnapshot,
                      RecordSnapshot, InteractionSnapshot
    agent.py          WakeRequest, AgentReport, Commitment, AgentUnderTest, Reported, Booked, Polled, Command,
                      GoalByWake, GoalByMessage, HumanAction, Inbox, StateHooks
    outbound.py       Acknowledge, PassThrough, Replay, Answer, Route, MessageReading, InForks, OnMiss
    people.py         PersonReply, Press, PersonMessage, InboundTarget, PermissionGrant, InboundCredentialAsk,
                      InboundCredential
    provider.py       Manifest, Tier, TicketField, DocumentChange, PersonChange, WorldKey, world_keys(),
                      fault_fragment(), merged_seed()
    experiment.py     Fork, CallMatch, PromptPatch, ModelSwap, PersonChange, TicketEdit, DeadlineShift
    checks.py         Finding, CheckReport, Pattern, Obligation, Stability, Effectiveness, PersonBurden,
                      WakeRecord, RunView, Check
    clock.py          Due, Jump, next_jump()
    run.py            RunRecord, StopReason
    telemetry.py      ReceivedSpan, StoredSpan, Attribute and its value kinds, SpanSource, Signal, ForwardFailure
  ports/              Store, Clock, Provider, PushesEvents, PushesInteractions, HoldsTickets, EditsTickets,
                      ActsOnTickets, DeletesTickets, ChangesDocuments, NotifiesChanges, DeclaresFaults,
                      ChangesPeople, GrantsPermissions, MintsInboundCredentials, OwnsSeed, BooksWakes, Wakes,
                      AgentDriver, Reports, Replier, Telemetry
  application/        orchestrator.py (the run loop), checkpoint.py, rewind.py, restore.py (settle, restore,
                      verify), replier_scripted.py, run_clock.py, state_hooks.py, files.py, refusals.py,
                      model_calls.py (the join), standing.py and further_seed.py (`minutehand serve`)
  checks/             one module per check; runner.py, ledger.py, effectiveness.py, patterns.py, _waits.py
  adapters/
    proxy/            server.py, addon.py, policy.py, registry.py, hosts.py, edit.py, redact.py, model_calls.py,
                      capture.py (outbound hosts: answering, keeping, reading a send, replay), worlds.py
    providers/<key>/  manifest.py, provider.py, app.py, wire.py, state.py, seed.py
    store/            sqlite.py
    agent/            reported.py, polled.py, command.py, reach.py
    telemetry/        otel.py (what Minutehand sends), receiver.py, otlp.py, forward.py (what the agent sends)
  session.py          the composition root: play(), fork(), load(), runs(), fork_points()
  cli.py              minutehand run | findings | fork | runs
lints/                discovered by directory, no registration
```

`lints/import_boundaries.py` holds the direction: `domain` imports nothing from `application`, `adapters` or `ports`; `ports` nothing from `application` or `adapters`; `application` nothing from `adapters`; `checks` only `domain` and `ports`; a provider nothing from another provider. `session.py` is the one module that imports both `application` and `adapters`.

### Data models

Every model extends `Model` (`frozen=True, extra="forbid"`). Kinds are `StrEnum` members and unions are discriminated on a `kind` literal, so nothing is decided by matching on a string.

The contract with the agent:

```python
class WakeRequest(Model):
    """ "It is now `now`; go." Sent each time the clock reaches a due moment."""

    run_id: str
    now: AwareDatetime
    reason: WakeReason
    goal: str | None = Field(default=None, description="Set on the START wake only")
    direction: str | None = Field(default=None, description="What the owner said; set on a DIRECTION wake only")


class AgentReport(Model):
    """The answer to "are you still working?" and "when do you next need to wake?"."""

    status: AgentStatus
    next_wake: AwareDatetime | None = None
    commitments: list[Commitment] | None = None


WakeSource = Annotated[Reported | Booked | Polled | Command, Field(discriminator="kind")]

GoalSource = Annotated[GoalByWake | GoalByMessage, Field(discriminator="kind")]


class AgentUnderTest(Model):
    name: str
    goal: GoalSource = GoalByWake()
    wakes: list[WakeSource] = []
    inbound: list[InboundTarget] = []
    human_actions: list[HumanAction] = []
    inbox: Inbox | None = None
    state: StateHooks | None = None
```

`WakeReason` is `START | DUE | PERSON_REPLIED | DIRECTION | TICK`; `AgentStatus` is `WORKING | IDLE | DONE`. `AgentUnderTest` refuses a goal sent by message on a provider it declares no inbound target for, and a goal handed over in a wake when it declares no way to be woken. `wakes` may be empty when the goal comes by message.

What every provider records:

```python
class WorldEvent(Model):
    """One change, or one read, in a shape that is the same for every provider."""

    seq: int = Field(ge=1)
    run_id: str
    wake: int = Field(ge=0, description="The agent wake this happened in; 0 is setup")
    sim_time: AwareDatetime
    wall_time: AwareDatetime
    actor: Actor
    operation: Operation
    entity: EntityRef
    after: Snapshot | None = None
    exchange: Exchange | None = None
```

`Actor` is `AGENT | PERSON | SCENARIO`; `Operation` is `CREATE | UPDATE | DELETE | READ | SEARCH`; `Snapshot` is `TicketSnapshot | MessageSnapshot | DocumentSnapshot | RecordSnapshot`. A provider hands the store a `Change` (the entity, its body as the provider's own JSON text, its parent, its snapshot); `Store.apply` gives it a `seq` and both clocks. The proxy ties the HTTP call (`Exchange`, no headers, credentials redacted by `adapters/proxy/redact.py`) to the events written while answering it.

What a check returns, after the parent repository's lint core:

```python
class Finding(Model):
    check: str
    severity: Severity
    kind: FindingKind
    message: str
    at: AwareDatetime | None = Field(default=None, description="Simulated time")
    wake: int | None = None
    evidence: list[int] = Field(default=[], description="WorldEvent.seq values")
    pattern: str | None = Field(default=None, description="Pattern.key: how a proactive agent avoids this")


class CheckReport(Model):
    findings: list[Finding] = []
    blocked: list[str] = []
    notes: list[str] = []
```

`Severity` (`ERROR | WARNING | INFORMATION`) is how loud; `FindingKind` (`FAIL | REVIEW | INFORMATIONAL`) is what the reader must do. A check that could not read its input puts the reason in `blocked` and did not run.

### The provider port

Eight protocols, because most services push nothing, hold no tickets and book nothing, and a method that returns nothing on their behalf would be a stub:

```python
class Provider(Protocol):
    manifest: Manifest

    def app(self, world: Store, clock: Clock) -> ASGIApp: ...

    def seed(self, scenario: Scenario, world: Store) -> None: ...


@runtime_checkable
class PushesEvents(Protocol):
    async def deliver(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None: ...

    async def say(
        self, message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None: ...

    async def happen(
        self, happening: MessagingHappening, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None: ...


@runtime_checkable
class PushesInteractions(Protocol):
    async def press(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None: ...


@runtime_checkable
class HoldsTickets(Protocol):
    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None: ...


@runtime_checkable
class EditsTickets(Protocol):
    def edit(
        self, ticket: EntityRef, *, state: TicketState | None, assignee_email: str | None, world: Store, clock: Clock
    ) -> None: ...


@runtime_checkable
class ActsOnTickets(Protocol):
    def act(self, happening: TicketHappening, scenario: Scenario, world: Store, clock: Clock) -> None: ...


@runtime_checkable
class ChangesDocuments(Protocol):
    def change(self, happening: DocumentHappening, scenario: Scenario, world: Store, clock: Clock) -> None: ...


@runtime_checkable
class NotifiesChanges(Protocol):
    def watched(self, world: Store, clock: Clock) -> bool: ...

    async def notify(self, world: Store, clock: Clock) -> None: ...


@runtime_checkable
class DeclaresFaults(Protocol):
    def declare(self, faults: str, world: Store, clock: Clock) -> None: ...


class Wakes(Protocol):
    def book(self, due: Due) -> None: ...

    def cancel(self, ref: str) -> None: ...


@runtime_checkable
class BooksWakes(Protocol):
    def bind(self, wakes: Wakes) -> None: ...

    async def fire(self, ref: str, world: Store, clock: Clock) -> None: ...
```

| Provider | `Manifest.key` | Hosts (`path_prefix`) | Ports beyond `Provider` |
|---|---|---|---|
| Slack | `slack` | `slack.com`, `*.slack.com` (`files.slack.com` and `hooks.slack.com` included) | `PushesEvents`, `PushesInteractions`, `DeclaresFaults` |
| Asana | `asana` | `app.asana.com` (`/api/1.0`, and `/-/oauth_token` outside it) | `HoldsTickets`, `EditsTickets`, `ActsOnTickets`, `DeclaresFaults` |
| YouTrack | `youtrack` | `*.youtrack.cloud`, `*.myjetbrains.com` (none; the app answers `/api`, `/youtrack/api` and Hub's `/hub/api/rest`) | `HoldsTickets`, `EditsTickets`, `ActsOnTickets`, `DeclaresFaults` |
| Google Drive | `google_drive` | `www.googleapis.com`, `oauth2.googleapis.com`, `docs.googleapis.com`, `slides.googleapis.com`, `iamcredentials.googleapis.com` | `ChangesDocuments`, `NotifiesChanges`, `DeclaresFaults` |
| Microsoft | `microsoft` | `login.microsoftonline.com`, `login.botframework.com`, `smba.trafficmanager.net`, `graph.microsoft.com`, `*.sharepoint.com` | `PushesEvents`, `PushesInteractions`, `ChangesDocuments`, `NotifiesChanges`, `DeclaresFaults` |
| AWS | `aws` | `*.amazonaws.com` | `BooksWakes` |
| GitHub | `github` | `api.github.com` (REST and `/graphql`; no prefix) | none: repository reads only, see its `README.md` |
| Jira Cloud | `jira` | `*.atlassian.net`, `api.atlassian.com`, `auth.atlassian.com` (none; the app reads the site path and `/ex/jira/{cloudId}` itself) | `HoldsTickets`, `EditsTickets`, `ActsOnTickets`, `DeclaresFaults` |
| Notion | `notion` | `api.notion.com` | `ChangesDocuments`, `NotifiesChanges`, `DeclaresFaults` |

Every provider but GitHub is `Tier.FINISHED`. A person "replying" on a tracker is a `TicketFate`: `HoldsTickets.transition` moves the ticket as actor `PERSON`, and the agent finds it on its next read. `session._services` holds each provider to the ports its manifest claims (`pushes_events`, `books_wakes`) and refuses a mismatch by name, and refuses a seeded ticket that sets a field its provider's `Manifest.ticket_fields` does not hold (`key`, `labels`, `comments`), naming the ticket: YouTrack and Jira hold all three (each one's own seed names a ticket by its `key`); Asana holds `labels` (as tags) and `comments` (as stories by their people), and has no meaning for `key`. `refusals.refuse_unheld` also refuses a document happening whose action is not in its provider's `Manifest.document_changes`, naming it: Drive shows every change but `field_set`; Notion `edited`, `renamed`, `trashed`, `commented` and `field_set`; Microsoft `edited`, `renamed`, `moved`, `shared` and `trashed`. `Manifest.world_keys` says which host label or path segment of a request names its world (a Microsoft tenant, an Atlassian site or cloud id, a YouTrack or SharePoint site), for `minutehand serve` (`docs/serve.md`).

### Things people do by themselves

`Scenario.happenings` is one list of one discriminated union, `Happening`, on `kind`. Each member is something a person does at an offset with no agent involved, and each family lands through the port its provider implements:

| Family | Members (`kind`) | Names its target | Lands through | Wakes the agent |
|---|---|---|---|---|
| Ticket | `TicketHappening` (`ticket`): `action` is `Moves(to)`, `Reassigns(to: person or None)`, `Comments(text)` or `Deletes()` | the seeded ticket's `title`, exactly one | `ActsOnTickets.act` (Asana, YouTrack, Jira) | never: it is found on the next read |
| Document | `DocumentHappening` (`document`): `action` is `Edited(append)`, `Renamed(to)`, `Moved(folder)`, `Shared(access)`, `Trashed()`, `Commented(text)` or `FieldSet(field, value)`, as far as the provider's `Manifest.document_changes` allows | the seeded document's `title`, exactly one (a Notion seed's page or row is one by its `document`) | `ChangesDocuments.change` (Drive, Notion, Microsoft) | only when the agent watches (`NotifiesChanges`: Drive's `changes.watch`, a Graph subscription on the drive, a seeded Notion webhook); that wake carries no `WakeRequest`, and inside it `notify` sends what the service sends |
| Messaging | `MessagingHappening`: `PersonPosts` (`posts`), `PersonEdits` (`edits`), `PersonDeletes` (`deletes`), `PersonReacts` (`reacts`), `PersonJoins` (`joins`), `PersonAddsAgent` (`adds_agent`: Slack invites the bot to a channel; Teams installs the app in a channel or, with none, the person's own chat), `PersonOpensAgent` (`opens_agent`), `PersonCommands` (`commands`) | its own `provider`, channel and post keys | `PushesEvents.happen` (Slack, Microsoft) | yes, `PERSON_REPLIED`, as a pushed reply does |

The run schedules every happening when it seeds, as one `PendingHappening` under `DueKind.HAPPENING`, and fires it from one place (`Orchestrator._happen`), as actor `PERSON` on the run's clock. A run whose happening lands on a provider without its family's port, or a messaging one on a provider with no inbound target, is refused in the `Orchestrator` constructor before anything is seeded, naming the happening and the provider (`happening 1 (tom deletes the seeded ticket 'Lease') lands on testsched, which has no tickets a person can act on`). A ticket or document already deleted when its happening falls due is left alone. A standing world (`minutehand serve`) owes the same happenings and fires each as its clock is advanced past it.

```yaml
tickets:
  - {provider: asana, project: Launch, title: Book the venue, assignee: nadia}
  - {key: notes, provider: youtrack, project: Launch, title: Write the release notes, assignee: nadia,
     labels: [docs], comments: [{by: owen, text: Due before the partner call}]}
documents:
  - {provider: google_drive, title: Launch plan, text: "# Launch plan"}
happenings:
  - {kind: ticket, person: nadia, ticket: Book the venue, after: P1D, action: {kind: moves, to: done}}
  - {kind: ticket, person: nadia, ticket: Write the release notes, after: P2D,
     action: {kind: comments, text: Draft is in the shared folder}}
  - {kind: document, person: nadia, document: Launch plan, after: P3D, action: {kind: renamed, to: Launch plan (final)}}
  - {kind: posts, provider: slack, person: nadia, text: All three are done on my side, after: P4D}
```

That file plays in one run in `tests/providers/test_every_happening_family.py`; `tests/data/every_new_provider/scenario.yaml` plays a Jira ticket, a Notion page, a Microsoft file, a Teams post and a Teams card press in one run in `tests/providers/test_every_new_provider_happening.py`.

### Deliberate failures

A fault is typed by the provider that can produce it and declared in that provider's own seed (`ProviderSeed.body`); there is no shared `Scenario.faults`, because no one shape holds every provider's failures without carrying knobs the others would ignore: Slack's `SlackSeed.faults` (any Slack error code or `ratelimited` with `Retry-After`, every call or N, `only_rich`), Drive's `DriveSeed.faults` (N calls of a Google operation answered one of seven `wire.FaultKind`s), YouTrack's `YouTrackSeed.faults` (a method and path glob answered any HTTP status over a window), Asana's `AsanaSeed.rate_limits` (throttled stretches), Jira's `JiraSeed.rate_limits` (N calls to a path answered 429), Notion's `NotionSeed.faults` (rate limits and edit conflicts, per integration), Microsoft's `MicrosoftSeed.faults` (a Graph or connector error code, or a rate limit) and `MicrosoftSeed.holds` (a seeded file held open by a person over a window, every write refused 423 `resourceLocked`), and GitHub's `GitHubSeed.faults` (rate limits, secondary limits, server errors) and `GitHubSeed.limits` (a repository's truncated-tree and directory-listing thresholds). Each of those providers is `DeclaresFaults`: the standing mode's `POST /v1/worlds/{id}/provider-faults` hands it a fragment of its own seed model that sets only these fields, and it records them as seeding does, counted from the world's now. The control API also keeps its own `faults` route, whose caller writes the status and body.

### A provider's own seed: Asana

Built and tested (`tests/providers/asana/`). What only one service has is seeded through `Scenario.provider_seeds`: a `ProviderSeed(provider, body)` whose body is that provider's own seed model as JSON text (a scenario file writes it as structure), parsed only by that provider. Asana's is `AsanaSeed` (`adapters/providers/asana/seed.py`):

```yaml
tickets:
  - {provider: asana, project: Backend Services, title: API timeout in production, assignee: bob}
provider_seeds:
  - provider: asana
    body:
      workspace: {name: Test Workspace, organization: true, premium: true}
      teams: [{name: Engineering}, {name: Design, members: [alice], agent: false}]
      custom_fields:
        - {name: Status, kind: enum, options: [{name: Open}, {name: In Progress}, {name: Done}, {name: Cancelled}]}
        - {name: Priority, kind: enum, options: [{name: High}, {name: Low}]}
      tags: [production]
      projects:
        - {name: Backend Services, team: Engineering, custom_fields: [Status, Priority],
           sections: [{name: Open}, {name: In Progress}, {name: Done}, {name: Cancelled}]}
      tasks:
        - {ticket: API timeout in production, section: In Progress, tags: [production], due_after: P3D,
           values: [{field: Priority, option: High}, {field: Status, option: In Progress}],
           comments: [{person: alice, text: "Seen again at 9am.", ago: PT2H}]}
      status: {kind: custom_field, field: Status, means: {Open: open, Done: done, Cancelled: cancelled}}
      tokens: [{token: pat-agent}, {token: pat-alice, person: alice, expires_after: PT1H}]
      refresh_tokens: [{refresh_token: refresh-agent}]
      rate_limits: [{after: P2D, lasts: PT90S}]
```

- **Status is one declared fact.** Asana keeps three independent facts about a task: `completed`, its section, and any status custom field. `status` names which one `TicketSnapshot.state` reads: `completed`, `section` (the default; a section says what it `means`, and an unseeded project has To do, Done and Cancelled), or `custom_field` with each option's meaning. The completed box is the floor under all three: a ticked task is never open. Completing a task through the API moves nothing else, as in Asana; a person's fate or happening moves the task by the declared source and ticks it, and a state the source cannot say (cancelled, with `completed`) raises.
- **Who calls is the token's user.** With `tokens` or `refresh_tokens` seeded, only those and the access tokens `/-/oauth_token` mints from a refresh token (an hour of run time each) are accepted; `users/me` and `created_by` are that user. With none, any bearer token is the agent.
- **Refusals on purpose:** 403 on a `private` project, and its tasks, to anyone not in `members`; 402 to search and custom fields when `premium: false`; 429 with `Retry-After` through each `rate_limits` stretch; 404 on a deleted task; 400 on a custom field that does not exist, is not on the task's projects, or an option the field does not have.
- **Every name must name something.** A person, team, field, option, tag, section or ticket the seed names and does not define is refused before anything is written.

The emulator this replaced answered two things differently from Asana as documented, and a client tested against it needs changing: its factory hard-coded the workspace's and the Status, Priority and Story Points fields' gids, where this provider's gids are derived from names and found through the API; and it had no project memberships or `addMembers`.

YouTrack's own seed names a seeded ticket by its `key` (`IssueSeed.ticket`, `LinkSeed.ticket`); a happening names it by title, as on every ticket provider.

```yaml
tickets:
  - {key: notes, provider: youtrack, project: Launch, title: Write the release notes, assignee: tomas,
     labels: [docs], comments: [{by: iris, text: Due before the partner call}]}
happenings:
  - {kind: ticket, ticket: Write the release notes, person: tomas, after: P2D, action: {kind: moves, to: done}}
provider_seeds:
  - provider: youtrack
    body: '{"tokens": [{"token": "perm:…", "login": "agent-bot"}],
            "projects": [{"name": "Launch", "fields": [{"name": "State"}, {"name": "Assignee"}, {"name": "Due Date"}]}],
            "issues": [{"ticket": "notes", "fields": [{"field": "Due Date", "value": "2026-08-26"}]}],
            "grants": [{"login": "tomas", "permission": "jetbrains.youtrack.updateIssue", "project": "Launch", "held": false}],
            "faults": [{"method": "POST", "path": "/issues/*/customFields/*", "status": 429, "after": "PT1H", "lasts": "PT30M"}]}'
```

The YouTrack seed (`adapters/providers/youtrack/seed.py`, `YouTrackSeed`) declares users beyond the people, tokens and Hub services that act as them (once a token is seeded, any other is a 401), instance fields, projects with their own field sets, bundles, resolved flags, defaults, required flags, teams and leaders, any field value on a seeded issue, links between seeded issues, an issue's history before the run, permissions given or taken (403), refusals put in the way of one path for a while (`Retry-After`), and `issuesGetter/count` answering -1. A project made through `POST /admin/projects` carries YouTrack's default template (no Due Date), has no Hub project, and nobody holds Update Project on it, as measured on a live instance. `issuesGetter/count` answers the exact count at once unless the seed says `count_unknown`.

#### Slack, against its production caller

The fake was brought to what a production Slack agent sends and expects, read call by call from its source and driven with stock `slack_sdk` (`WebClient` and `AsyncWebClient`, no base URL) through the real proxy (`tests/providers/slack/test_slack_through_the_proxy.py`, `test_slack_interactions.py`, `test_slack_happenings.py`, `test_slack_whole_run.py`). Every inbound request is checked at the test endpoint by `slack_sdk.signature.SignatureVerifier`.

| Call or shape | Answered | Notes |
|---|---|---|
| `auth.test`, `users.list`, `users.info`, `users.lookupByEmail` | Yes | Users carry `is_restricted` (guest), `deleted` (deactivated), `is_bot`, `tz`, `tz_offset`, profile `email` and `title`. No `tz_label`, no profile images. |
| `conversations.list`, `.info`, `.open`, `.members`, `.history`, `.replies` | Yes | Cursors, `has_more`, `inclusive`, thread summaries. `conversations.open` is idempotent and refuses a deactivated person (`user_disabled`) and a bot (`cannot_dm_bot`). |
| `chat.postMessage`, `.postEphemeral`, `.update`, `.delete`; `reactions.add` | Yes | Blocks get a `block_id` and interactive elements an `action_id` when the agent leaves them out, as Slack does; an app's message carries `bot_profile`. `blocks=[]` on an update clears them. `unfurl_*` and `mrkdwn` are accepted and change nothing. |
| `views.open`, `views.update`, `views.publish` | Yes | A modal needs a `trigger_id` from a press or a command, once (`exchanged_trigger_id`), within three simulated seconds (`expired_trigger_id`); `hash` is checked (`hash_conflict`). `views.push` is `unknown_method`. |
| `oauth.v2.access` | Yes | Client id and secret by HTTP Basic or arguments. Any code is accepted once, since no browser makes one; the installer is the scenario's owner. |
| `response_url` POST (`hooks.slack.com/actions/…`, `/commands/…`) | Yes | New message (ephemeral by default, or `in_channel`), `replace_original`, `delete_original`; five uses within thirty minutes. Error answers (`used_url`, `expired_url`, `invalid_token`, 404) are not verified against Slack. |
| `url_private`, `url_private_download` GET | Yes | Served to any bot or user bearer token; without one, a 302 to an HTML sign-in page, as a browser is redirected. |
| `files.*`, `chat.scheduleMessage`, `search.*`, `team.info`, `reactions.remove` | No | The caller makes none of these. |
| Events: `message` (channel, group, IM, group DM, thread, `file_share` with `files`), `app_mention`, `message_changed`, `message_deleted`, `reaction_added`, `member_joined_channel`, `app_home_opened` (with the published Home view) | Yes | Sent only from conversations the agent's bot is in. A refused event is sent again three times with `X-Slack-Retry-Num` and `X-Slack-Retry-Reason`. |
| `url_verification` | Yes, as `inbound.verify_url` | Nothing in a run sends it. |
| `app_uninstalled`, `tokens_revoked`, `message` subtypes other than `file_share` | No | |
| `block_actions` (buttons, `users_select`) | Yes | Form-encoded `payload`, with `trigger_id`, `response_url`, `actions[]`, `container`, `channel`, `message`, `user`, `team`; the message is left out for an ephemeral one, as Slack does. Other element types (static selects, overflow, date pickers) are not offered to a person. |
| `view_submission` | Yes | `view.state.values` for every input block, `private_metadata`, `callback_id`; the answer's `response_action` (`errors`, `update`, `push`, `clear`, or none) is applied to the view. Only `plain_text_input` can be filled. `view_closed` is never sent. |
| Slash commands | Yes, as `PersonCommands` | Form fields with `response_url` and `trigger_id`; the immediate answer is shown to the person, or the channel with `in_channel`. Sent to the inbound URL, not one per command. |
| Faults | `SlackSeed.faults`, in Slack's `ProviderSeed` | `RateLimited(retry_after)` answers HTTP 429 with `Retry-After`; `Refused(error)` answers any Slack error code; `only_rich` fails only calls with blocks or attachments, so a plain retry passes; `times` and `after` bound it. Each failure is a `faults` record by actor `SCENARIO`. |
| Seeding | `Scenario.channels`, `Person.account` | Public and private channels with topic, purpose, members, the agent in or out, history with threads and files; DMs and group DMs with history; guests (not in `#general`), deactivated accounts and other apps' bots. |

Where the parent repository's emulator answered differently from Slack, the fake follows Slack, and a test written against the emulator would change: it listed ephemeral messages in history; minted DM ids as `D` and the joined member ids; never listed IMs; let any author's message be updated or deleted (Slack: `cant_update_message`, `cant_delete_message`); ran a request with no token as the bot (Slack: `not_authed`); gave every user `tz_offset` 3600; answered missing reaction arguments `invalid_arguments`; served no `response_url` (a 404); and answered `missing_scope` from a per-token scope list, which this fake has not, so `missing_scope` is a declared `Refused` fault.

### Flow of one wake

```
Orchestrator.run():
  every provider seeds the world (actor SCENARIO); happenings, directions and the first Polled tick
  enter `pending`
  checkpoint (wake 0)
  START wake: by message, the owner says the goal (PushesEvents.say); otherwise WakeRequest(START, goal)
  loop:
    jump = next_jump(now, pending)
      None                  -> clock runs on to the deadline, checkpoint, stop NOTHING_PENDING
      jump.now > deadline   -> clock runs on to the deadline, checkpoint, stop DEADLINE_PASSED
    clock.jump(jump.now)
    only ticket fates and ticket or document happenings fired -> HoldsTickets.transition, ActsOnTickets.act,
                     ChangesDocuments.change; no wake, unless the agent watches a changed provider's documents
                     (NotifiesChanges.watched), then a DUE wake in which NotifiesChanges.notify tells it
    otherwise, one wake:
      fire in order: fates (transition), replies (PushesEvents.deliver, or PushesInteractions.press for a
                     reply that uses a control), happenings (ActsOnTickets.act for a ticket,
                     ChangesDocuments.change for a document, PushesEvents.happen for a message), directions by
                     message (say),
                     bookings (BooksWakes.fire), the next Polled tick
      WakeRequest to each driver that must hear of it; AgentDriver.settled() waits until not WORKING
      read the new events: an agent message to a person, as its text reads when the wake ends
                           -> Replier.decide -> a pending reply
                           an agent ticket assigned to a person with a TicketFate -> a pending fate
      AgentReport.next_wake replaces the agent's previous DUE wake
      checkpoint: with StateHooks, settle (not WORKING, and no call through the proxy for `quiet`), then
                  StateHooks.snapshot, or NotRestorable with the reason after `settle_limit`;
                  then a Checkpoint row in the log, carrying the agent's report
      stop on DONE (AGENT_DONE), Scenario.max_wakes (WAKE_LIMIT), AgentFailed (AGENT_FAILED)
  RunRecord -> Scorer (every check) -> Telemetry.found, Telemetry.run_ended
```

- A person answers a message as it reads when the wake ends. A placeholder the agent edits into its question within the wake is never put to anyone; the question is, once. An edit in a later wake that changes the text is put to the person again unless they have already answered that message, and a reply to the old text still on its way is withdrawn. The withdrawn reply stays in the `reply` table and its position is kept in every later `Checkpoint.withdrawn`: the ledger reads it as the replier's decision that the message asked something, and never as an answer, so a wait whose only reply was withdrawn stays open (`test_a_reply_withdrawn_by_an_edit_that_gets_no_answer_settles_no_wait`). Before, the ledger read the latest reply to a message, and an edit that got no answer was settled by the withdrawn one, and then `slow_to_react` failed the agent for never acting on an answer that never arrived.
- A `Reported` wake is asked for its report after `report_first_after`, then at doubling intervals up to `report_at_most_every`; one still WORKING after `working_limit` stops the run as `AGENT_FAILED`, and `RunRecord.failure` says which limit it hit.
- When one jump fires several things, the wake carries the reason that matters most: `PERSON_REPLIED`, then `DIRECTION`, `DUE`, `TICK`.
- A wake made only of bookings sends no `WakeRequest`: the scheduler's delivery is the wake. The loop still polls the agent's main driver (if it has one) until it is not `WORKING`, adopts its report and counts what it wrote in that wake. It cannot see whether the agent's own poll of its queue has picked the delivery up yet: an agent that answers `IDLE` before it has is moved on past it.
- A wake whose only news is a pushed event, to an agent with no wake endpoint, sends nothing either: the push is the wake.
- Every `Polled` tick is a wake and counts toward `Scenario.max_wakes` (default 20).
- An agent that refuses a pushed event fails the wake as one that refuses the wake does.

## Implementation

### Storage: SQLite, one file per root run

| Option | Verdict |
|---|---|
| **SQLite (WAL)** | Chosen. In the standard library, one file, on disk, one writer with any number of readers, and the world can be read "as of" any sequence number with plain SQL. |
| DuckDB | Not for the world: column store, weak at many small transactional writes. Useful read-only for questions across many runs; it attaches SQLite files directly. |
| LMDB / RocksDB | No query language; every listing endpoint (`conversations.history`, issue search) would be hand-written scans. |
| Postgres | A second process. Breaks "one container, one command". |
| JSON files (the parent repository's fakes) | Rewrites the whole file per mutation and holds it all in memory. |

What a run leaves behind (`session.py`):

```
<state>/ca/                          the proxy's CA, made on first use and reused
<state>/runs/<run_id>/world.db       the world, for a run started from the beginning; a fork writes
                                     into its root run's file
<state>/runs/<run_id>/record.json    `RunRecord`
<state>/runs/<run_id>/result.json    `RunResult`: findings, blocked checks, notes, the scorecard
<state>/runs/<run_id>/scenario.json  the scenario as this run played it (a fork's, with its changes)
<state>/runs/<run_id>/agent.json     `AgentUnderTest`
<state>/runs/<run_id>/agent.log      what the agent's own process printed, when Minutehand started it
<state>/runs/<run_id>/captured.jsonl every call the run captured to a host no provider claims, redacted, for
                                     a later replay (`docs/capture.md`)
<state>/runs/<run_id>/world.pool/    the agent's snapshots, each file once, zstd-compressed, named by its
                                     SHA-256, for a root run and all its forks
<state>/runs/<run_id>/wake-<n>/      where the snapshot command writes after wake n; removed once kept
<state>/runs/<run_id>/restoring/     a snapshot written back out for the restore that starts this run;
                                     removed once the restore is over
<state>/runs/<run_id>/restore.json   for a fork, or a sample after the first: each restore step with its
                                     command's output, and whether the restore was verified
```

`world.db` holds twelve tables: `run` (each run and the seq, call count and wake it was forked at), `event`, `entity_version`, `exchange`, `reply`, `span` (the agent's spans, see "Telemetry"), `wake_edge` (the real moment each wake began and ended), `forward_failure`, and four that keep bytes once: `content` (stored bodies), `span_body` (which span attribute values are stored bodies), `snapshot` and `snapshot_file` (the agent's snapshots as manifests into `world.pool/`).

- `entity_version` is the world: append-only, one row per change, read "as of" a sequence number. Providers page through it with `Store.children`; nothing is held in process memory between requests.
- `event` and `exchange` are append-only too. A call that produced no event is recorded with `first_seq > last_seq`.
- `reply` stores every person's reply the first time it is decided. A fork copies the parent's replies up to its checkpoint, so a rerun asks no one again.
- One file per root run isolates parallel runs. A fork lives in its root's file.
- One `sqlite3` connection per store, shared across threads behind one lock: a provider served from a worker thread writes through it.
- `span` is append-only and keyed like `exchange`: each row carries the wake it is placed in, the wake it arrived in and `after_seq`, the head of the log when it arrived. A fork sees its parent's rows placed in wakes up to the one whose checkpoint it was forked at (`run.forked_wake`), however late they arrived.
- `SCHEMA_VERSION = 6` is stamped into `user_version`; a file with tables and another version is refused, not guessed at. Changes: 5 let an exchange carry `captured` and a message `answerable`; 6 moved every body of 512 bytes or more into `content` and the agent's snapshots into manifests. A version 5 file (bodies inline, snapshots as `wake-<n>/` directories) is refused; there is no migration.

#### Bytes kept once

The rule: what goes in is what comes out, and the record is exactly that. Every body is kept verbatim (after redaction, which applies to the stored copy only); nothing is dropped or summarised to save space. Space is saved only by not storing the same bytes twice.

- **Bodies.** Every body passes one door, `SqliteStore._keep`: an entity version's body, an event's snapshot (as its JSON), an exchange's request and response bodies, and each string value of a span attribute. Under `INLINE_LIMIT` (512) bytes of UTF-8 it stays in its row. At 512 or more it goes into `content` under the SHA-256 of its UTF-8 bytes, once per file whoever wrote it, compressed with zstd (level 3) when that is smaller, and the row holds the 32-byte hash. The hash is over the uncompressed bytes. Every reader (`get`, `children`, `versions`, `events`, `calls`, `spans`, and through them the viewer, the MCP tools and the control API) gets back the exact text written: `test_an_awkward_body_reads_back_identical_from_every_reader` writes an empty body, odd JSON spacing, all 256 byte values as the proxy decodes a binary body, bodies one under, at and one over the limit (in one- and two-byte characters), NULs, and 2 MB, through every reader.
- **Where the bytes live.** A body in a table of the same file, not in files beside it: one file to copy, and the body commits in the same transaction as the row that refers to it, so a sweep in another connection never sees it unreferenced in between. The largest body the proxy keeps is bounded (`body_limit`, 1 MiB by default for a captured host; a provider's own limits, 5 MiB for a Drive file), so SQLite's per-value limit is never near. A snapshot file is the opposite case (an agent's database can be gigabytes and must be streamed), so snapshot files live beside the file, in `world.pool/`.
- **The threshold**, measured on run A below: a body under 512 bytes compresses to about its own size (49,577 bytes of 712 distinct bodies under 256 became 52,542), and a stored body costs about 110 bytes of hash, index entry and row header, so moving it saves nothing unless it repeats; between 512 and 1,024 bytes zstd halves a body (564,278 → 303,954 bytes over 1,000 bodies), so even one that never repeats is smaller stored.
- **Compression.** zstd rather than zlib: within 6% of zlib's size on small JSON and three times faster (2.6 ms against 8.0 ms over those 1,000 bodies); `zstandard` was already installed under mitmproxy and is now a direct dependency.
- **Redaction comes first.** The proxy redacts before it hands a body to the store, so a secret never reaches `content` and two bodies that differ only in a redacted value are one row (`test_bodies_that_differ_only_in_a_redacted_secret_are_stored_once_and_no_secret_is_kept`). The stored-bytes searches read every stored body and pooled file decompressed (`tests/support/stored.py`), since a compressed secret would not appear in the raw bytes.
- **Forks** share their parent's bodies: a body is per file, not per run, and a fork lives in its root's file.
- **Discarding** a run deletes its rows and every body no remaining row in any run refers to, in one transaction (a sweep over the five reference columns, not a reference count, so no write path can get a count wrong): a process killed halfway rolls back and every row still reads its body (`test_a_process_killed_while_discarding_deletes_no_body_a_row_still_refers_to`).
- **The write-ahead log** is cut back to 1 MiB after each checkpoint (`journal_size_limit`) and to nothing when a store closes (`wal_checkpoint(TRUNCATE)`, which also runs when a `play` finishes, a fork ends, and a standing world closes).

#### The agent's snapshots

The one place a full copy per step cannot be avoided, since the agent's own state cannot be replayed from the log. The snapshot command fills `wake-<n>/` as before; then `Store.keep_snapshot` records the directory as a manifest (`snapshot_file`: path, regular file or directory, SHA-256, mode, size) and stores each file in `world.pool/<2 hex>/<sha256>.zst`, unless the pool already holds those bytes, and the directory is removed. A restore gets `Store.materialise`: the same paths, bytes and modes written into a fresh `restoring/` directory, each file's hash checked as it is written, removed when the restore is over.

- **Manifest and pool, not hard links.** A hard link shares the inode, so a `restore` command that opened a snapshot file for writing would change every snapshot linked to it, a file's mode would be shared by every snapshot holding the same bytes, and a pooled file could not be compressed. A manifest costs a copy on restore (30 MB in about 130 ms here) and nothing else.
- **Pooled files are always compressed** (streamed, so a file of any size); on the benchmark's random bytes zstd adds under 0.1%, on an agent's database it saves most of it.
- **Only regular files and directories** are kept. A link or a socket fails the checkpoint, naming it: a restore could not put it back as it was.
- **Retention.** `StateHooks.keep: N` prunes after each snapshot: the newest N stay restorable, and so do one pinned (`minutehand pin <run> <seq>`), the run's start (every later sample restores from it) and any a fork was taken from, or a fork of that fork. A pruned checkpoint is shown everywhere as "not restorable: its snapshot was pruned", and a fork from it is refused with that reason. Default: keep all.
- **Crash safety.** Pruning commits the manifests' removal first, then removes the files no manifest names, holding the file's write lock so no new manifest can name a file while it goes. A process killed before the commit changes nothing; one killed after it leaves only unreferenced files, which the next sweep removes (`test_a_process_killed_between_pruning_and_removing_files_leaves_every_kept_snapshot_whole`).

#### Measured: before and after

`uv run pytest -q -m benchmark tests/benchmarks -s` (`tests/benchmarks/test_storage_size.py`) builds three runs through the real proxy and store: **A** 5,000 calls to Slack, 4,500 of them `users.list` answering the same 20,044-byte listing and 500 `chat.postMessage`; **B** 50 uploads to Drive of a 4 MiB text file with 10 distinct contents, each read back 5 times; **C** 200 wakes, each ending in a checkpoint whose snapshot command copies a 30 MB directory of 300 files of random bytes, 3 of which change per wake. Before is `integration-main` at `465507a`; the runs were interleaved, three of each, on a shared machine with a load average of 13 to 17, and the medians are shown. On disk is the world file, its log and the snapshots once the store has closed.

| Run | On disk before | On disk after | Write before | Write after | Read as of mid-run, before | after |
|---|---|---|---|---|---|---|
| A | 112,791,552 | 2,834,432 (40× less) | 13.0 s | 9.5 s | 0.03 ms (a message) | 0.05 ms |
| B | 1,315,889,152 | 29,786,112 (44× less) | 6.4 s | 5.8 s | 3.1 ms (a file's 5.6 MB of bytes) | 4.6 ms |
| C | 6,000,274,432 | 98,735,657 (61× less) | 12.4 s | 20.8 s | 0.05 ms (the checkpoint) | 0.05 ms, and 43 ms to write its 30 MB snapshot back out for a restore |

- Where the bytes went before: A's `exchange` table held 110.6 MB (4,500 copies of one listing), B's 1.26 GB (300 copies of ten files), C's snapshot directories 6.0 GB. After: A's stored bodies are 0.57 MB; B's are 29.5 MB, compressed: each of the ten contents as the call's text (an upload's body and the read-back answer decode to the same text, so share a row) and as the Drive provider's own base64 entity; C's pool is 89.7 MB, the 897 distinct files once each, with 0.01% zstd overhead on random bytes.
- The log before the store closed was 4.1 to 5.9 MB before and 0 after: SQLite's own autocheckpoint kept it from growing past one transaction plus 4 MB here, and it was never "many times the data"; what grew B was the bodies, not the log. The log is now cut back after each checkpoint and to nothing on close.
- C writes 8 s slower over 200 wakes (40 ms a wake): every byte the snapshot command wrote is read once more to hash it (in eight threads, before the write lock is taken), and the directory is removed. A restore now writes the snapshot back out first, 43 ms for 30 MB.
- B's read of a 5.6 MB body takes 1.5 ms more: it is decompressed.
- The threshold, run A at each: 1 byte 3,059,712; 128 2,842,624; 256 and 512 2,834,432; 1,024 3,059,712; 4,096 3,629,056; never 96,448,512 bytes. 512 is chosen over 256 at the same size because it keeps fewer rows in `content`.

#### Housekeeping

`minutehand runs` shows each run's size on disk in three parts that do not overlap: its rows, the stored bodies only it refers to, and the snapshot files only its snapshots name. `minutehand checkpoints <run>` lists each checkpoint, whether it is restorable and its snapshot's size and the bytes only it holds. `minutehand gc` sweeps every world file under the state directory of stored bodies and pooled files nothing refers to and prints what it freed. A standing server's retention of closed worlds is the same function (`session.collect`): it removes the directories of worlds beyond `keep`, then sweeps the rest.

### One container

Partly built: the image runs `minutehand serve` by default, and `minutehand env --format compose --serve-as minutehand` writes a stack's override ("The standing mode", `docs/serve.md`, `tests/packaging/test_stack.py`). For `run`, Minutehand runs as one Python process. `Proxy` listens on `127.0.0.1` on a port the system picks unless `--proxy-host` and `--proxy-port` say otherwise; `minutehand run … -- <command>` starts the agent's own process beside it, or, with no command, wakes an agent that is already running. An agent in containers is given the proxy as `--agent-proxy-host` names this machine (`host.docker.internal`), and `minutehand env --format compose --service <name>…` prints a Compose override that sets the variables below in each named service and mounts the CA bundle read-only.

The design: one image, one process, two ports.

| Port | Serves |
|---|---|
| 8080 | Proxy. `CONNECT` with TLS termination under a CA generated on first start. |
| 8081 | Control API, viewer, MCP at `/mcp`, the CA at `/ca.pem`, and every provider again at `/p/<provider>/…` for clients that cannot use a proxy. |

```yaml
services:
  minutehand:
    image: ghcr.io/alknoma/minutehand
    volumes: ["minutehand-state:/var/lib/minutehand", "minutehand-ca:/ca"]
  agent:
    environment:
      HTTPS_PROXY: http://minutehand:8080
      NO_PROXY: localhost,firestore
      SSL_CERT_FILE: /ca/minutehand-ca-bundle.pem          # httpx, requests, slack_sdk
      REQUESTS_CA_BUNDLE: /ca/minutehand-ca-bundle.pem
      HTTPLIB2_CA_CERTS: /ca/minutehand-ca-bundle.pem      # googleapiclient
      NODE_EXTRA_CA_CERTS: /ca/minutehand-ca-bundle.pem
    volumes: ["minutehand-ca:/ca:ro"]
```

Three limits of the design:
- **The agent's own database is not a SaaS fake.** Firebase's emulator suite is Google's and stays a separate container.
- **The agent's container must trust the CA.** One environment variable per HTTP library, as above, each naming the bundle: certifi's public roots and then the proxy's CA. A file holding the proxy's CA alone replaces a library's roots, and every call the proxy tunnels to a real host, a model API, fails verification.
- **A client that ignores proxy settings** would use the `/p/<provider>/` base URL instead, which is a configuration change in the agent. That base-URL mode is not built.

### The standing mode

Built and tested; the whole of it is in `docs/serve.md`. `minutehand run` owns the loop, the clock and the people;
`minutehand serve` owns none of them and holds a world per test for as long as the test needs it, so a suite that
used to seed an emulator, call its own services and inspect the emulator can do the same against Minutehand.

- **Which world.** `adapters/proxy/addon.py` asks a `Worlds` (`adapters/proxy/worlds.py`) for each call's world.
  `minutehand run` gives it `One`; `serve.Standing` routes by the host a world claims, then by the first
  credential the call carries in a place the OAuth standards define (`adapters/proxy/credentials.py`), then to
  the one default world, else refuses with 502 into the lobby. An OAuth token answer's tokens are claimed by the
  world whose call minted them. Each world has its own lock, so calls in different worlds do not wait on each
  other.
- **A world** is `application/standing.py`'s `StandingWorld` over a `Seed` (`domain/scenario.py`): its own
  store and `RunClock`, providers seeded on first use, and, with scripted people on, the replies, fates and
  directions it owes, fired only when its clock is moved past them.
- **The record.** A world is a run (`StopReason.CLOSED` when closed), so `findings`, `view` and the MCP tools
  read it; the newest `--keep` closed worlds are kept.
- **Outbound hosts.** `CreateWorld.outbound` declares, per world, the hosts no provider claims that it captures
  (`docs/capture.md`); a call reaches them once it is the world's by its claims, so two worlds can declare one
  host differently. `GET /v1/worlds/{id}/calls?captured=true` lists them.
- **A world changed while open.** Everything an emulator's admin routes did is a typed request here, validated by
  the provider that owns the thing and recorded in the world as actor `SCENARIO` at its clock: a further seed
  (`POST /seed`, `application/further_seed.py`: each held provider's seeding is run in a scratch store over the
  scenario before and after the addition, from the position in the log it was seeded at, and what the second
  wrote that the first did not is landed; refused with nothing written when it would renumber, rewrite something
  changed since it was seeded, or take an id in use), a person's account changed (`ChangesPeople`, each provider's
  `Manifest.people_changes`), a named permission (`GrantsPermissions`), a provider's switches as typed faults
  (`DeclaresFaults`), a ticket deleted by a person (`DeletesTickets`), the headers a pushed request is signed with
  (`MintsInboundCredentials`), a reset in place to the seed, and a provider's raw state for a person to read.
  `GET /v1/providers` says which of these each provider has; a provider asked for one it has not answers 409 with
  `kind: unsupported`.

When to use which: `run` measures an agent over simulated days and gives a verdict; `serve` stands in for a set
of emulators under an ordinary service-level suite, where the test drives and asserts.

### Lazy loading

Built and tested (`test_provider_module_is_imported_on_its_first_request`).

- A provider is a package with `manifest.py` (`MANIFEST: Manifest`, data only) and `provider.py` (`build() -> Provider`). `Registry.installed()` imports every manifest; nothing else.
- Built-in providers are found by walking `minutehand.adapters.providers`. An installed package names itself under the entry-point group `minutehand.providers`, with its package as the value; an entry point whose name differs from its manifest's key is refused.
- The proxy maps a request's host to a manifest and builds that provider on the first call (`Registry.provider`). `session.play` builds the providers the scenario and the agent name before the run, because it seeds them and holds them to their ports; any other installed provider is built on its first call and seeded then with the scenario, unless the world already holds anything of it (a fork of a run that seeded it); the seed's events are not tied to that call. `RunRecord.providers` lists every provider the agent called.
- Host patterns are an exact lower-case host or `*.` and a domain; a wildcard does not claim its own apex. Two providers claiming overlapping hosts, or one key, are refused when registered.
- Hosts a scenario declares (a self-hosted YouTrack) are designed, not built.

### Thousands of services

Designed, not built. Five hand-written providers exist, all `Tier.FINISHED`; the parent repository's nine fakes took about 19,400 lines. That does not reach thousands, so a provider has a `Tier`, and only the top one is written by hand.

| `Tier` | What it is | Made from | Effort per service |
|---|---|---|---|
| `OBSERVED` | Calls pass through to the real service and are recorded | Nothing | None |
| `GENERATED` | Stateful create, read, update, delete, list and paginate over the store; requests and responses validated against the service's own description | An OpenAPI document, or the tool list of a remote MCP server | None; one generic engine serves them all |
| `TAUGHT` | Generated, then corrected: recorded real traffic is replayed against it and a coding agent fixes each difference | `OBSERVED` recordings plus the conformance run | Minutes of agent time, reviewed |
| `FINISHED` | Refusals, pushed events, sign-in, known quirks | Hand work | Weeks; reserved for what every agent touches (Slack, Teams, the big trackers) |

- **One engine, not thousands of providers.** `GENERATED` is a single provider whose manifests are produced from descriptions. The APIs.guru directory holds roughly 1,900–2,500 public descriptions.
- **MCP is the shorter road.** A remote MCP server lists its tools with schemas. The same engine can stand in for any of them.
- **An unmapped resource is recorded as `RecordSnapshot(resource, text)`.** The AWS provider and the Drive provider's permissions and comments emit it today. Two checks read it: `near_miss_name`, which needs only the text the agent wrote, and `acted_after_deadline`, which needs only that a write happened. `duplicate_ticket` and `repeated_message` do not: a record has no title, project or channel to compare.
- **Mapping is data.** Which resource is a ticket and which field is its title is a short mapping file per service, drafted by a coding agent and reviewed.
- **Prior art:** FetchSandbox generates a stateful sandbox from an OpenAPI document and is hosted; Prism and Microcks serve examples without state. Nango's provider catalogue (1,000+ APIs) is under the Elastic License and cannot be copied into this repo.
- **Unproven:** how much of a real service's behaviour create-read-update-delete over its description actually covers. This needs measuring on five services before the tier is promised.

### Hosts the proxy does not own

Built and tested (`tests/proxy/`). `Routing.policy(host)` decides by host alone:

| `HostPolicy` | When | What happens |
|---|---|---|
| `ANSWER` | A provider claims the host | Its app answers with the manifest's `path_prefix` stripped; the call is recorded. A provider that fails to load answers 500; the call never reaches the real host. |
| `TUNNEL` | A model host (`DEFAULT_MODEL_HOSTS`: `api.openai.com`, `api.anthropic.com`, `generativelanguage.googleapis.com`) with no edit for this run | Bytes pass through, never decrypted, never recorded |
| `EDIT` | A model host the run edits | Decrypted, edited, sent on with the upstream certificate verified; recorded as a span only with `--record-model-calls`. An edit that fails answers 502 rather than sending the request unedited. |
| `RECORD` | A model host, with no edit, in a run started with `--record-model-calls` | Decrypted, sent on unchanged with the upstream certificate verified, and kept as a span (`adapters/proxy/model_calls.py`) whose `minutehand.request.body` and `minutehand.response.body` hold both bodies byte for byte: UTF-8 text as a string, credentials redacted, anything else as its bytes; a streamed answer is passed to the agent chunk by chunk as it arrives |
| `REFUSE` | Anything else | Captured when the call's world declares the host outbound (below); else passed through and kept as `discovered` under `--capture-unknown`; else 502 and recorded with no provider, surfacing as an `unmatched_call` finding that says how to declare it |

A host left to `REFUSE` is decided per world, by the declarations of the world the call belongs to (`Mounted.capturing`; `docs/capture.md`):

| Declared | What happens |
|---|---|
| `acknowledge` | Never leaves the machine: answered with the declared status, headers and body (or a route's); kept as an `Exchange` carrying `Captured`; with a `message` reading, also a world event, a message from the agent to the person the body names |
| `pass_through` | Sent to the real host unchanged, upstream certificate verified; the answer reaches the agent chunk by chunk through the tee `RECORD` uses, and both sides are kept |
| `replay` | Answered from an earlier run's `captured.jsonl`, marked `x-minutehand-replayed`; a miss is passed through and kept, or refused with 502, as declared |

A declared host a provider claims, or a model host, is refused when the run or world is created, naming both.

A model host that overlaps a provider's claim is refused when `Routing` is built. The model-host list is a `Routing` argument; the CLI uses the default. Upstream connections open only when a request is forwarded (`connection_strategy="lazy"`) and the certificate shown to the client is minted, not copied (`upstream_cert=False`); `test_claimed_host_is_answered_without_contacting_it` and `test_unclaimed_host_is_refused_and_recorded_without_contacting_it` watch a listener receive no connection.

Capture of hosts no provider claims is built (above). Passing a host a provider claims through to the real service, to measure the provider against it, is not.

### What "right" means for a scenario

Built and tested. A scenario states what must be true of the world, in macro terms: a person was asked, a ticket was created, deleted, or reached a state. Each is a typed selector with a count (`Bound.at_least`, default 1; `at_most`) and an optional time bound (`by`, an offset from `starts_at`).

```yaml
expect:
  - {kind: person_asked,    person: owner}
  - {kind: ticket_created,  assignee: sofia}
  - {kind: ticket_created,  assignee: dania, by: P5D}
  - {kind: ticket_created,  mentions: [Aiven], at_least: 0, at_most: 0}
  - {kind: ticket_deleted}                      # default: at most 0
  - {kind: ticket_in_state, assignee: sofia, state: done}
```

`mentions` is a case-insensitive substring match on the message text or the ticket's title and body. "Was Sofia asked about pricing" in the sense of meaning, not words, is the judged check `asked_about`.

A substring match can pass hollow, and both were seen on a real run: the owner was "told" the answer by a message that restated the question and carried none of the answer, and an agent told to send exactly "hello" passed by sending a paragraph that contained the word. No match is made smarter to catch it. Every met expectation is reported as an `INFORMATIONAL` finding that quotes what met it, trimmed to 160 characters, with who it went to and its seq, up to three matches and a count of the rest:

```
informational (1)
  expectations: owner asked mentioning ['confirmed']: met by the message to Owner (seq 1): "Asked Sofia: has the venue been confirmed?"
```

A met expectation carries no pattern: there is nothing to fix. Only `FAIL` findings count against `expectations_met`.

"The owner was told what Sofia answered" is `Relayed`, defined from the log alone, with no judgement and no word list:

```yaml
  - {kind: relayed, said_by: rosa, to: owen, tell: lakeside hall}
```

- **The tell is the author's.** A phrase only `said_by`'s answer holds. The scenario is refused when its goal, a direction, a seeded ticket or document, or anyone else's scripted reply or facts holds it, when `said_by` is `Silent`, and when none of their scripted replies (or, for a model-written person, none of their facts) holds it.
- **The person must say it first.** The first event in the world whose text holds the tell must be a message from a person that is `said_by`'s own reply, not withdrawn, landing at that moment (`RunView.replies`). An agent message that held it earlier means the agent did not hear it from them, and the finding says so: "the agent wrote it (seq 1) before rosa said it, so nothing relayed it".
- **A match** is an agent message to `to`, after that reply, holding the tell, in any case.

What it gets wrong: the tell is a substring, so a relay that paraphrases ("the hall by the lake") is not counted, and one that quotes the tell inside a sentence that contradicts it is. A model-written person may phrase the fact without the tell, and the expectation then fails though the content was passed on.

### A person acting in the agent's own product

Designed, not built: `HumanAction` and `Inbox` are models in `domain/agent.py` and nothing reads them.

Some of what a person does never touches a SaaS: approving an operation in the agent's own web app, answering a question on its own page. No fake can stand in for those: they are the agent's own endpoints.

```python
class HumanAction(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(description="When a person would do this; the persona reads it")
    method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str = Field(description="May hold {argument} placeholders")
    body: str | None = Field(default=None, description="JSON text with {argument} placeholders")
    arguments: list[ActionArgument] = []


class Inbox(Model):
    url: str = Field(description="Lists what is pending; may hold {person_email}")
    id_field: str
    summary_field: str
```

- **Two ways to supply them.** Listed in the agent file, which changes nothing in the codebase. Or marked in the agent's own API description: any operation carrying `x-minutehand: human_action` is learned when the run starts. The mark is data on the route, not an import.
- **Each one becomes a tool the simulated person holds,** beside replying in chat and pressing a button in a message. A model-written person chooses among them; a scripted person names one.
- **Every call is recorded** as a `WorldEvent` with `actor=PERSON`, so "the owner approved on day three" sits on the same timeline as the tickets.

### Pillar one: proactive effectiveness

A run ends with a scorecard. It is computed from the world and the clock; nothing in it is the agent's own account of itself.

```python
class Effectiveness(Model):
    expectations_met: int = Field(ge=0)
    expectations_total: int = Field(ge=0)
    waits_opened: int = Field(
        ge=0,
        description="Asks and hand-offs the agent is owed an answer or work on; the scenario's deadline is not one",
    )
    waits_open_at_end: int = Field(ge=0, description="Of those, the ones the world had not settled when the run ended")
    follow_ups_due: int = Field(
        ge=0,
        description="Moments a wait fell due while still open: its expected date, and again its patience after "
        "each follow-up",
    )
    follow_ups_made: int = Field(
        ge=0, description="Agent writes the person could see on a wait still open, whether before or after it fell due"
    )
    follow_ups_late: int = Field(
        ge=0, description="Of the moments due, those followed up more than the grace after, or never"
    )
    time_lost: timedelta = Field(description="Late follow-ups plus slow reactions to answers")
    slowest_follow_up: timedelta | None = None
    reactions_due: int = Field(
        default=0, ge=0, description="Settled waits naming a person or entity, so a reaction can be timed"
    )
    reactions_slow: int = Field(
        default=0, ge=0, description="Of those, the agent's next touch came after the grace, or never"
    )
    slowest_reaction: timedelta | None = None
    messages_to_people: int = Field(default=0, ge=0)
    burden: list[PersonBurden] = Field(default=[], description="Messages per person, in Scenario.people order")
    messages_per_outcome: float | None = Field(
        default=None, description="messages_to_people per expectation met; None when none was met"
    )
    wakes: int = Field(ge=0)
    idle_wakes: int = Field(ge=0, description="Wakes that changed nothing")
    failed_checks: int = Field(ge=0)
```

The rule that makes it fair: time the world itself took is not the agent's. A person who needed three days, or a reviewer who never answered, costs the agent nothing. `time_lost` counts only the stretch between the moment the agent should have acted and the moment it did, beyond `GRACE` (one hour, `checks/_waits.py`): a follow-up after a wait fell due, and a reaction after a wait settled. A wait left after it fell due costs its whole stretch, from that moment to its settling or the run's end.

What a follow-up is (`checks/_waits.chase`):

- **A follow-up is an agent write the person could see while the wait is open**: a message to them or their delegate, or a change to the ask's thread or ticket. Before the wait fell due or after, it counts in `follow_ups_made`. A read is not one: looking at the channel tells nobody anything.
- **A wait falls due at its `expected_by`, and again its `patience` after each follow-up.** For an answer the patience is the person's longest delay: a reminder gives them their usual time again. For work there is none: the ticket's fate has its own pace, and the first follow-up after the date answers it.
- **A wait is followed up for a due moment when a follow-up comes at or after it.** One that came more than `GRACE` after is late (`late_follow_up`). A wait still open whose last due moment passed with nothing after it was abandoned (`no_follow_up`), and the finding says what came before: "the agent followed up once, the last 2 days after the ask, then nothing; due again 4 days 18 hours after the ask, it sat 9 days 6 hours until the run ended".

One reading serves every surface: `checks/_waits.chases` reads each wait of the ledger for these moments, the scorecard counts them, and the viewer's `/obligations` answers them per wait (`fell_due`), so the page draws a wait overdue only where the scorecard counts it due. The page once drew "overdue" from a wait's first `expected_by` by itself, and a silent owner the agent kept reporting to showed overdue while the scorecard said no wait fell due (`tests/web/test_viewer_waits.py`).

What this gets wrong: the patience is the person's longest delay whatever the follow-up said, so a reminder that only adds a detail gives the same allowance as one that re-asks; and a follow-up sent a minute before a due moment moves it on a whole delay. That one now costs something: each follow-up sent before its wait fell due is counted (`follow_ups_early`), and more of them on one ask than the person's `Person.early_follow_ups` is the `nagged` finding. The reference agent's nagging behaviour (every 12 hours, against a 66-hour delay) fails it with 10 early follow-ups where the diligent one makes 2.

The reference run, scored by `tests/test_checks_on_reference_run.py` from the run's own turn files (`timeline.json`):

```
expectations_met       3 of 4      the legal-review ticket was never filed
waits_opened           12          5 still open when the run stopped
follow_ups_due         3           made 3, late 1
time_lost              1 day, 8:43 one reminder, 33 hours after the wait expired
wakes                  20          3 changed nothing
```

These waits are read from the captured agent's own records, which predate the ledger and name no person or entity, so no reaction is timed on this run.

#### The verdict

Built and tested (`checks/runner.verdict`, `tests/checks/test_verdict.py`, `tests/e2e/test_unfinished_verdict.py`). Whether the checks held and whether the agent finished are two questions, and a run answers both. `RunResult.verdict` is a `Verdict` (`domain/run.py`): its kind, how the run stopped, how many waits and commitments were still open, and one sentence that the command, the viewer, `list_findings`, `run_scenario` and `list_runs` all print as it is.

| `VerdictKind` | When | Exit |
|---|---|---|
| `FAILED` | Any finding is `FindingKind.FAIL` | 1 |
| `PASSED` | No check failed, and the agent reported `DONE`, or nothing was left open: no wait the world had not settled and no commitment its last report held `OPEN` | 0 |
| `UNFINISHED` | No check failed, the run stopped any other way (`WAKE_LIMIT`, `DEADLINE_PASSED`, `NOTHING_PENDING`, `AGENT_FAILED`, or a captured run that does not say), and a wait or a commitment was still open | 3 |

```
run 5c1e0a9f2b77: partner_pricing
  Not finished: no check failed, but the agent never reported it was done; the run stopped at the scenario's wake limit, with 1 wait still open.
```

Exit 3 is not a failure: a scenario whose point is that nobody answers ends at its deadline with the agent's question open, and is `UNFINISHED` rather than `FAILED`. A CI job that wants such a scenario green accepts 3 for it; one that wants every agent to close its work accepts only 0. 2 stays "could not be performed", which has no verdict.

What the rule gets wrong:

- **It trusts `DONE` as far as the ledger lets it.** An agent that reports done while a question it asked is unanswered and was never followed up is `UNFINISHED`, naming whom; one that reports done with every question answered or chased passes unless an expectation or a check says otherwise. Whether a `DONE` whose expectations are met is true to the goal's meaning, and whether a message was a question at all, are left to judged checks.
- **A wait on a `Silent` person is never settled,** since every message to them is an unanswered question. The one exception: once every expectation is met, a message to a `Silent` owner sent with or after the last of them is the result being reported, and keeps nothing open. A silent person other than the owner still leaves the run `UNFINISHED`.
- **Nothing open is read as finished.** An agent stopped at a limit that reports no commitments and has no wait open passes, though its goal may be untouched; only the expectations can say the goal was not met.
- **A commitment counts only as the agent reported it.** An agent that reports none is judged on waits alone.

| Layer | Answers | State |
|---|---|---|
| Scorecard (`Effectiveness`, `checks/effectiveness.py`) | How well, in numbers that compare across runs, prompts and models | Built and tested; ends every run |
| Expectations (`checks/expectations.py`) | Did the world end up right | Built and tested |
| Checks | Which known failure, where, with evidence | 12 built and tested (below) |
| Patterns (`checks/patterns.py`) | What design fixes it | 9, each with a page in `docs/patterns/` |
| Verdict (`Verdict`) | Did the checks hold, and did the agent finish | Built and tested; ends every run and sets the exit code |
| Stability (`Stability`) | How often, over several samples | Built: `--samples N` reports "passed k of N"; a sample that did not finish did not pass |

The checks, discovered by `checks/runner.py` (any class in a module of `checks/` with `id`, `needs` and `run`; no registration):

| `id` | Kind of finding | `Pattern.key` |
|---|---|---|
| `acted_after_deadline` | `FAIL`; `REVIEW` for a wake whose late writes are all messages | `budgeted_follow_up` |
| `nagged` | `FAIL`: more follow-ups on one ask, each before the answer was due, than the person's `early_follow_ups` (default 2) | `budgeted_follow_up` |
| `chased_absent_person` | `FAIL`: messaged someone away while a delegate covered | `absence_aware` |
| `duplicate_ticket` | `FAIL`: the same normalised title filed twice in one project while the first was open | `one_open_ask_per_person` |
| `expectations` | `FAIL` per unmet expectation | `honest_closure` |
| `idle_wake` | `REVIEW`: a wake that changed nothing in the world and nothing the agent was waiting on, with the model calls received during it, or that none could be counted; not raised when the agent's telemetry shows it made none | `check_world_before_model` |
| `kept_chasing_after_done` | `REVIEW`: a message threaded under an answered ask, or naming a finished ticket | `one_open_ask_per_person` |
| `late_follow_up` | `FAIL`: a follow-up more than `GRACE` after the wait fell due | `expiry_on_every_wait` |
| `near_miss_name` | `FAIL`: a protected name written one letter off | `confirm_names` |
| `no_follow_up` | `FAIL`: a wait still open fell due and nothing followed; says how many follow-ups came before | `expiry_on_every_wait` |
| `repeated_message` | `REVIEW`: two messages to one channel within five minutes of simulated time, no reply between, sharing rare wording | `one_open_ask_per_person` |
| `slow_to_react` | `FAIL`: an answer landed or work was finished and the agent came back late or never | `expiry_on_every_wait` |
| `unmatched_call` | `REVIEW`: a call to a host no provider claims | none |

`idle_wake` states what it saw and gives no reason: "wake 3 changed nothing in the world and nothing the agent was waiting on; 2 model calls were received or recorded during it", or, with no span of a model call in the run, that the count is not known. Why a wake was idle (asked the model to look, woke too early, dropped a reply) is not observed, so the advice for each cause is on the pattern's page, not in the finding. A wake that changed nothing and made no model call, by telemetry that reports model calls, cost nothing worth fixing and is only noted. What this gets wrong: an agent that traces some of its model calls and not others has an untraced, costly wake read as free; and "changed nothing" counts only writes to the world and changes to reported commitments, so a wake that learned something it kept in its own memory reads as idle.

Not built:

- **The earliest the work could have finished,** given how the people and systems behaved. With it, `time_lost` becomes "finished four days later than was possible". It needs to know which waits depend on which.
- **A cross-check against `AgentReport.commitments`.** The commitments are kept in each checkpoint and only change `WakeRecord.commitments_changed`.

### Pillar two: the changelog, rewind and forks

The world is a log, not a state. Every change any provider makes is one row with a sequence number and the simulated time, and a fake's "current state" is a question asked of that log.

```sql
CREATE TABLE IF NOT EXISTS entity_version(
  run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  provider TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL,
  parent TEXT, body TEXT, sim_time TEXT NOT NULL,
  PRIMARY KEY (run_id, seq));
```

- **The world as of any moment is a query:** the latest version of each entity at or below a sequence number. Rewinding does not restore anything; it moves the point the fakes read from.
- **A fork is a child run that shares its parent's log up to a sequence number** and writes its own rows after it (`SqliteStore.fork`). No copy is made. It also sees the calls its parent had recorded by then, and none after.
- **Providers do not know.** They read and write through the store, which applies "as of" for them.
- **A fork starts only at a checkpoint.** At the end of every wake, and at setup and at the deadline the clock runs on to, the run loop appends a `Checkpoint` (the clock, the decided reply count, scheduled fates, commitments, everything pending, and whether the agent's own state there can be put back) as an entity in the same log (`application/checkpoint.py`). `fork_run` refuses any other `at_seq`; `minutehand run` and `findings` list the ones that exist and say which are restorable.

What a rewind needs beyond the world:

| Part | How it comes back | State |
|---|---|---|
| The clock and everything pending | The `Checkpoint` row at the fork's seq | Built |
| People's replies already given | The `reply` table; copied up to the fork, decided fresh after it | Built |
| The agent's own state | Locally: `StateHooks`, a procedure Minutehand owns (below) | Built and tested: SQLite in the default suite, a Firestore emulator against a real container (`-m firestore`) |
| | Hosted: a snapshot of the whole virtual machine the agent runs in, which needs no hooks | Designed, not built |
| AWS's own queues and schedules | Not at all: moto keeps them in process memory, outside the log, and the fork's app takes a fresh account. The manifest says so (`Manifest.state_outside_log`), and a fork at any checkpoint after the parent first called AWS, or wrote an AWS record, is refused, naming the provider, its first call and what it keeps (`application/rewind.py`; `tests/e2e/test_booked_on_aws.py`). A fork before the first call runs. | Refused, never silently wrong |

#### The agent's own state: settle, restore, verify

The agent supplies commands; Minutehand decides when they run and checks what they did (`application/restore.py`).

```python
class StateHooks(Model):
    snapshot: list[str] = Field(min_length=1)
    restore: list[str] = Field(min_length=1)
    stop: list[str] | None = Field(default=None, min_length=1, description="Stops the agent's processes")
    start: list[str] | None = Field(default=None, min_length=1, description="Starts them again after `restore`")
    quiet: timedelta = Field(default=timedelta(seconds=1), ge=timedelta(0), ...)
    settle_limit: timedelta = Field(default=timedelta(seconds=60), gt=timedelta(0), ...)
    answer_limit: timedelta = Field(default=timedelta(seconds=120), gt=timedelta(0), ...)
    step_limit: timedelta = Field(default=timedelta(minutes=5), gt=timedelta(0), ...)
    keep: int | None = Field(default=None, ge=1, ...)
```

- **Settle.** A checkpoint is snapshotted only when the agent reports it is not `WORKING` and no outbound call of its has been seen for `quiet`, measured from the later of the moment settling began and its last call. The proxy remembers the latest call it saw (`Proxy.last_call`, `Traffic`): every request it answers, edits or refuses, every `CONNECT`, every new tunnelled connection, and every message of bytes on a tunnel. Nothing it sent may still await an answer (`Traffic.waiting`): a call sent on to a real host until its answer has passed, and a tunnel, never decrypted, from a request on it until the server's bytes answer it. A tunnel's bytes are read only as far as TLS record headers, which are in the clear (`adapters/proxy/tunnel.py`): on a TLS 1.3 connection (the server sends an encrypted record before the client has) the client's first encrypted record is its `Finished`, and the first encrypted message the server sends after it is its session tickets, sent unasked; they arrive after a request sent straight after the `Finished` and are not its answer. What record headers cannot tell: a server that sends no tickets and answers the first request so fast that the answer is read with them holds the tunnel as awaiting until the client sends again or closes it (a refusal at the settle limit, never a wrong checkpoint); tickets sent in two separate writes, or an HTTP/2 server's own preface, read as an answer. Proved by `tests/proxy/test_tunnel_settle.py`: tickets at once, the answer two seconds later. An agent with a `Reported` wake source is asked for its report again once quiet (`ports.agent.Reports`); a call made while it is asked starts the quiet again. One that has not settled within `settle_limit` is written as `NotRestorable(reason)`, e.g. "the agent was still making outbound calls when the settle limit (0.5 s) ran out: its last, GET /testchat/inbox, …", and is never snapshotted. A settled one is `Restorable(snapshot_of, wake, report)`: the report is what the restore must bring back. A run with hooks and nothing watching the agent's calls is refused.
- **Restore** is a sequence, each step's output kept in the child's `restore.json`: `stop` (the agent's command, when Minutehand started it with `--`, then the agent's own `stop`), `restore`, `start` (the agent's own, then Minutehand's command), then `answer`: the report endpoint must answer within `answer_limit`. A step that fails, cannot be started or runs past `step_limit` refuses the fork: "the restore of the agent from the checkpoint at seq 18 failed at step `restore`: … exited 5 after 0.1 s. Its output: …". `minutehand fork` names each step on stderr as it is taken. A sample after the first is restored the same way from the first sample's setup.
- **Verify.** The report after the restore is compared with the recorded one (`differences`). Equal means: the same `status`; a `next_wake` that is the same instant, or both none; the same commitments as a set keyed by `Commitment.key`, each with the same `status`, where none reported and an empty list are the same. A difference refuses the fork field by field: `next_wake: 2026-08-26T09:00:00+00:00 at the checkpoint, none after` is a restore that silently did nothing; `…, 2026-08-28T09:00:00+00:00 after` is the snapshot of another wake restored.
- **What the comparison cannot see:** anything the report does not carry (a conversation, a cache, a draft, a commitment's description and dates); a restore that put back another moment whose report reads the same; state in a service the agent uses that was not restored and is not reflected in its report; in-memory state in a process the restore did not stop and start. An agent with no `Reported` source (`Command`, `Polled`, by message only) cannot be asked between wakes: it is restored, and `restore.json` and `minutehand fork` say it was not verified and why.
- **Refusals leave nothing.** No hooks, no checkpoint at the seq, a checkpoint not restorable, its snapshot never kept or pruned, a booking pending, a ticket edit that cannot land: each is refused before `Store.fork`. A restore that fails after the child exists discards it (`Store.discard`) and `session.fork` removes its directory. Before, every refusal after `Store.fork` left an empty child run in the world file.

What no fork can rewind, said in the refusals and in `examples/state/README.md`:

- what a real third-party service the run reached keeps: the proxy refuses unclaimed hosts, but a tunnelled host (a model API) is reached for real;
- what a model provider keeps on its side: a stored conversation or response, a cache, a batch, an uploaded file;
- the AWS provider's queues and schedules, in moto's memory;
- background work in the agent that outlives the quiet period, and calls that never pass the proxy (to `localhost`, or on a tunnel already open), which settling cannot see.

Without `StateHooks`, `fork_run` is refused: a world rewound under an agent that remembers the future is not a rerun.

A fork can change something, and none of it touches the agent's code:

```python
Override = Annotated[PromptPatch | ModelSwap | PersonChange | TicketEdit | DeadlineShift, Field(discriminator="kind")]


class Fork(Model):
    parent_run: str
    at_seq: int = Field(ge=0, description="The last WorldEvent.seq the fork shares with its parent")
    overrides: list[Override] = []
    samples: int = Field(default=1, ge=1)
```

| Override | What changes | Where | Tested |
|---|---|---|---|
| `PersonChange` | A person's `ReplyBehaviour` from the fork onward; every message to them still unanswered at the fork is put to them again | `changed_scenario`, `_ask_again` in `application/rewind.py` | Through a whole run (`tests/e2e/test_fork_calls_telemetry.py`) |
| `TicketEdit` | A ticket's state or assignee, as actor `SCENARIO` | `EditsTickets.edit` | `tests/orchestrator/test_rewind.py` |
| `DeadlineShift` | The scenario's deadline | `changed_scenario` | `tests/orchestrator/test_rewind.py` |
| `PromptPatch`, `ModelSwap` | The agent's prompt or model | On the wire: the proxy's `EDIT` policy rewrites the body of the agent's request to its model API | At the proxy only (`tests/proxy/test_model_hosts.py`); not through a whole run |

- `adapters/proxy/edit.py` knows three wire shapes that carry a system prompt: OpenAI chat completions (`messages[0]` with role `system` or `developer`), OpenAI responses (`instructions`), Anthropic messages (`system`). A body no edit applies to goes on byte for byte. Edits match the request as the agent sent it, so a model swap cannot change which prompt patches apply.
- `CallMatch` picks which of an agent's several prompts a patch applies to (`host`, `model`, `system_contains`). How reliably `system_contains` singles one out in a real agent is untested.
- The agent's model calls are never replayed. A patch changes the request and the real model answers it. They are recorded, as spans, only when the agent exports its own telemetry or the run is started with `--record-model-calls`.
- Patching means the proxy opens model traffic it otherwise only tunnels, so it sees prompts and the API key. Locally that stays on the developer's machine. Hosted, it is a trust decision for the customer.
- `Routing.apply` sets the edits for the run about to play; one run plays at a time through one proxy.

The parent repository's nearest equivalent restores one captured model step (its inputs and message history) and reruns that step under the current prompt. It restores nothing in Slack or the trackers.

### Hosted

Designed, not built. Hosted Minutehand keeps every run's log and can rewind or fork any of them on request. Each run executes in its own small virtual machine whose only route out is the proxy.

| Problem locally | Why the virtual machine removes it |
|---|---|
| The agent's own state needs snapshot and restore commands | The whole machine is snapshotted: the agent, its database, its files |
| A faked date must stay near the real one | The machine's clock is set to the simulated time; the proxy outside holds the real clock and issues certificates valid for the simulated date |
| An agent that reads the clock without the system library is out of reach | Every process on the machine sees the same clock |

It is the reason the hosted service is more than the open-source tool run for you.

### How the clock knows what is next

The clock jumps to the earliest `Due` (`AGENT_WAKE`, `PERSON_REPLY`, `DIRECTION`, `TICKET_FATE`). Four sources produce one, and a run may use several at once (`adapters/agent/reach.py` assembles them into `Reach`).

| Source | What the agent must do | Exact? | Cost of a quiet fortnight | State |
|---|---|---|---|---|
| **Replies and pushed events** | Nothing. The monitor plays the people and delivers through the provider. | Yes | None | Built (Slack) |
| **`Booked`**: the agent books wake-ups with a scheduler | Nothing. The booking is an outbound call the proxy already intercepts; a scheduler provider (`Manifest.books_wakes`) records the time and delivers when the clock reaches it. | Yes | None | Built and tested through a whole run on AWS (`tests/e2e/test_booked_on_aws.py`) |
| **`Reported`**: the agent answers `next_wake` at `report_url` | An endpoint, or an adapter beside its tests | Yes | None | Built and tested |
| **`Command`**: one process per wake, `WakeRequest` on stdin, `AgentReport` on stdout | A command | Yes | None | Built and tested |
| **`Polled`**: the agent is invoked every `every` (default 5 minutes) and decides for itself | Declare the rhythm | Yes, at that rhythm | One call per tick: 4,032 calls for 14 days at 5 minutes, each a wake counted against `Scenario.max_wakes` | Built and tested |

- `Polled` never skips a tick, never names a next wake and never reports `DONE`. Skipping is only safe when the agent says when it next matters, which is `Reported`.
- An agent may declare one `Reported` or `Command` source and one `Polled` source; `reach_for` refuses two of either. An agent with only `Booked` wakes must take its goal by message.
- An agent whose scheduler is an in-process loop over its own database is `Reported` through an adapter: nothing can intercept that loop.
- `Booked` is the source that makes a stranger's agent work with no adapter. `tests/providers/aws/test_aws_provider.py` shows it at the provider: a stock `boto3` client with no endpoint override creates an EventBridge Scheduler schedule targeting an SQS queue; `ReceiveMessage` finds nothing before the booking fires and the schedule's input after.

Every scheduler is translated into one internal shape (`Due`, booked through `Wakes`), so the clock knows nothing about any vendor:

| Scheduler | How the agent books | How the wake is delivered | State |
|---|---|---|---|
| AWS EventBridge Scheduler | `CreateSchedule`, `UpdateSchedule`, `DeleteSchedule` with `at(...)`, `rate(...)` or `cron(...)` (no `L`, `W`, `#`), a timezone, start and end dates, state, `ActionAfterCompletion` | Into the target SQS queue (with `MessageGroupId` for FIFO), where the agent's own poll finds it; taken (`ConfirmsDelivery`) when the agent deletes the message (`DeleteMessage`, `DeleteMessageBatch`, JSON or query protocol), and the run waits for that | Built and tested at the provider and through a whole run. Any other target raises when it fires. Each booking, delivery and the agent's delete of a delivery is in the log; the queues themselves are in moto's memory. |
| SQS delay | `DelaySeconds` | moto's own, on the machine clock | Not on the run's clock |
| Google Cloud Tasks | gRPC by default, plus a token fetch from Google's sign-in host; no `moto` equivalent | An HTTP call to the task's URL | Not attempted. gRPC responses need trailers, which the app host does not produce. |
| A fixed schedule set at deploy time (Cloud Scheduler, a Kubernetes CronJob, Vercel cron) | Not booked at run time at all | `Polled`, with the schedule written in the agent file | Designed |
| A workflow engine's timers (Temporal, Inngest) | Inside the engine | The engine's own time-skipping test server would have to be driven | Not designed |

### What the agent is waiting on

Built and tested (`checks/ledger.py`, `tests/checks/test_ledger.py`). The monitor does not need to be told. It plays every person and owns the clock, so it derives every wait from the world and the stored replies.

```python
class Obligation(Model):
    key: str
    kind: ObligationKind
    person: str | None = Field(default=None, description="Person.key")
    entity: EntityRef | None = None
    opened_at: AwareDatetime
    opened_by: int = Field(description="WorldEvent.seq of the ask or hand-off")
    expected_by: AwareDatetime | None = Field(
        default=None, description="After this, silence is the agent's to act on; None means no date applies"
    )
    patience: timedelta | None = Field(
        default=None,
        description="How long the person may take over each message on this wait: a follow-up gives them this long "
        "again from the moment it was sent. None: a follow-up does not move the date (work has its own pace)",
    )
    settled_at: AwareDatetime | None = Field(default=None, description="When the answer landed or the work was done")
    agent_touches: list[int] = Field(default=[], description="Agent events on the same person or entity while open")
    first_touch_after_settled: int | None = None
```

| `ObligationKind` | Opens when | `expected_by` | Settles when |
|---|---|---|---|
| `ANSWER_FROM_PERSON` | The agent messages a person who has a reply decided to it, or is `Silent`, and owes no answer in that conversation already | The message's time plus the person's `DelayRange.longest`; `patience` is that delay | The first reply to any message on the wait, if the run reached it and it was not withdrawn |
| `WORK_WITH_PERSON` | The agent creates a ticket assigned to a person, or reassigns one to them | The assignment's time plus that person's `TicketFate.after`; none without a fate | The person moves it to `DONE` or `CANCELLED` |
| `DATE` | The scenario has a deadline | The deadline | The run reaches it |

Whether a message asked anything is the replier's decision, never the ledger's. A person with a reply decided to the message was asked; a `Silent` person is asked by every message, since that is what `Silent` means; anyone else was told something that needs no answer (a thank-you, a report), away or not. A person who is only ever told things is `Scripted` with no replies, not `Silent`: the passing example's owner is one.

A message is the same ask as an earlier one, and so a follow-up on that wait rather than a wait of its own, when it goes to the same person in the same conversation (provider and channel; a thread shares its channel) while the earlier wait is open; an answer to it settles the wait. Nothing is read from the text, which gets two cases wrong: a second, different question in the same conversation before the first is answered is folded into the first, and its own answer settles both; and a reminder sent somewhere else (email after chat, a group channel after a direct message) is a new wait. A `Scripted` person whose script answers only their second message was, by the replier's decision, not asked by the first, so an agent that asked and then chased them is scored as having asked once.

A touch is any later agent event on the ask's entity or channel, or an agent message to the person or their delegate. `no_follow_up`, `late_follow_up`, `slow_to_react`, `kept_chasing_after_done` and the scorecard read the ledger; `chased_absent_person` reads the absences directly.

`AgentReport.commitments` stays optional. The cross-check it would allow (the agent believes it is waiting on something the world shows as answered, or the reverse) is not built.

### Time, for the agent

From least to most invasive; the fakes are on Minutehand's clock in every case.

1. `WakeRequest.now`. The agent uses it as its "now" for the wake. Built.
2. `GET :8081/clock`. For agents that read the time more than once per wake. Not built.
3. The system clock, faked from outside with `libfaketime`. No code change in the agent. Not built; see "Evidence" for the spike that tried it.

What follows from that spike:

- **A run under option 3 must start at the real date and stay inside the real certificates' lifetime,** about two months ahead, unless model-API traffic is terminated at the proxy and re-sent from the real clock. Terminating it means the proxy decrypts prompts it otherwise only tunnels.
- **A scenario with a fixed past `starts_at` cannot use option 3.** A scenario file that leaves `starts_at` out starts at the moment its run does, which is what an agent reading the real clock needs.
- **An agent that polls on a short real-time loop works under option 3:** after a jump its next poll reads the new time. The cost is one real poll interval per jump.
- **An agent that sleeps until a far-off moment does not wake.** It needs `Booked` or `Reported`.
- **The monitor still cannot see when an in-process scheduler next wants to run.** Jumping straight to the next reply would skip a follow-up the agent meant to send in between, and the run would blame the agent for lateness the jump caused. Such an agent is `Polled` at a declared rhythm, or `Reported`.
- Untested: a JVM under a faked clock (the Firestore emulator), Node, and a faked clock across several containers at once.

Real time still enters a run in three places: `WorldEvent.wall_time`; the Slack provider's `X-Slack-Request-Timestamp`, which the agent's signature verifier checks against its own clock; and moto, which runs SQS delays, visibility timeouts and timestamps on the machine clock.

### Telemetry

Two directions, kept apart. Minutehand SENDS its own run as OpenTelemetry to wherever the user points it. It RECEIVES the agent's own OpenTelemetry during a run and keeps it in the run's file, so a finding's evidence can name the model call that caused it whichever vendor, if any, the agent's team traces with. Minutehand never fetches traces from a vendor's API.

#### What Minutehand sends

Built and tested (`tests/telemetry/test_otel_telemetry.py`). OpenTelemetry SDK, exported over OTLP/HTTP; the CLI builds it only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. The SQLite store is the record; telemetry is an export of it. It carries nothing the agent exported: no received span, attribute or message is re-exported (`test_minutehands_own_telemetry_carries_none_of_what_the_agent_exported`).

- **Spans**

  | Span | Parent | Carries |
  |---|---|---|
  | `minutehand.run` | none | `minutehand.run_id`, `minutehand.scenario`, `minutehand.seed`, simulated start; at the end `minutehand.stop`, `minutehand.wall_seconds` |
  | `minutehand.wake` | run | `minutehand.wake`, `minutehand.wake.reason`, simulated time |
  | `<provider> <operation> <kind>`, e.g. `asana create ticket`, `SpanKind.SERVER` | the caller's span; else the wake; else the run | `minutehand.seq`, `minutehand.wake`, `minutehand.entity.id`, `minutehand.actor`, simulated time; with a call, `http.request.method`, `server.address`, `url.path`, `http.response.status_code` |
  | `<method> <host>`, one per captured call, `SpanKind.CLIENT`, at its real start and end | the caller's span; else the wake; else the run | `http.request.method`, `server.address`, `url.path`, `http.response.status_code`, `minutehand.wake`, `minutehand.capture.mode`, `minutehand.capture.answered_by`, `minutehand.capture.declared_as`, `minutehand.capture.replayed_from`, simulated time; never a body |

- **Joining the agent's trace.** When the intercepted request carried a valid W3C `traceparent`, the world-event span is created as its child and linked to its wake, so what happened in the world sits in the same trace as the model call that caused it.
- **Simulated time.** A world-event span starts and ends at its event's `wall_time`, because backends reject or misplace future timestamps. Simulated time travels as `minutehand.sim_time` (ISO 8601 string) and `minutehand.sim_time_unix_nano` (int). World-event spans are emitted when the run loop reads a wake's new events, after the wake.
- **Findings.** One log record per `Finding` (`event_name="minutehand.finding"`), in the trace of the span of its first evidence, with `minutehand.check`, `minutehand.finding.kind`, `minutehand.evidence`, `minutehand.pattern`, and OTel severity from `Finding.severity`.
- **A fake's 4xx is not an error.** A provider refusing bad input is the fake working; the span status stays unset. A 5xx sets status `ERROR`.
- **Bodies stay local** unless `MINUTEHAND_EXPORT_BODIES=1` (`minutehand.request.body`, `minutehand.response.body`).
- **Metrics.** `minutehand.findings{check,kind}`, `minutehand.wakes{changed}`, and per run, by scenario, the histograms `minutehand.time_lost_seconds`, `minutehand.idle_wakes`, `minutehand.follow_ups_late`, `minutehand.run.sim_seconds`, `minutehand.run.wall_seconds`.

#### What the agent sends

Built and tested (`tests/telemetry/test_receiver.py`, `tests/test_store_spans.py`, `tests/test_model_call_join.py`, `tests/proxy/test_record_model_calls.py`, `tests/e2e/test_agent_telemetry.py`).

- **The receiver.** `adapters/telemetry/receiver.py`: an OTLP/HTTP endpoint served for the length of a run, on the proxy's host (`--proxy-host`) and its own port (`--telemetry-port`, default any free one), moved from run to run with the proxy (`session.Intercepting.mount`). `POST /v1/traces` keeps every span; `/v1/logs` and `/v1/metrics` are acknowledged and dropped. Each takes `application/x-protobuf` or `application/json` (OTLP/JSON, whose ids are hex), gzip, deflate or neither, decoded with `opentelemetry-proto`'s own messages (`otlp.py`), and is answered in the format it came in. A body that is not OTLP is a 400; an unknown content type or encoding a 415; the run goes on. `--no-receive-telemetry` serves none and leaves the agent's exporter alone.
- **What the agent is handed.** `minutehand run … -- cmd`, `minutehand env` (which then needs a fixed `--telemetry-port`) and the Compose override set `OTEL_EXPORTER_OTLP_ENDPOINT` to the receiver, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` for SDKs that read only that, and `OTEL_EXPORTER_OTLP_PROTOCOL` and `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` to `http/protobuf`. The name the agent has for this machine joins `NO_PROXY`, so its exporter is not sent through the proxy. `tests/telemetry/test_receiver.py` drives the stock Python SDK in a process of its own configured by these variables alone.
- **What is kept.** `domain/telemetry.py`:

  ```python
  class ReceivedSpan(Model):
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


  class StoredSpan(Model):
      span: ReceivedSpan
      run_id: str = Field(description="The run it arrived in; a fork lists its parent's spans of wakes up to the fork")
      source: SpanSource
      wake: int = Field(
          ge=0,
          description="The wake it belongs to: the one whose real-time window holds its start, else the one it arrived in; 0 is setup",
      )
      placed_by: Placement
      arrived_in_wake: int = Field(ge=0, description="The wake in progress when it arrived")
      sim_time: AwareDatetime = Field(description="Simulated time when it arrived")
      after_seq: int = Field(ge=0, description="The head of the world's log when it arrived")
  ```

  An attribute's value keeps OTLP's kinds (string, bool, int, double, bytes, array, key/value list) as a union discriminated on `kind`. The run loop records the real moment each wake begins and, after its checkpoint, ends (`Store.wake_began`, `wake_ended`). `Store.receive(spans, source=)` places each span in the wake whose window holds the span's own start (`Placement.WINDOW`), or, when none does (setup, or between wakes), in the wake it arrived in (`Placement.ARRIVAL`); the arrival wake, simulated time and head of the log are kept too. Its own start and end stay the real times its SDK gave it. `Store.spans(trace_id=, wake=)` reads them back by placement, and a fork's view of its parent's spans uses the same placement.
- **Passing it on.** When the environment Minutehand was started from already names an OTLP endpoint (`OTEL_EXPORTER_OTLP_ENDPOINT`, or a signal's own `…_TRACES_ENDPOINT`), every payload the receiver takes, traces, logs and metrics, read or not, is sent on to it unchanged with `OTEL_EXPORTER_OTLP_HEADERS`, in the background (`forward.py`). A failure is recorded in the run (`Store.forward_failed`, the `forward_failure` table) and never fails it. A destination that is the receiver itself is dropped rather than looped.
- **The wire as the fallback trace.** For an agent with no tracing, `--record-model-calls` puts model hosts under `HostPolicy.RECORD`: each call is decrypted, sent on unchanged, and kept as a span of `SpanSource.WIRE` in the same stored shape, with GenAI-convention attributes (`gen_ai.system`, `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.system_instructions`, `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`). The three request shapes `edit.py` knows are read, answered as JSON or as server-sent events. A stream reaches the agent as a stream: the addon sets mitmproxy's `response.stream` to a function that passes each chunk on as it arrives and keeps a copy, and reads the copy when the stream ends (`test_a_streamed_answer_reaches_the_agent_as_a_stream_and_is_kept_whole` holds the model API's second chunk back until the client has the first). A call that carried a `traceparent` is put in that trace under that span. Nothing from the request's headers or query string is stored; a test searches the store file's bytes for the key. Off by default: without the flag a model host stays `TUNNEL`.
- **The join.** `application/model_calls.trace_of(event, world)`: from the `traceparent` the intercepted call carried, the agent's calling span, its ancestors to the root, every span of that trace with a `gen_ai.*` attribute, and the model call that led to the event: of the spans whose `gen_ai.operation.name` is `chat`, `text_completion` or `generate_content` (or that name no operation and carry a model), the last to END before the calling span started. With no `traceparent`, or no model call in the trace, it takes the last model call of the same wake that ended before the event, a wire-recorded one only if it was kept before the event's seq, and says so (`JoinedBy.WAKE`: the nearest call, not a proven cause).
- **Where it shows.** `show_evidence` gives each cited event its `model_call` (model, the messages the span carries, token counts), `joined_by` and the agent's span names, and says in `telemetry` when the run received nothing. The viewer serves `GET /api/runs/{run_id}/model-calls` (each event a finding cites, joined) and `GET /api/runs/{run_id}/traces/{trace_id}`; its page shows "What the model was asked and answered" under a finding, or that no telemetry was received.
- **Privacy.** Received spans often hold whole prompts. They are kept in the run's `world.db` on the machine running Minutehand and go nowhere else except to the endpoint the agent's own environment already named.

#### How telemetry serves the two pillars

| Pillar | What the agent's telemetry adds | Built |
|---|---|---|
| Measuring | A finding says what went wrong in the world; its evidence now says what the agent's model was asked and answered just before, so "followed up 33 hours late" comes with the prompt that chose silence. Every span is placed in the wake its start fell in, so a wake's model calls and tokens can be read beside what it changed. | The join and its surfaces. `idle_wake` reads each wake's model calls (`RunView.model_calls`). No scorecard number counts tokens. |
| Comparing a fork with its parent | A fork sees its parent's spans of the wakes up to the fork, and keeps its own after it; the same event in parent and child can be read with the model call behind each, so a `PromptPatch` can be judged by what the model was then asked and answered, not only by what the world did. | The fork's view of spans. No side-by-side of a parent's and a child's model calls. |

### What a coding agent calls

Designed, not built. `mcp` is a declared dependency; no module imports it.

| MCP tool | Returns |
|---|---|
| `list_scenarios` | names and goals |
| `run_scenario(name, agent)` | `run_id`, counts by `FindingKind` |
| `list_findings(run_id)` | `list[Finding]` |
| `show_evidence(run_id, finding)` | the `WorldEvent`s, their `Exchange`s, the wake, the trace id, and for each event the agent's model call that led to it when the run received its telemetry |
| `rerun_from(run_id, wake)` | a new `run_id` started from that checkpoint |

What exists is the command line (`cli.py`):

```
minutehand run <scenario.yaml> --agent <agent.yaml> [--state DIR] [--samples N] [--json] [-- <command...>]
minutehand findings <run_id> [--state DIR] [--json]
minutehand fork <run_id> --at <seq> --changes <fork.yaml> [--state DIR] [--json] [-- <command...>]
minutehand runs [--state DIR]
minutehand env --agent <agent.yaml> --proxy-port N [--format shell|compose] [--service NAME...] [--ca-path PATH]
```

`run`, `fork` and `env` take `--proxy-host`, `--proxy-port`, `--agent-proxy-host`, `--no-proxy HOST` (repeated), `--telemetry-port`, `--no-receive-telemetry`, `--record-model-calls`, `--capture-unknown` and `--upstream-ca FILE` (`serve` takes the last two as well). `run`, `fork` and `findings` print an "outbound calls" section, per host no provider claims, and a declaration for each host nobody declared. `env` prints the environment an agent Minutehand does not start needs, for every run on that port under that state directory: `export` lines, or a Compose override. It makes the proxy's CA if there is none yet, and refuses a port left to the system and a signing secret generated per run.

`run`, `fork` and `findings` exit by the verdict (see "The verdict"): 0 passed, 1 failed, 3 not finished, and 2 when the run could not be performed. With samples: 1 when any sample failed, else 3 when any did not finish, else 0. The state directory defaults to `$MINUTEHAND_STATE`, else `.minutehand`. A fork's changes file holds `overrides` and optionally `samples`; one that names `parent_run` or `at_seq` itself is refused.

### Distribution: a tool beside the codebase, never a dependency of it

The target is zero lines changed in the project under test. Minutehand is installed and run the way a linter is, outside the project's own dependencies.

| Channel | For | Touches the project | State |
|---|---|---|---|
| PyPI, run as `uvx minutehand …` | Anyone with Python 3.12 available | Nothing. `uvx` runs it from its own environment. | Not published; `pyproject.toml` declares the `minutehand` console script, version 0.0.1 |
| Docker image, built from the same release | Any stack, and CI | Nothing | Not built |
| The git repo | Contributors and provider authors; also `uvx --from git+https://…` before the first release | Nothing | Exists |

PyPI and the repo are not alternatives: the repo is the source, PyPI and the image are how a release reaches a user. The names `minutehand` and `minute-hand` were unclaimed on PyPI and npm on 2026-10-04 and stay claimable by anyone until a first upload.

One command wraps the agent's own start command and injects everything through the environment:

```
minutehand run scenario.yaml --agent agent.yaml -- python -m my_agent
```

| What the run needs | How it gets there with no code change | State |
|---|---|---|
| Outbound calls reach the fakes | The wrapped command gets `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY=localhost,127.0.0.1` (each in lower case too), and the CA bundle in `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `HTTPLIB2_CA_CERTS`, `AWS_CA_BUNDLE`. An agent Minutehand does not start gets the same from `minutehand env` | Built. A client that pins certificates is out of reach. `httplib2`, which `googleapiclient` uses, reads `HTTPS_PROXY` only when PySocks is importable and otherwise connects to Google directly, in silence: an agent on `googleapiclient` needs `pysocks` installed (`tests/providers/google_drive/test_drive_through_proxy.py` runs with it; its `offline/` guard is what turns the silent bypass into a failure). Node's built-in `fetch` needs `NODE_USE_ENV_PROXY=1`; unverified. The Compose override is tested as text; no container has been run with it. |
| The agent is up before the run starts | Minutehand waits up to 30 seconds for its wake URL, or else its first inbound URL, to accept connections, and fails the run if the command exits first; its output goes to `agent.log` | Built |
| Pushed events reach the agent | The agent's event URL and where its signing secret comes from are in the agent file: generated per run and handed to the command, or the agent's own, read from a variable of Minutehand's | Built |
| The agent wakes at the right moments | Replies, pushed events and `Booked` wake-ups need nothing. `Polled` needs a URL in the agent file. | Built. `Reported` needs an endpoint or an adapter, which is code, though it can live outside the project. |
| The agent agrees on what time it is | `WakeRequest.now`; `libfaketime` preloaded through the same wrapper | `WakeRequest.now` built; `libfaketime` not built |
| Scenarios and the agent file | Plain YAML or JSON files, in the project or anywhere else | Built |

- A run needs no client package and nothing imported. A suite that uses `minutehand serve` imports `minutehand.testing` (a client and a pytest plugin), which is test tooling, never a dependency of the code under test.
- mitmproxy requires Python 3.12 and pins many dependencies, which is one more reason the tool never enters a project's environment.
- Providers register under the entry-point group `minutehand.providers`. Five ship inside `minutehand`; anything else is `minutehand-provider-<name>`, installed into the tool's environment with `uvx --with`.

### Python, not Rust

- A run's time is the agent's model calls, seconds each. A fake that answers in about a millisecond (see "Evidence") is not on the critical path, and Rust would not shorten a run.
- Providers are where the work is, and they are written by Python-speaking agent builders and by coding agents.
- What Rust would buy: one static binary with no runtime, and a smaller image. Both matter for distribution, not for speed.
- The seam that keeps the door open: a provider speaks ASGI and a store interface. The proxy and store behind that seam can be replaced without touching a provider.
- Revisit when a measurement says so: memory or latency with a large world, or adoption blocked by needing Python.

### Packages

| Package | Use |
|---|---|
| `mitmproxy` 12.x (MIT) | Proxy, TLS interception, tunnelling and editing model calls. Embedded through one addon; each provider's ASGI app is served with `asgiapp.serve()`, which does not do WebSockets or streaming. |
| `pydantic` 2 | Every model |
| `starlette` | Every provider app |
| `asgiref` | `WsgiToAsgi` around moto's WSGI server |
| `moto[server]` (Apache-2.0) | AWS |
| `httpx` | Agent drivers; Slack's pushed events |
| `pyyaml` | Scenario, agent and fork files |
| `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | Telemetry Minutehand sends |
| `opentelemetry-proto`, `protobuf` | Reading the OTLP the agent sends |
| `uvicorn` | Serving the OTLP receiver in the run's event loop, the viewer, and `serve`'s control API |
| `mcp`, `flask` | Declared in `pyproject.toml`; no module in `src/` imports either |
| `uv`, `ruff`, `pyright`, `pytest` (with `pytest-xdist`, `pytest-socket`, `pytest-timeout`, `pytest-randomly`) | Tooling |

Named by the design and not dependencies: `datamodel-code-generator` (Pydantic models from a provider's OpenAPI document; Asana publishes one, YouTrack serves one at `/api/openapi.json`, Slack's official spec repo is abandoned), `openapi-core` (validate a fake's responses against the published document), `libfaketime`, `duckdb`.

Codebases worth reading before writing a provider: `vercel-labs/emulate` (Apache-2.0; Slack, Google, GitHub surfaces), LocalStack (lazy service loading), `moto` (one server, many services).

## Patterns

```python
class Pattern(Model):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    failure: str = Field(description="What the agent does wrong, in one sentence")
    design: str = Field(description="What a proactive agent does instead")
    reference: str | None = Field(default=None, description="Where a working implementation can be read")
```

Nine, in `checks/patterns.py`; each `Pattern.reference` is its page `docs/patterns/<key>.md`. Each is taken from a mechanism a production agent has.

| `Pattern.key` | Failure | Design | Found by | Reference mechanism |
|---|---|---|---|---|
| `expiry_on_every_wait` | Waits on something forever | Every wait carries an expected-by date and the agent wakes on it | `late_follow_up`, `no_follow_up`, `slow_to_react` | An expected-by date on every blocker and one "next stale check" time derived from them, which the scheduler books |
| `check_world_before_model` | Spends a wake, and the model calls in it, to learn nothing changed | Spend a wake's model calls only on what changed since the last look; the page lists why a wake can change nothing and what each cause needs | `idle_wake` | A filter to the waits actually stale, and a cheap preflight that ends the wake when none is |
| `absence_aware` | Chases someone who is away | Know who is away and until when; extend the wait or go to their delegate | `chased_absent_person` | An absence filter over every follow-up before it is sent, rerouting to the named cover |
| `budgeted_follow_up` | Follows up too often, or too late | Space reminders across the time left before the deadline | `acted_after_deadline`, `nagged` | The next reminder computed from the time remaining and the number already sent |
| `bounded_asking` | Asks for input indefinitely | After a fixed number of attempts, stop asking and deliver the best available version | none | A count of attempts per unmet need and a pivot to best-effort delivery past a threshold |
| `one_open_ask_per_person` | Sends the same question twice | Track what is already open with each person before asking | `repeated_message`, `duplicate_ticket`, `kept_chasing_after_done` | A judge that compares each outgoing question with those already open with the same person |
| `no_double_tick` | Does the weekly task twice | A recurring task has one instance per period | none | A check for an existing instance in the current period before each cadence tick creates anything |
| `honest_closure` | Reports done when it is not | Closing is decided from the state of the world, not from the agent's last message | `expectations` | Closure evaluated against the recorded state of every piece of work the goal depends on |
| `confirm_names` | Acts on a name it guessed | A name that matters is carried exactly as given, and an assumption is asked about before it is acted on | `near_miss_name` | None. Run `f431fc97f427` is the evidence one is needed. |

- Patterns are documentation in the repo, one page each, and data the tool returns (`pattern(key)`; the CLI prints the pattern under each finding). They are not code the user must import.
- The reference mechanisms are what make them more than advice. Publishing the implementations they point to is the separate, heavier product.
- A scenario library graded from easy to hard, with a pass mark, is the form in which this becomes a standard others measure against. It is not designed yet.

## Queued behind a working emulator suite

### People written by a model

Designed, not built. The models exist; `ScriptedReplier` refuses any person whose reply is `Answers`, naming them, before the run starts.

```python
class Person(Model):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    email: str
    title: str | None = None
    facts: list[str] = Field(default=[], description="What this person knows; all a model reply may draw on")
    stale_facts: list[str] = Field(default=[], description="What they believe that is no longer true")
    reply: ReplyBehaviour = Answers()
    working_hours: WorkingHours | None = None
    absences: list[Absence] = []


class Answers(Model):
    kind: Literal["answers"] = "answers"
    delay: DelayRange = DelayRange()
    helpfulness: Helpfulness = Helpfulness.FULL
    voice: str | None = Field(default=None, description="How they write: terse, formal, chatty")
    model: str | None = Field(default=None, description="None uses the run's default model")
    temperature: float = Field(default=0.6, ge=0, le=2)
```

What is built for people today (`application/replier_scripted.py`, `ScriptedReplier`):

- `Scripted`: the nth message the agent sends a person gets the `ScriptedReply` with `to_ask == n`, or no reply. `Silent`: never.
- The delay is drawn from the person's `DelayRange` (default 6 to 66 hours) by hashing the scenario's `seed` with the asked message's identity, so the same seed gives the same delays on every run and every fork.
- A reply that would land inside an `Absence` (from the start, or from the first ask) or outside `WorkingHours` (in the person's own timezone) moves to the next moment they would answer.
- A reply is stored the first time it is decided and arrives through the provider as a real inbound event: a threaded reply in a channel, a new message in a DM.
- A scripted reply may use a control instead of writing back (`ScriptedReply.press`): the control on the asked message whose label reads `ScriptedPress.label`, in any case, a person picked for a person picker, and `form` typed into the modal the agent opens in answer. A message with no such control gets no reply: the person cannot press what is not there. A model-written person is shown the message's controls (a link is not one) and may answer with `press` and `form` instead of `text`. Either way the reply is a `PersonReply` with `press` set, stored and replayed like any other, its `text` what the person typed or else the label, so `Relayed` hears a reason typed into a form. The press lands as `InteractionSnapshot` events by actor `PERSON` naming the person, the control, its value and what was typed: "nadia pressed “Accept”" is one event in the log.
- An edit that changes a message's controls without changing its text is put to the person again, as a text change is: a "Thinking…" message turned into a card by `chat.update` asks its question then.
- `Scenario.happenings` are what people do unprompted at set offsets, in three families (see "Things people do by themselves"); a messaging one (post, edit, delete, react, join, open the agent's Home tab, run a slash command) wakes the agent with `PERSON_REPLIED`.

Designed:

- Each knob changes what the agent has to cope with: `PARTIAL` and `ASKS_BACK` force a second round, `DECLINES` forces a reroute, `MISTAKEN` plus `stale_facts` tests whether the agent verifies.
- The replier's first decision is whether a message needs an answer at all. That decision is what opens an `Obligation` (`Replier.decide` returning `None` already opens none).
- A model-written reply is stored the first time it is written. A fork replays it, so a rerun costs no model call for the people and plays out the same.

### Checks that need a model

Designed, not built. "Does this ticket make sense to the person it was assigned to" cannot be computed. A judged check implements the same `Check` protocol and returns the same `Finding`, with three differences:

- `FindingKind.REVIEW` unless the scenario sets a pass rate over several samples.
- The model, its prompt version and its rationale are recorded with the finding.
- It runs after the deterministic checks and only on entities they passed, so a ticket already failed for a wrong name is not judged for clarity.

Every finding, judged or not, becomes one OpenTelemetry log record in the trace of the entity it is about, so "tickets judged unclear, by scenario, across ten thousand runs" is a query in whatever store receives the telemetry.

## Build tracks

The contracts came first, alone; the parallel tracks were written against them.

| Track | State |
|---|---|
| **Contracts**: the models in `domain/`, the protocols in `ports/` | Built |
| **Proxy runtime**: host routing, lazy load, CA, tunnel, edit and refusal policy | Built. Base-URL mode not built. |
| **Store**: schema, events, checkpoints, bodies and snapshots kept once | Built. Checkpoints are rows in the log; bodies of 512 bytes or more and snapshot files are content-addressed. |
| **Clock and orchestrator**: `next_jump`, wake sources, the run loop, forks | Built. The fork is `minutehand fork --at <seq>`, not `rerun_from`. |
| **Providers**: Slack, YouTrack, Asana, Drive | Built, each written new over the store and the clock rather than mounting the parent repository's fakes |
| **Scheduler provider** (`Booked`) | Built for AWS |
| **Checks and the obligations ledger** | Built |
| **Telemetry export** | Built |
| **Telemetry received from the agent**: receiver, storage, forwarding, the join, `--record-model-calls` | Built |
| **Surfaces**: CLI, MCP, viewer | CLI built. MCP and viewer not built. |
| **Composition and end-to-end**: `session.py`, files, agent drivers, a real agent process on stock `slack_sdk` | Built |
| **Adoption in the parent repository**: one container in compose and CI, the adapter, the deletions | Not built here; see the adoption guide in `docs/` |
| **People written by a model** | Not built |
| **Generated providers** (the `GENERATED` engine), then **judged checks** | Not built |

Every fake of the parent repository now has a provider here: Jira, Notion and Microsoft (Teams and Graph, one provider since they share a host and a directory) landed on `integrate/providers-2`.

## Licence

Decided 2026-10-04: the licence must stop anyone else selling Minutehand as a hosted service, because Alknoma will.

| Licence | Stops a competing hosted service | Still "open source" by the OSI definition | Used by |
|---|---|---|---|
| **Functional Source License (FSL-1.1-ALv2)** | Yes: any use except a competing product | No, "fair source". Each release becomes Apache-2.0 two years after it ships. | Sentry, Codecov, Liquibase, Convex, PowerSync, GitButler |
| Elastic License 2.0 | Yes: may not be offered as a managed service | No. Never converts. | Elastic, Arize Phoenix, Nango |
| Business Source License | Yes, with terms each vendor writes | No. Converts after a delay the vendor picks (four years at HashiCorp). | MariaDB, HashiCorp |
| AGPL-3.0 | No. Hosting is allowed; changes must be published. | Yes | Grafana, k6 |
| Apache-2.0 | No | Yes | LocalStack before 2026, vercel-labs/emulate |

FSL-1.1-ALv2 for the tool from its first commit; `LICENSE.md` carries it today. Starting under it avoids the relicensing that drew forks at HashiCorp, Redis and LocalStack. The cost: it cannot be called open source, only source-available or fair source, and some companies' policies admit OSI licences only. Confirm with a lawyer before the first public commit.

## Testing

- `uv run pytest -q -n auto`: every test, hermetic. Sockets are refused except to `127.0.0.1`, `::1` and `localhost`, each test fails at 60 seconds, the order is random every run, and a warning raised against our own code is an error (`pyproject.toml`). `.github/workflows/nightly.yml` repeats the suite five times and runs it against the newest release of every client library.
- A check is tested on a hand-built world whose obligations come from the real ledger (`tests/checks/world.py`). The checks written for the captured run are tested on it, including that `near_miss_name` goes clean when the defect is removed from the run (`tests/test_checks_on_reference_run.py`).
- A provider is tested over its ASGI app, against the store as its only state, through its refusals (`test_*_refusals.py`; AWS's are in `test_aws_provider.py`), and through the service's own client library over a real socket (`slack_sdk`, `asana`, `google-api-python-client`, `boto3`; YouTrack, which has no client library, with `httpx` through the run's proxy over TLS, one test per call a production YouTrack client makes and per query shape it builds). Response validation against a provider's OpenAPI document is not built.
- Whole runs start a real agent process (`tests/e2e/agents/slack_agent.py`) through `session.play`, `session.fork` and the `minutehand` command.
- `uv run python -m lints` runs every lint in `lints/`. See `docs/lints.md`.
- Conformance against the real APIs needs real accounts and is a scheduled job, not a lint. Not built.

## Evidence

Measurements from 2026-10-04, from throwaway spike scripts that are not in this repo. Each is kept because a decision above rests on it; where code now proves the same thing, the test is cited instead.

**Interception, one process** (macOS, Python 3.12, mitmproxy 12.2.3, the parent repository's Flask fakes for Slack and Asana mounted unchanged; again as one container on `python:3.12-slim`). Decides "Python, not Rust" and the one-container design.

| Measure | As a process | As one container |
|---|---|---|
| Ready to accept calls | 0.26–0.54 s over five starts | 1.6 s including `docker run` |
| Memory, no provider loaded | 87 MB | 57 MiB |
| Memory, Slack and Asana loaded | 91–97 MB | 58 MiB |
| Memory after 1,100 calls | 94 MB | not measured |
| Image size | n/a | 367 MB |
| One call | p50 0.9 ms, p95 1.0 ms | not measured |
| Eight clients at once | 1,401 calls a second, p50 5.4 ms | not measured |

Performance is known only at this scale: two providers, a few calls. Nothing is measured on the providers in this repo, with thousands of entities, or with concurrent runs.

What that spike found that code now proves:

| Finding | Now proved by |
|---|---|
| mitmproxy can be embedded and answer for real hostnames | `tests/proxy/test_answer.py` |
| A client needs environment variables only | `test_a_client_configured_only_by_environment_is_answered`; the Asana, Drive and AWS client tests |
| `traceparent` survives onto the recorded call | `test_exchange_keeps_traceparent_and_strips_credentials` |
| An unclaimed host is refused with 502 and recorded | `test_unclaimed_host_is_refused_and_recorded_without_contacting_it` |
| `connection_strategy="lazy"` and `upstream_cert=False` keep the real host uncontacted (the spike set them and did not watch the network) | The two `…_without_contacting_it` tests, which do |
| The real Asana API is under `/api/1.0` and a fake at the root misses it | `Manifest.path_prefix`; `test_exact_host_is_answered_with_the_path_prefix_stripped` |
| A provider loads on first use | `test_provider_module_is_imported_on_its_first_request` |
| A second embedded master served certificates its CA did not sign | `ProxyRunning`; `test_a_second_proxy_while_one_runs_is_refused` |
| Prompt edits on the wire leave the client unchanged and the user message untouched | `tests/proxy/test_model_hosts.py` |
| An agent on stock `boto3` books an EventBridge Scheduler schedule that the clock fires into SQS | `tests/providers/aws/test_aws_provider.py` |

Still true of mitmproxy and kept as a limit: its app host buffers each response whole and does not implement WebSockets (its own docstring). Streaming responses and Slack Socket Mode need a different path.

**A faked system clock** (`libfaketime`, an unmodified Python program in a Linux container, clock set by writing a file). Decides "Time, for the agent".

| Question | Result |
|---|---|
| Does the program's clock follow the file while it runs? | Yes, three jumps across 14 days, each read back within a minute of the value written. The sub-minute difference was not investigated. |
| Does a secure connection to a real model API still work under a faked date? | 14 and 60 days ahead: yes, for `api.openai.com` and `api.anthropic.com`. 200 days ahead: no, "certificate has expired". 60 days back: no, "certificate is not yet valid". |
| Does a program asleep on its own timer wake when the clock jumps? | No. `asyncio.sleep(8)` took 8 real seconds across a two-hour jump. |

**A production Teams and SharePoint client against the Microsoft provider** (2026-10-04, a throwaway driver run in its own virtualenv from that client's requirement files, through the proxy, with only its credential stores stubbed). 75 of 80 checks passed: the Teams messaging adapter's sends, replies, card sends and updates, deletes, DMs, history, members, search and workspace discovery; the Graph client's delegated and app-only tokens, item reads and writes, `delta` and subscriptions with the validation handshake; the SharePoint change watch; and the SharePoint document adapter's probes, moves, shares, deletes and containers. The five that failed are the client's, not the fake's: it reads a group or personal chat's history at `/teams/{group}/channels/{chat}/messages` (answered 404, which it swallows into an empty list); it downloads content with `httpx` without following the 302 Graph answers (the old emulator answered 200, which hid this); and its document adapter refuses a rename itself and fails its content update on the same 302.

## Known issues / limitations

- **The agent under test is a model, and its variance is reported, not hidden.** One run fails on any failed check. `--samples N` runs the scenario N times and reports `Stability(samples, passed)`: "passes 3 of 5" is the finding. Each sample after the first starts from the agent's state at the first sample's start, restored and verified as a fork's is; without hooks the samples are not independent.
- **One proxy per process.** mitmproxy keeps its master in a module global; `Proxy` refuses a second and is moved from run to run with `mount`, or, under `minutehand serve`, routes each call to its world (`route`).
- **A standing world isolates only by what the call carries.** Services that hold one fixed credential per provider put every test's calls in one world (`docs/serve.md`).
- **A fork starts only at a restorable checkpoint,** and only for an agent with `StateHooks`. A checkpoint at which the agent did not settle within `settle_limit` is not restorable.
- **A Firestore emulator restore is a restart:** Google's emulator imports only as it starts; measured at 4.5 to 16.7 s over four restores here, about 58 s on a more loaded machine.
- **AWS cannot be rewound.** moto holds queues, messages and its copy of each schedule in process memory, and every run's app takes a fresh AWS account, so a fork from any checkpoint after the agent first used AWS is refused, naming what it cannot rewind. Re-creating moto's state from the log is not built: queue creation, sends and receives are calls, not log entries, and SQS visibility timeouts run on the machine clock. moto reads the machine clock.
- **Slack's signature timestamp is real time** while message `ts` and `event_time` are simulated.
- **Slack's default workspace accepts any token.** A world whose `SlackSeed.workspaces` declares none has one workspace, `T0WORKSPACE`, in which any `xoxb-` or `xoxp-` token acts as the bot; a world that declares workspaces accepts only their tokens.
- **A Slack event retry is not spaced out.** Slack retries after about a minute and then five; the fake retries at once, since no simulated time passes while the agent is called.
- **A press that means to fill a form waits three real seconds** for the agent to open it with the press's `trigger_id`, as Slack's trigger lives three seconds; an agent slower than that fails the run (`FormNeverOpened`).
- **Most providers accept any token.** Slack treats any `xoxb-` or `xoxp-` token as the bot; Asana and YouTrack accept any bearer token unless their own seeds declare tokens. Drive accepts only tokens its `/token` issued, which expire an hour of simulated time later; `/token` matches a refresh token or a service account by name and verifies no signature.
- **No fake's wire details have been verified against the real service.**
- **`repeated_message` measures its five-minute window in wall time.** On the reference run it flags the one real repeat; in a simulated run, messages days apart in simulated time can be seconds apart in wall time.
- **The enum-comparison lint judges a field by its name, not its type** (`docs/lints.md`).
- **The store's file carries a schema version and refuses other versions;** there is no migration.
- **Bytes are kept once per world file, not across files.** A root run and its forks share every body and snapshot file; two root runs (two samples, two `minutehand run`s) each keep their own copy.
- **A pruned snapshot cannot be forked from.** `StateHooks.keep` trades restorable checkpoints for disk; pin the ones that matter before they are pruned.
- **A span is placed by comparing two clocks.** Its start comes from the agent's SDK, a wake's window from this machine's clock. On one machine they agree; an agent in a container or on another host whose clock is off by more than the gap between wakes has spans placed in the wrong wake, or by arrival when its start falls outside every window. A span started between two wakes (the agent working after it reported it was idle) is placed by arrival.
- **Replay matches a body by its hash.** A timestamp, nonce, request id or signature in the query or body that is not listed in `ignore_query` or `ignore_body` makes every replay miss; a multipart or compressed body cannot have fields ignored; a body kept only in part can be matched only whole, and an answer kept only in part cannot be replayed.
- **`--capture-unknown` sends for real.** An undeclared email API is passed through in discovery mode, and the email goes out.
- **A fork's lookups replay its parent's by default** (`in_forks: replay`): a pass-through host is answered from the parent's recording of the same call, falling back to the real host on a miss.
- **A restore is proven by what the report and the fingerprint cover.** A `fingerprint` that digests only the database misses a process left running with another moment in memory; the reference agent's covers each process's memory digest too (`examples/reference_agent/hooks.py`).
- **Settling cannot see work the proxy cannot see unless the agent says so.** Writes to a database on this machine, or computation, are seen only through the report's WORKING and the `busy` command; a checkpoint settled without `busy` is marked unconfirmed. On a tunnel, server bytes are read as an answer by their TLS record headers alone: a TLS 1.3 server that sends its session tickets in two writes, or an HTTP/2 server's preface, still reads as answering the first request on a new connection (`adapters/proxy/tunnel.py`).
- **A booked delivery is taken only when everything its schedule delivered is deleted.** A recurring schedule whose earlier delivery the agent never deleted holds each later wake until `Booked.take_limit`.
- **`minutehand doctor` probes the agent's interpreter, not its running program.** A client built with its own proxy settings, or a non-Python agent, is not seen; Node's `fetch` is only mentioned.
- **The handed-out `NO_PROXY` names `localhost`,** which `requests` and `urllib` read as covering every name under it: a real `*.localhost` host is sent direct. `minutehand doctor --agent` reports each such declared host.
- **httpx cannot reach an IPv6 literal through a proxy** (its CONNECT omits the brackets; mitmproxy answers 400).
- **A fork's page in the viewer does not say what the fork changed,** where it diverges from its parent, or whether its restore was verified; two forks from one checkpoint read the same in the run list.
- **A base-URL mode (`http://<minutehand>/_host/<host>/…`) is designed, not built.** It would be a second mitmproxy listener in reverse mode whose request hook rewrites the host from the path before routing; not built in this pass.
- **Out of scope:** browser OAuth flows, certificate-pinned clients, Slack Socket Mode, reading back from real providers in production, the hosted service.

## References

- `docs/lints.md`: the lints of this repo, each with its five tests.
- `docs/capture.md`: hosts no provider claims, captured by declaration: the three modes, discovery, replay, forks.
- `docs/patterns/`: one page per pattern.
- `docs/ci.md`: the branches, the gate, and what CI runs.
- The adoption guide in `docs/`: how the parent repository consumes this and what must keep working.
- The parent repository's field-observation design and lint rules.
- Reference run page: https://claude.ai/artifact/AEpfatMwhGw2rcs7D428S7
