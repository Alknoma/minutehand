# Minutehand

Reconciled with the code on `integration-main` (`9d86c6c`), 2026-10-04. Every code block below is quoted from `src/minutehand/` as it stands. Each part says whether it is built and tested, built with a known limit, or designed and not built. Results of one-off spike scripts, which are not in this repo, are kept only in "Evidence".

## Abstract

Minutehand is how a team builds its own proactive agent and finds out whether it works. It runs the agent through simulated days of work against fake Slack, trackers and document stores, with people who answer late or not at all, and then measures how well the agent carried the work.

Two pillars, in this order:

1. **Evaluating proactive effectiveness.** What the agent did and when, against what the world did and when, judged by the rules the team writes for its own agent. Minutehand states the facts; whether a follow-up was late, a reminder one too many, or an answer acknowledged too slowly is the team's policy, written in YAML (`docs/assessments.md`). A rule may name the design that fixes what it finds.
2. **Rewind.** Any run can be restarted from any moment with the prompt, the model, the people or the world changed, and played forward again. Minutehand owns the agent's time and its memory: the agent keeps what it remembers through one import (`minutehand.agent.store`), and a fork starts from that memory as it stood at the moment, with nothing else asked of the agent.

It is one process: it intercepts the agent's outbound API calls, owns the clock, plays the people and records every change. A coding agent reaches it over MCP, so "simulate it and fix what it finds" is a loop that needs no person in it.

## What exists

Tests are `def test_` functions counted per directory; `uv run pytest -q -n auto` runs every one that needs neither a package index nor Docker, with sockets disabled except to `127.0.0.1`, `::1` and `localhost`. The rest are marked `packaging`, `docker`, `benchmark` or `recipes`.

| Part | What it does | State | Tests | Known limits |
|---|---|---|---|---|
| Contracts: `domain/`, `ports/` | The models and protocols every other part is written against | Built and tested | 16 (`tests/test_scenario.py`, `tests/test_clock.py`) | No check declares `Needs.COMMITMENTS`. Only the agent file carries a `version`. |
| World store: `adapters/store/sqlite.py` | Append-only log of events, entity versions, calls, replies and the agent's spans; every body of 512 bytes or more and every snapshot file kept once, by SHA-256; forks share it; a refused fork is discarded with the bytes only it held; `minutehand runs`, `checkpoints`, `pin`, `gc` | Built and tested | 48 (`tests/test_sqlite_store.py`, `tests/test_store_spans.py`, `tests/test_store_bodies.py`, `tests/test_store_snapshots.py`, `tests/test_housekeeping.py`) | The file carries schema version 6 in `user_version` and refuses any other. Bytes are shared within one world file (a root run and its forks), not across root runs. No catalog of runs. |
| External emulators: `domain/emulator.py`, `adapters/emulator/`, `application/emulators.py` | A host declared `forward` is sent to a fake outside Minutehand (any language, a process or a container), through a loopback relay to its TCP port, Unix socket or HTTPS server; started or attached to, waited on (HTTP, TCP or log readiness) and health-checked; its answers told apart (`Exchange.outcome`); its failure answered 502/504 naming it and stopping the run `ENVIRONMENT_FAILED`, exit 2; its health changes in the log and the export; its OTLP received and joined by the forwarded `traceparent`; one per `serve` shared by worlds. See `docs/external-emulators.md` | Built and tested | 14 functions, 20 cases (`tests/emulators/` 12, `tests/serve/test_emulator_worlds.py` 2), and 1 marked `docker` (stripe-mock) | Its state is neither scored nor rewound: a fork after its first use is refused. Never restarted. Not seeded from the scenario, and people cannot act on it. |
| Proxy: `adapters/proxy/` | mitmproxy embedded in the process: answers claimed hosts, tunnels, edits or records model APIs, captures declared outbound hosts, refuses the rest; hands the agent one CA bundle (public roots plus its own CA); remembers the agent's latest call for settling; takes connections a container redirects to it; sends a claimed host's gRPC calls and WebSocket upgrades to a server of its provider's on a loopback port (`adapters/proxy/local.py`) and records each call and each message | Built and tested | 82 (`tests/proxy/`), and 1 marked `docker` (`tests/providers/slack/test_slack_redirected_container.py`), and gRPC and WebSockets through the providers that serve them (`test_cloud_tasks_grpc.py`, `test_slack_socket_mode.py`) | One proxy per process. A client that ignores proxy settings is given a base URL instead (`/_host/<host>`, below), or, in a Linux container, captured by a redirect to a second listener ("Transparent capture"); otherwise a call it made that the agent's telemetry names fails the run (`around_proxy`). Model API calls are recorded only with `--record-model-calls`, as spans. A tunnel is relayed as bytes, never decrypted: a request on it is seen and held until a TLS record comes back that is not of a TLS 1.3 server's session tickets, judged record by record from their headers (`adapters/proxy/tunnel.py`). Unary gRPC methods only, and gRPC needs `minutehand[grpc]`. A WebSocket connection is routed to its world only under `minutehand run`. |
| Outbound capture: `domain/outbound.py`, `adapters/proxy/capture.py`, `application/outbound.py` | Hosts that are not places the agent keeps state, declared per agent or per standing world: `acknowledge` (answered here, never sent), `pass_through` (sent on, both sides kept), `replay` (answered from a run's recording); `--capture-unknown` for a first run; a send read as a message to a person. See `docs/capture.md` | Built and tested | 41 (`tests/capture/` 28, `tests/checks/test_captured_messages.py` 5, `tests/e2e/test_capture_run.py` 3, `tests/e2e/test_example_capture.py` 1, `tests/serve/test_capture_worlds.py` 3, `tests/model/` 1) | People answer a captured send only when its declaration says how (`replies`); the standing mode's `act` cannot answer through one. Replay matches a body by its hash, so an unlisted timestamp or nonce misses. Discovery mode sends for real. |
| Slack provider | 19 Web API methods, `response_url`, `url_private`; every Events API shape its production caller handles, plus edits, deletes, reactions and joins; buttons, person pickers, modals and slash commands pushed as interactivity payloads and the agent's answers applied; Socket Mode (`apps.connections.open`, events as `events_api` envelopes on the agent's WebSocket, acknowledged or sent again); seeded channels, history, threads, files, guests, bots and deactivated accounts; scenario-declared faults including `ratelimited` with `Retry-After` | Built and tested | 124 (`tests/providers/slack/`), most through the real proxy with stock `slack_sdk`, Socket Mode with its `SocketModeClient`; and 1 whole run on Socket Mode (`tests/e2e/test_whole_run.py`) | Workspaces are per world (`SlackSeed.workspaces`, see its `README.md`); with none declared, one workspace in which any `xoxb-` or `xoxp-` token acts as the bot. `X-Slack-Request-Timestamp` is real time while `ts` and `event_time` are simulated. Over Socket Mode only `events_api` envelopes: a press or a slash command to a socket-mode agent is refused. See "Slack, against its production caller". |
| Asana provider | 54 routes over users, teams, workspaces, projects and their members, sections, custom fields and their settings, tags, tasks, subtasks and stories, and `/-/oauth_token`; a scenario's Asana seed; a declared status source; people acting on seeded tasks | Built and tested | 102 (`tests/providers/asana/`) | With no seeded token, any bearer token acts as the agent. A bare `custom_fields` in `opt_fields` answers each field's compact record with its value (`enum_value`, `display_value`, ...), as Asana's custom fields guide shows, while every other bare nested field answers its gid and resource type. Webhooks answer 501. No system stories (assigned, moved, completed) are written. |
| YouTrack provider | 45 REST routes, each at `/api` and `/youtrack/api`, and 9 Hub routes at `/hub/api/rest`: issues, custom fields of every single-valued type with per-project bundles, defaults and required flags, comments, tags, links, activities read from the log, projects and their fields, the instance's fields and bundles, users, commands, `issuesGetter/count`, Hub projects, groups, permissions and OAuth tokens; the query language every shape a production client builds; people acting on seeded issues (`ActsOnTickets`) | Built and tested | 119 (`tests/providers/youtrack/`, 221 cases with parameters; most through the proxy) | Only a seeded `*.youtrack.cloud` or `*.myjetbrains.com` host is reached: a self-hosted instance's own host cannot be declared. Multi-valued fields (`enum[*]`, `user[*]`, `version[*]`), text fields, work items, attachments, saved searches, agile boards and sprints as boards are not served. Wording of 403, 429 and several 400s, the activity item `$type`s for tags, links and summary, and the default issue order are not verified against the real service. |
| Google Workspace provider: `adapters/providers/google_workspace/` | Drive v3 (files incl. multipart, media and resumable uploads, export, copy, permissions, comments, about, drives, changes, channels), Docs v1 `documents.get|create|batchUpdate`, Slides v1 `presentations.get|create|batchUpdate`, Gmail v1 (`messages.send|list|get|modify`, `threads.get|list`, `labels.list`, `getProfile`, `history.list`; search operators), Calendar v3 (`events.insert|get|list|patch|update|delete` with guests and sync tokens, `events.watch` and `channels.stop`, `freeBusy.query`, `calendarList.list`), Google's `/token` and `/revoke`, OAuth2 v2 `userinfo`, the `iamcredentials` boundary lookup; per-user My Drives, mailboxes and primary calendars, shared drives; seeded emails and events; a person's reply to the agent's email landing in its mailbox, and a guest's Yes, Maybe or No on its invitation, at their moment (`LandsReplies`); a person's change to a document at its moment, pushed to a `changes.watch` address; a change to an event on a watched calendar, the agent's own or a guest's answer, pushed to an `events.watch` address, and every push recorded with how the address answered; declared faults; see its `CLAIMS.md` | Built and tested | 151 (`tests/providers/google_workspace/`; 23 run Google's own clients in a process of their own through the proxy) and 1 whole run (`tests/e2e/test_mail_and_calendar_run.py`) | A credential is matched by name, never by signature. `httplib2` reaches the proxy only when PySocks is installed beside it. No Sheets API (a Sheet is exported as CSV). Drive does not notify the agent of its own changes (Calendar does). Docs has no headers, footers, footnotes or suggestions, and images are never fetched. Content is capped at 5 MiB per file. Gmail pushes nothing (`users.watch`, which delivers through Pub/Sub, answers 501): an agent polls its mailbox. A push is never retried; Calendar's longest channel lifetime is the documented default ttl, a week, since Google documents no other. No drafts, user labels, attachments by id, recurring events or calendars beyond each account's primary. A reply landing in a standing world (`minutehand serve`) is refused, since only the run loop lands one. |
| Microsoft provider | Identity platform sign-in (client credentials, authorization code, refresh) issuing RS256 JWTs from a published key; the Bot Framework connector (10 routes) and its OpenID metadata; Graph `v1.0` for users, teams, channels, chats and messages, SharePoint and OneDrive files (sites, drives, items by id and path, children, 302 downloads, simple and session uploads, folders, move, copy, delete, invite, links, search, `delta`), subscriptions with the validation handshake; people's messages, edits, deletes, reactions, joins, installs (`PersonAddsAgent`) and card presses pushed with a Bot Framework JWT; people's file changes (`ChangesDocuments`) notified to subscriptions (`NotifiesChanges`); Outlook mail (`sendMail`, messages listed with the filters and orderings clients send, read, marked read, replied to, `delta`, subscriptions on a mailbox) and calendars (events, `calendarView`, `getSchedule`, answers), a sent email being the agent's ask threaded by `conversationId` and an invitation its ask of each attendee; people's replies by email into the mailbox they answer and their Accept, Tentative or Decline on an invitation at its moment; `MicrosoftSeed.mailbox`, `.emails`, `.events`, `.faults` and `.holds` | Built and tested | 111 (`tests/providers/microsoft/`), one a whole run of an agent emailing a person and booking her (`test_microsoft_outlook_whole_run.py`), through the real proxy with plain `httpx`, PyJWT and `python-docx` | Graph is gated by its own published OpenAPI description: 127 of the 2,103 operations kept for the claimed resources are served, every other one refused by name (501). Credentials are deliberately not enforced: any secret, client or token works. Token lifetimes are real time. Ids derive from the scenario's name. No comments on files and no record-shaped documents. No drafts, attachments or recurring events; an event is one item seen from every attendee's calendar; a person's email is only ever a reply. A production client's adapters were run against it once, outside this repo (see Evidence). See the provider's `README.md`. |
| Jira provider | Jira Cloud REST v3 and Agile 1.0 at `<site>.atlassian.net` and `api.atlassian.com/ex/jira/{cloudId}`: issues, transitions through per-project workflows and screens, comments in ADF, changelog, links, JQL search, projects, users, permissions by project role, boards and sprints; OAuth refresh at `auth.atlassian.com`; people's acts (`ActsOnTickets`); `JiraSeed.rate_limits` | Built and tested | 103 (`tests/providers/jira/`) | No webhooks, no authorization-code grant, no API v2. Link direction read from Atlassian's reference, not verified live. See its `README.md`. |
| Notion provider | Notion API `2022-06-28` at `api.notion.com`: pages, blocks, databases and their queries, search, users, comments, OAuth tokens; seeded workspaces, integrations and what is shared with them; people's changes (`ChangesDocuments`); integration webhooks (`NotifiesChanges`), verified and signed; `NotionSeed.faults` | Built and tested | 88 (`tests/providers/notion/`; most through the official SDK) | Webhook signing, verification and payloads are written from the reference, not verified live; events are not aggregated, delayed or retried. A person cannot move or share a page. See its `README.md`. |
| GitHub provider: `adapters/providers/github/` | REST at `api.github.com` (`/user`, `/user/repos`, a repository, its languages, branches, contents, blobs, trees and commits, code search with text matches, `/rate_limit`) and `/graphql` (`viewer`, `repository`); classic and fine-grained personal access tokens; primary rate limits counted per user and per address in the store; seeded faults (`rate_limited`, `secondary_rate_limited`, `server_error`); see its `README.md` and `CLAIMS.md` | Built and tested | 155 (`tests/providers/github/`) | Repository reads only: no issues, pull requests, webhooks, OAuth web flow or App tokens. A GraphQL query costs one point. Every branch and commit shows the head's files. No `ETag` or conditional requests. The seed comes beside the scenario (`GitHubProvider.seed_with`), not from it. |
| AWS provider | moto in the process; EventBridge Scheduler bookings become wakes delivered to SQS | Built and tested at the provider | 17 (`tests/providers/aws/`) | AWS's own state lives in moto's memory and cannot be rewound; each run's app takes a fresh AWS account, so a fork starts with none of its parent's queues. moto reads the machine clock for delays, visibility and timestamps. A target other than SQS raises when it fires. No whole run with a `Booked` agent is tested. |
| Google Cloud Tasks provider: `adapters/providers/google_cloud_tasks/` | Queues and HTTP tasks over the REST API and over gRPC, the client's default, from the same operations and world; a task's `scheduleTime` booked as a wake and delivered as an HTTP call to the agent's handler with Cloud Tasks' headers; a non-2xx answer retried with the queue's backoff until its attempts run out; a task's name taken for an hour after it ran or was deleted; queues seeded by `CloudTasksSeed`; see its `CLAIMS.md` | Built and tested | 20 (`tests/providers/google_cloud_tasks/` 16 through the real proxy with Google's own `google-cloud-tasks`, 11 on its REST transport and 5 on gRPC; `tests/e2e/test_cloud_tasks_run.py` 4 whole runs, one on gRPC) | gRPC needs `minutehand[grpc]` (grpcio and Google's messages); without it a gRPC call is answered UNIMPLEMENTED, saying so. Tasks asking for signed OIDC or OAuth tokens, App Engine tasks, `tasks:run` and batch calls answer 501. Only a task URL on this machine is called. Backoff stops growing after `maxDoublings` instead of growing linearly. |
| Run loop, fork, scripted people, agent drivers, files: `application/`, `adapters/agent/` | Plays a scenario on the run's clock, with people's acts on seeded tickets at their moments; forks a finished run from a checkpoint | Built and tested | 74 (`tests/orchestrator/`) | A fork starts only at a checkpoint after which the agent did not go on writing its memory in the same wake. `PromptPatch` and `ModelSwap` are tested through a whole run only on the reference agent's scratch measurements, not in the suite. A `PersonChange` withdraws a reply decided before the fork that had not landed by it, and asks again under the new behaviour. Only `Scripted` and `Silent` people: `Answers` is refused. |
| The agent's memory: `minutehand/agent/` (`store`, `wake`), `domain/memory.py`, `application/memory.py`, `application/restore.py`, the receiver's `/minutehand/agent` routes | What the agent remembers, as JSON under string keys, through one import inert in production; under Minutehand every write recorded in the run's log and every read answered from it, so each run and fork has its own memory and a fork starts from its parent's at the checkpoint with no hooks; the agent's next wake marked the same way; a fork's agent proven by its memory's digest and its report; a fresh empty SQLite file per run for each database of its own the agent file names | Built and tested | `tests/agent/` 21, `tests/e2e/test_memory_run.py` 4, `tests/orchestrator/test_rewind.py`, `tests/orchestrator/test_fork_start.py` 8 | Only state written through the store is part of a run: anything the agent keeps elsewhere is not simulated, not kept apart between runs and not rewound, and is seen only when its report shows it or the agent file names the database ("The agent's memory"). The package ships inside `minutehand`, whose own dependencies are heavy; it imports none of them. |
| Facts, assessments, ledger, scorecard, patterns: `checks/`, `domain/assessments.py` | The facts of a run (`checks/facts.py`), the team's YAML rules over them (`checks/assessments.py`), the checks that state the scenario's own words and the run's integrity (`expectations`, `near_miss_name`, `agent_contract_changed`, `around_proxy`, `unmatched_call`), the agent's own checks, the obligations ledger, `Effectiveness` (facts only), 10 patterns | Built and tested | `tests/checks/`, `tests/orchestrator/test_team_rules.py`, `tests/test_checks_on_reference_run.py`, `tests/e2e/test_around_proxy_run.py` | A rule counts; whether a message meant something is a judgement no rule makes. |
| Telemetry out: `adapters/telemetry/otel.py` | Spans, a log record per finding, metrics, over OTLP | Built and tested | 18 (`tests/telemetry/test_otel_telemetry.py`) | World-event spans are emitted when a wake ends, not as calls arrive. |
| Telemetry in: `adapters/telemetry/receiver.py`, `otlp.py`, `forward.py`, `application/model_calls.py` | Receives the agent's own OTLP during a run, keeps its spans with the run, passes it on to where it went before, joins a world event to the model call that led to it | Built and tested | 17 (`tests/telemetry/test_receiver.py`, `tests/test_model_call_join.py`, `tests/e2e/test_agent_telemetry.py`) | OTLP over HTTP and, with `minutehand[grpc]`, gRPC on the same port. Metrics are dropped; a log record is kept only when it carries GenAI content. A span is placed in a wake by comparing its SDK's clock with this machine's. |
| Session and CLI: `session.py`, `cli.py`, `doctor.py` | `minutehand run`, `findings`, `fork`, `runs`, `env`, `doctor` (which client libraries would go around the proxy); `--model-host` names a model API besides the three public ones; starts the agent's own command, or reaches one already running through a proxy on a fixed address | Built and tested | 19 (`tests/e2e/`) | Whole runs are tested with the Slack provider only, and with the agent as a local process: an agent in containers was run by hand once, not in the suite (`docs/containers.md`). Each sample has a memory of its own; what the agent keeps outside the store is carried from one sample into the next. |
| Standing mode: `serve.py`, `application/standing.py`, `adapters/control/`, `adapters/proxy/worlds.py`, `adapters/proxy/credentials.py`, `minutehand.testing` | `minutehand serve`: one process holding many worlds at once for a test suite; each call routed to the world that claims its host, a world key its URL names (`Manifest.world_keys`), or a credential (including a JSON or form token request's refresh token, code, client id and secret, and client assertion); a control API under `/v1` that also fires a happening, presses a control, declares a provider's own faults and switches, seeds more into an open world, changes people's accounts and permissions, deletes a ticket as a person, mints the credentials a pushed request carries, resets a world in place and shows its raw state; a pytest client and plugin; a composite action for another repository's CI; the image serves by default | Built and tested | 104 (`tests/serve/` 95, `tests/testing/` 3, `tests/e2e/test_cli.py` 2, `tests/test_scenario.py` 3, `tests/packaging/test_stack.py` 1) | Isolation is as fine as the credentials the services carry: one fixed token per stack means one world at a time. A base-URL call is routed as a proxied one. A call whose only claim is a `common` or `organizations` sign-in path, or a shared host with no credential, still reaches the default world only. A recorded call whose response body is binary is answered but not kept (see Known issues). Booked wakes are never fired. See `docs/serve.md`. |
| Reference agent: `examples/reference_agent/` | Two processes (an API on `requests`, a worker on `httpx`) over a job queue in its memory (`minutehand.agent.store`, SQLite in production), email by a captured channel with replies, a pass-through search, a model API on a local HTTPS server, OpenTelemetry over HTTP or gRPC, five behaviours; `docs/reference-agent.md` | Built and tested | 9 (`tests/architecture/`) | |
| Session and CLI: `session.py`, `cli.py` | `minutehand run`, `findings`, `fork`, `runs`, `env`; starts the agent's own command, or reaches one already running through a proxy on a fixed address | Built and tested | 19 (`tests/e2e/`) | Whole runs are tested with the Slack provider only, and with the agent as a local process: an agent in containers was run by hand once, not in the suite (`docs/containers.md`). Each sample has a memory of its own. |
| Lints: `lints/` | `wall_clock`, `import_boundaries`, `enum_string_comparisons`, `boundary_dicts` | Built and tested | 27 (`tests/lints/`) | The enum-comparison lint judges a field by its name, not its type. |
| Inboxes in the agent's own product: `domain/inboxes.py`, `application/inboxes.py`, `adapters/agent/inboxes.py`, `adapters/agent/openapi.py` | What waits on a person in the agent's own product (an approval, a question on its page), read and decided as that person; an item is an ask, a decision its answer; templates or OpenAPI operations; `docs/inboxes.md` | Built and tested | 41 (`tests/inboxes/` 33, `tests/architecture/test_approvals.py` 7, `tests/web/test_viewer_decisions.py` 1), and 2 driven in `tests/architecture/test_driven_approvals.py` | HTTP only; MCP is a designed second `kind`. An item raised and withdrawn between two readings is missed. |
| Scenario library: `src/minutehand/library/`, `domain/library.py`, `application/library.py` | Eleven ready-made scenarios with the team's goal, owner, person asked and answer as `{team.*}` placeholders; `minutehand scenarios`, `scenarios show`, `scenarios new`; see `docs/scenarios.md` | Built and tested | 22 functions, 113 cases (`tests/test_library.py` 18 functions, 51 cases; `tests/architecture/test_library.py` 4 functions, 62 cases, 60 of them one run each of a scenario against an example agent's behaviour) | Not graded and no pass mark. Conflicting answers are not in it. Three scenarios' main check never fires against either example agent. |
| The agent contract: `agent_api.py`, `schemas/`, `minutehand schema`, `minutehand validate` | One page of every touch point (`docs/agent-contract.md`), JSON Schemas of the files, an OpenAPI document of what an agent may implement, a file validated without a run | Built and tested | 5 functions, 7 cases (`tests/test_schemas.py`) | The older placeholders and dotted paths are listed as debt, not changed. |
| MCP tools: `adapters/mcp/`, `minutehand mcp` | Seven tools over stdio (`list_scenarios`, `run_scenario`, `list_findings`, `show_evidence`, `rerun_from`, `list_outbound_calls`, `list_runs`) reading and writing the state directory through `session`, as the command line does; see "What a coding agent calls" | Built and tested | 14 (`tests/mcp/`; one speaks to the installed command over stdio with the SDK's own client) | One run at a time per process: a second `run_scenario` or `rerun_from` while one plays is refused, not queued. |
| Run viewer: `adapters/web/`, `minutehand view` | A read-only JSON API over a state directory (runs and the fork tree, wakes, events, calls, obligations, findings, scorecard, messages, model calls and traffic, steps and spans) and the one page that draws it, its libraries vendored under `static/` | Built and tested | 20 (`tests/web/`) | Serves 127.0.0.1 only. Reads, never writes: a fork is taken from the command line or MCP, not the page. |
| Container image, model-written people, judged checks, generated providers, a faked system clock, hosted | See their sections | Designed, not built | 0 | |

No fake's wire details have been verified against the real service. The providers are tested against the services' own client libraries (`slack_sdk`, `asana`, `google-api-python-client`, `boto3`) and not against the services.

## Positioning

**Claim:** Build your proactive agent yourself.

**The objection it answers:** "A proactive agent is a product somebody sells me. If I build one, I get a chatbot with a cron job, and I will not know what it is missing until it embarrasses me in front of a colleague."

**The answer:** what separates the two is know-how, and Minutehand hands it over. It runs your agent through the situations a proactive agent has to survive, shows where yours breaks, and names the design that fixes each break.

What is sold is the know-how, in three forms:

| Form | What it is | Where it comes from |
|---|---|---|
| **Scenarios** | The situations: a person goes quiet, a person is away, a date moves, an approval is declined, a weekly task recurs, a deadline closes in. Eleven ship as a library a team fills with its own goal and people (`docs/scenarios.md`) | 21 field-observation cases from a production agent |
| **Rules** | What going wrong looks like in each, as the team defines it: rules in YAML over the facts of a run, which the library scenarios ship as a starting point and the team edits (`docs/assessments.md`) | The nine failure categories captured from that agent's runs, and the incidents behind each guard |
| **Patterns** | The design that stops it, with a working implementation to read | How that agent does it |

A finding carries its pattern (`Finding.pattern`, from the rule's `pattern`), so the coding agent that reads "followed up 33 hours late" is handed "give every wait an expiry and wake on it" in the same answer.

**Facts in the core, judgement with the team.** Minutehand holds no opinion of how an agent should behave. When to follow up and how often, whether to acknowledge an answer, when to escalate and to whom, what counts as nagging: two teams with good agents answer differently, and a team that wants an agent which reminds every hour may have one. The core records what happened and when (who was asked what, every follow-up, every answer, every write, every wake and the wakes the agent planned, what it reported) and states it. Nothing judges a run but what the team declared: its rules (`assess:`), the scenario's expectations (`expect:`) and protected names, and its own Python checks. Minutehand's library scenarios carry rules too, but as text written into the team's own file, theirs to change. The checks left in the core state the scenario's own words (`expectations`, `near_miss_name`) or whether the run can be trusted at all (`around_proxy`, `unmatched_call`, `agent_contract_changed`); none is about how an agent should behave.

What it gets wrong: a team that declares nothing gets no verdict, only facts (`Not assessed`), so a first run teaches less than it did when Minutehand judged by itself. The library's rules are the answer to that, and they are a suggestion a team must read.

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
5. **Facts and assessments.** The facts of a run read from `RunView` (`checks/facts.py`), and the team's own rules over them (`checks/assessments.py`); a few checks in the core state the scenario's own words and the run's integrity. Deterministic first; anything that needs judgement answers `FindingKind.REVIEW`.

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

An agent on Slack's Socket Mode names no URL: `{provider: slack, delivery: socket_mode}` takes its events on the WebSocket it opens itself ("gRPC and WebSockets"), and needs no `secret`. `secret` says where the secret that signs pushed events comes from: `generated` is made per run and handed to the command Minutehand starts in the variable `env`; `from_env` is the agent's own, for an agent already running, and Minutehand reads the same value from its own variable `env` (a run is refused when it is not set). A `Reported` source may also say how long a wake may take:

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

The e2e test agent (`tests/e2e/agents/slack_agent.py`, behaviour `forgetful`: asks once on stock `slack_sdk` and never comes back), a scenario where Sofia and the owner are silent, run with the command that `tests/e2e/test_cli.py` drives. The scenario's team asks for a follow-up within the hour of an answer falling due:

```yaml
assess:
  - id: follows_up_when_due
    each: ask
    where: {person_not: [owner]}
    when: {open_at: due}
    count: {follow_ups: {}, since: due, until: due+PT1H}
    at_least: 1
    message: "{person.key}'s answer was due and no follow-up came within the hour"
    pattern: expiry_on_every_wait
```

Abbreviated:

```
$ minutehand run scenario.yaml --agent agent.yaml --state state -- python slack_agent.py serve --port 8765 --state agent.json
run d799b57b7ad2: partner_pricing
  Failed: 2 checks failed; the run stopped because nothing more was due and the agent asked for no wake.
  stopped at 2026-09-07 10:00 UTC (simulated) because nothing more was due and the agent asked for no wake

fail (2)
  follows_up_when_due: sofia's answer was due and no follow-up came within the hour
    pattern expiry_on_every_wait: An expiry on every wait. Every wait carries an expected-by date and the agent wakes on it.
  expectations: owner asked mentioning ['confirmed']: wanted at least 1, found 0
    pattern honest_closure: Honest closure. Closing is decided from the state of the world, not from the agent's last message.

informational (1)
  expectations: sofia asked: met by the message to Sofia Romano (seq 17): "Could you confirm the partner pricing, please?"

scorecard
  expectations met: 1 of 2
  waits opened: 1, still open at the end: 1
  follow-ups made: 0
  wakes: 1, of which changed nothing: 0
  messages to people: 1
  failed checks: 2

checkpoints
  seq 14, after wake 0: restorable
  seq 18, after wake 1: restorable
  seq 19, after wake 1: restorable
exit 1
```

The agent stopped after one wake; the clock ran on to the deadline (seq 19 is that checkpoint), so the moment Sofia's answer fell due was reached and the rule read. Without `assess:` and `expect:` the same run says `Not assessed` and exits 5: the scorecard still shows one wait open and no follow-up made.

### The checks on a captured run

`tests/data/partner_pipeline/` is run `f431fc97f427`, converted to this package's models. `tests/test_checks_on_reference_run.py` asserts:

| Check | Result |
|---|---|
| `near_miss_name` | 4 failures, each `wrote "Aiven" where the scenario says "Ayven"`: two tickets and two messages. 0 once the name is corrected. |
| A rule `each: person`, `count: {messages: {to: [person]}}`, `gap_at_least: PT5M`, `severity: review` | 1 review, evidence `[3, 4]`, the two messages 18 seconds apart. The check this replaced also weighed the wording the two shared; a rule weighs only their spacing. |
| `expectations` | 1 failure: `ticket created for dania: wanted at least 1, found 0`. The legal-review ticket the goal depended on was never filed. |

### The loop a coding agent runs

Built, on the command line:

```
minutehand run scenario.yaml --agent agent.yaml -- <command>   -> run id, verdict, findings, scorecard, checkpoints; exit 0, 1, 2, 3, 4 or 5 by the verdict
minutehand findings <run_id>                                   -> the same report, read back from the state directory
minutehand run-all <folder> --agent agent.yaml -- <command>    -> every scenario in parallel; exit 1 when a verdict differs from its expect_outcome
minutehand fork <run_id> --at <seq> --changes fork.yaml -- <command>
minutehand runs
```

The same loop is served as MCP tools by `minutehand mcp` (see "What a coding agent calls").

## Architecture

### Components

```
src/minutehand/
  agent/              what an agent imports, standard library only: store.py (its memory), wake.py (its next wake),
                      _store.py (the adapters and the run's backend), _wire.py (HTTP to the run's receiver)
  domain/             pure: no I/O, no clock reads
    scenario.py       Model, Scenario, Person, Account, Answers, Scripted, ScriptedReply, ScriptedPress, FormInput,
                      Silent, DelayRange, WorkingHours, Absence, SeededTicket, SeededComment, SeededDocument,
                      DocumentKind, Access, SharedSpace, SignIn, SeededChannel, SeededPost, SeededFile,
                      ProviderSeed, Happening (TicketHappening (Moves, Reassigns, Comments, Deletes) |
                      DocumentHappening (Edited, Renamed, Moved, Shared, Trashed, Commented, FieldSet) |
                      MessagingHappening (PersonPosts, PersonEdits, PersonDeletes, PersonReacts, PersonJoins,
                      PersonAddsAgent, PersonOpensAgent, PersonCommands)), TicketFate, Direction, PersonAsked, TicketCreated, TicketDeleted,
                      TicketInState, Relayed, DocumentCreated, DocumentShared; `{{start+P2D}}` in any text (DATED)
    world.py          WorldEvent, Change, Stored, Exchange (CallOutcome, CallFailure), Captured, Body, RecordedCall, EntityRef,
                      TicketSnapshot, MessageSnapshot (with MessageAction), DocumentSnapshot, GrantSnapshot,
                      RecordSnapshot, InteractionSnapshot
    agent.py          WakeRequest, AgentReport, Commitment, AgentUnderTest, Reported, Booked, Polled, Command,
                      GoalByWake, GoalByMessage, HumanAction, Inbox, Marked, OwnDatabase
    outbound.py       Acknowledge, PassThrough, Replay, Forward, HostHeader, Answer, Route, MessageReading, InForks, OnMiss
    emulator.py       ExternalEmulator, Upstream, ReadyHttp, ReadyTcp, ReadyLog, Health, ErrorMarker, EmulatorHealth,
                      EmulatorChange, ADDED_HEADERS
    people.py         PersonReply, Press, PersonMessage, InboundTarget, PermissionGrant, InboundCredentialAsk,
                      InboundCredential
    provider.py       Manifest, Tier, TicketField, DocumentChange, PersonChange, WorldKey, world_keys(),
                      fault_fragment(), merged_seed()
    experiment.py     Fork, CallMatch, PromptPatch, ModelSwap, PersonChange, TicketEdit, DeadlineShift, DispatchChange
    model.py          Model: the base of every model
    assessments.py    Rule, Each, Anchor, Moment, Count and the facts it counts, Where, When, Judged, merged()
    checks.py         Finding, CheckReport, Pattern, Obligation, Stability, Effectiveness, PersonBurden,
                      WakeRecord, RunView, Check
    clock.py          Due, Jump, next_jump()
    run.py            RunRecord, StopReason, Verdict, VerdictKind, EmulatorUse, WakeLimit, wake_limit()
    errors.py         ServiceRefusal, Rendered, Asked: what leaves a provider's app
    memory.py         SeededMemory, StoreCall (MemoryGet, MemoryList, MemoryWrite), WakeMark: the agent's memory on the wire
    telemetry.py      ReceivedSpan, StoredSpan, Attribute and its value kinds, SpanSource, Signal, ForwardFailure
  ports/              Store, Clock, Provider, PushesEvents, PushesInteractions, HoldsTickets, EditsTickets,
                      ActsOnTickets, DeletesTickets, ChangesDocuments, NotifiesChanges, DeclaresFaults,
                      ChangesPeople, GrantsPermissions, MintsInboundCredentials, OwnsSeed, BooksWakes, Wakes,
                      AgentDriver, Reports, Replier, Telemetry
  application/        orchestrator.py (the run loop), checkpoint.py, rewind.py, restore.py (a fork's agent proven),
                      memory.py (the agent's memory in the log), replier_scripted.py, run_clock.py, files.py, refusals.py,
                      model_calls.py (the join), standing.py and further_seed.py (`minutehand serve`)
  checks/             facts.py (the facts, public), assessments.py (the team's rules read over them), ledger.py,
                      runner.py, effectiveness.py, patterns.py, and the checks of the scenario's words and the
                      run's integrity: expectations.py, near_miss_name.py, agent_contract_changed.py,
                      around_proxy.py, unmatched_call.py
  run_all.py          `minutehand run-all`: every scenario of a folder, each in a `minutehand run` of its own
  adapters/
    answering.py      the converter: what leaves a provider's app, as the answer the agent gets
    proxy/            server.py, addon.py, policy.py, registry.py, hosts.py, edit.py, redact.py, model_calls.py,
                      capture.py (outbound hosts: answering, keeping, reading a send, replay), worlds.py
    providers/<key>/  manifest.py, provider.py, app.py, wire.py, state.py, seed.py
    store/            sqlite.py
    agent/            reported.py, polled.py, command.py, reach.py
    telemetry/        otel.py (what Minutehand sends), receiver.py (also the agent's memory and next wake), otlp.py,
                      forward.py (what the agent sends)
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
    inboxes: list[HttpInbox] = []
    own_databases: list[OwnDatabase] = []
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

Nine protocols, because most services push nothing, hold no tickets and book nothing, and a method that returns nothing on their behalf would be a stub:

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
class LandsReplies(Protocol):
    def lands(self, reply: PersonReply, world: Store) -> bool: ...

    def heard(self, reply: PersonReply, world: Store, clock: Clock) -> bool: ...

    async def land(self, reply: PersonReply, world: Store, clock: Clock) -> None: ...


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

    async def deliver_booking(self, ref: str, world: Store, clock: Clock) -> None: ...
    async def advance_booking(self, ref: str, world: Store, clock: Clock) -> None: ...
```

Two more say what a provider's real service speaks besides HTTP answers, for what mitmproxy's app host cannot carry (it buffers an answer whole and sends no trailers): Minutehand serves each from a server of its own on a loopback port, one per world, and the proxy sends the calls there (see "gRPC and WebSockets"):

```python
@runtime_checkable
class ServesGrpc(Protocol):
    def grpc(self, world: Store, clock: Clock) -> Sequence[GrpcMethod]: ...


@runtime_checkable
class ServesSockets(Protocol):
    def sockets(self, world: Store, clock: Clock) -> ASGIApp: ...
```

| Provider | `Manifest.key` | Hosts (`path_prefix`) | Ports beyond `Provider` |
|---|---|---|---|
| Slack | `slack` | `slack.com`, `*.slack.com` (`files.slack.com`, `hooks.slack.com` and Socket Mode's `wss-primary.slack.com` included) | `PushesEvents`, `PushesInteractions`, `ServesSockets`, `DeclaresFaults` |
| Asana | `asana` | `app.asana.com` (`/api/1.0`, and `/-/oauth_token` outside it) | `HoldsTickets`, `EditsTickets`, `ActsOnTickets`, `DeclaresFaults` |
| YouTrack | `youtrack` | `*.youtrack.cloud`, `*.myjetbrains.com` (none; the app answers `/api`, `/youtrack/api` and Hub's `/hub/api/rest`) | `HoldsTickets`, `EditsTickets`, `ActsOnTickets`, `DeclaresFaults` |
| Google Workspace (Drive, Docs, Slides, Gmail, Calendar) | `google_workspace` | `www.googleapis.com` (Drive at `/drive/v3`, Calendar at `/calendar/v3`), `oauth2.googleapis.com`, `gmail.googleapis.com`, `docs.googleapis.com`, `slides.googleapis.com`, `iamcredentials.googleapis.com` | `LandsReplies`, `ChangesDocuments`, `NotifiesChanges`, `DeclaresFaults`, `OwnsSeed` |
| Microsoft | `microsoft` | `login.microsoftonline.com`, `login.botframework.com`, `smba.trafficmanager.net`, `graph.microsoft.com`, `*.sharepoint.com` | `PushesEvents`, `PushesInteractions`, `LandsReplies`, `ChangesDocuments`, `NotifiesChanges`, `DeclaresFaults` |
| AWS | `aws` | `*.amazonaws.com` | `BooksWakes` |
| Google Cloud Tasks | `google_cloud_tasks` | `cloudtasks.googleapis.com` | `BooksWakes`, `ConfirmsDelivery`, `ServesGrpc`, `OwnsSeed` |
| GitHub | `github` | `api.github.com` (REST and `/graphql`; no prefix) | none: repository reads only, see its `README.md` |
| Jira Cloud | `jira` | `*.atlassian.net`, `api.atlassian.com`, `auth.atlassian.com` (none; the app reads the site path and `/ex/jira/{cloudId}` itself) | `HoldsTickets`, `EditsTickets`, `ActsOnTickets`, `DeclaresFaults` |
| Notion | `notion` | `api.notion.com` | `ChangesDocuments`, `NotifiesChanges`, `DeclaresFaults` |

Every provider but GitHub is `Tier.FINISHED`. A person "replying" on a tracker is a `TicketFate`: `HoldsTickets.transition` moves the ticket as actor `PERSON`, and the agent finds it on its next read. A person replying by email, or answering a calendar invitation, is a reply like any other, decided by the replier when the agent's message reaches them, but it lands through `LandsReplies.land` (Google Workspace; Microsoft for a reply to an email or a meeting request, which `LandsReplies.lands` tells from a reply to a Teams message): in the agent's mailbox, threaded with what it answers, or as the guest's `responseStatus` (the invitation offers Yes, Maybe and No as controls, so a scripted `press` answers it) or response comment. Nothing is pushed to an inbound target, so the agent needs none for it. It wakes nobody unless the service itself tells the agent (`LandsReplies.heard`: a live Graph subscription on the mailbox or calendar it lands in, or a live Calendar `events.watch` channel on a calendar the event is on; Gmail never has one): otherwise the agent finds it on its next poll, and `Orchestrator._unheard` counts it with ticket fates. `session._services` holds each provider to the ports its manifest claims (`pushes_events`, `books_wakes`) and refuses a mismatch by name, and refuses a seeded ticket that sets a field its provider's `Manifest.ticket_fields` does not hold (`key`, `labels`, `comments`), naming the ticket: YouTrack and Jira hold all three (each one's own seed names a ticket by its `key`); Asana holds `labels` (as tags) and `comments` (as stories by their people), and has no meaning for `key`. `refusals.refuse_unheld` also refuses a document happening whose action is not in its provider's `Manifest.document_changes`, naming it: Drive shows every change but `field_set`; Notion `edited`, `renamed`, `trashed`, `commented` and `field_set`; Microsoft `edited`, `renamed`, `moved`, `shared` and `trashed`. `Manifest.world_keys` says which host label or path segment of a request names its world (a Microsoft tenant, an Atlassian site or cloud id, a YouTrack or SharePoint site), for `minutehand serve` (`docs/serve.md`).

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
  - {provider: google_workspace, title: Launch plan, text: "# Launch plan"}
happenings:
  - {kind: ticket, person: nadia, ticket: Book the venue, after: P1D, action: {kind: moves, to: done}}
  - {kind: ticket, person: nadia, ticket: Write the release notes, after: P2D,
     action: {kind: comments, text: Draft is in the shared folder}}
  - {kind: document, person: nadia, document: Launch plan, after: P3D, action: {kind: renamed, to: Launch plan (final)}}
  - {kind: posts, provider: slack, person: nadia, text: All three are done on my side, after: P4D}
```

That file plays in one run in `tests/providers/test_every_happening_family.py`; `tests/data/every_new_provider/scenario.yaml` plays a Jira ticket, a Notion page, a Microsoft file, a Teams post and a Teams card press in one run in `tests/providers/test_every_new_provider_happening.py`.

### Deliberate failures

A fault is typed by the provider that can produce it and declared in that provider's own seed (`ProviderSeed.body`); there is no shared `Scenario.faults`, because no one shape holds every provider's failures without carrying knobs the others would ignore: Slack's `SlackSeed.faults` (any Slack error code or `ratelimited` with `Retry-After`, every call or N, `only_rich`), Google Workspace's `WorkspaceSeed.faults` (N calls of a Drive, Docs, Slides, Gmail or Calendar operation answered one of seven `wire.FaultKind`s), YouTrack's `YouTrackSeed.faults` (a method and path glob answered any HTTP status over a window), Asana's `AsanaSeed.rate_limits` (throttled stretches), Jira's `JiraSeed.rate_limits` (N calls to a path answered 429), Notion's `NotionSeed.faults` (rate limits and edit conflicts, per integration), Microsoft's `MicrosoftSeed.faults` (a Graph or connector error code, or a rate limit) and `MicrosoftSeed.holds` (a seeded file held open by a person over a window, every write refused 423 `resourceLocked`), and GitHub's `GitHubSeed.faults` (rate limits, secondary limits, server errors) and `GitHubSeed.limits` (a repository's truncated-tree and directory-listing thresholds). Each of those providers is `DeclaresFaults`: the standing mode's `POST /v1/worlds/{id}/provider-faults` hands it a fragment of its own seed model that sets only these fields, and it records them as seeding does, counted from the world's now. The control API also keeps its own `faults` route, whose caller writes the status and body.

### What leaves a provider: three kinds, one converter at each boundary

After LocalStack's `ServiceException` and moto's, a provider's request handler lets out three kinds of exception (`domain/errors.py`), and one converter at the proxy (`adapters/answering.py`, wrapped around the app in `ProxyAddon._answer`) answers each:

| Raised | Answered | Logged | `Exchange.outcome` |
|---|---|---|---|
| a `ServiceRefusal` subclass (each provider's own refusal classes: Slack's, Asana's, Graph's, ...) | `render(asked)`: the bytes that provider's app answers it with | debug | `refused` |
| `NotImplementedError` | 501 in the vendor's error shape (`RendersErrors.error`), "minutehand's <provider> fake does not implement <METHOD> <path>" | info | `not_implemented` |
| anything else | 500 in the vendor's error shape, "minutehand internal error while answering <provider> <METHOD> <path>: <Type>: <message>"; never raised on into mitmproxy | error, with the traceback | `internal_error` |

```python
class ServiceRefusal(Exception, ABC):
    @abstractmethod
    def render(self, asked: Asked) -> Rendered: ...


class RendersErrors(Protocol):  # ports/provider.py; a provider without it is answered {"error", "message"}
    def error(self, status: int, code: str, message: str) -> Rendered: ...
```

The guard holds back what the app sends and, when the app raises, sends the converted answer instead: mitmproxy's `asgiapp.serve` would answer a bare 500 "ASGI Error.", and Starlette's `ServerErrorMiddleware` sends its own 500 before it re-raises. A provider that cannot be built is answered the same way, where it used to be "provider failed to load" and re-raised. Most refusals never leave an app (each provider catches its own and answers it, unchanged); a call no exception left is `refused` when its status is 400 or more or the provider noted it (Slack's `ok: false` at 200), `injected_fault` when a fault the scenario, a provider seed or the control API armed answered it (`answering.injected()`), else `answered`. The kind is trunk's one `CallOutcome`, which forwarded calls to an external emulator carry too. `Exchange.failure` (`CallFailure`: kind, message, exception type, traceback) is set for `not_implemented` and `internal_error`, and a run with an `internal_error` call answered by a provider in this process is `TOOL_FAILED`, exit 4; an external emulator's `internal_error` stays the review finding `application/emulators.py` raises, and one unavailable stays `ENVIRONMENT_FAILED`, exit 2.

Port methods the application calls (`seed`, `act`, `change`, `transition`, `deliver`, ...) still refuse with `ValueError` and `LookupError`; the standing world turns each into `WorldRefused` or `NotFound`. The control API's one converter (`_guarded`) maps only typed refusals to their statuses and anything else to 500 `internal_error`; the command line's (`cli.main`) keeps each known refusal's message and exit code, and answers anything else with one line naming it an internal error and the file its traceback was written to, exit 4 (`--debug` prints the traceback too).

What this gets wrong: a refusal a provider answers in its own app is recorded `refused` only when its status says so or the provider notes it, and only Slack notes one; a GraphQL error GitHub answers at 200 is `answered`. A refusal answered by the converter rather than the provider's app lacks what the app adds per call: GitHub's rate-limit headers, and Notion's request id, minted from the call rather than the store's head.

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
    only ticket fates, ticket or document happenings and unheard landed replies fired -> HoldsTickets.transition,
                     ActsOnTickets.act, ChangesDocuments.change, LandsReplies.land; no wake, unless the agent watches a changed provider's documents
                     (NotifiesChanges.watched), then a DUE wake in which NotifiesChanges.notify tells it
    otherwise, one wake:
      fire in order: fates (transition), replies (LandsReplies.land where the agent reads them, PushesEvents.deliver,
                     or PushesInteractions.press for a
                     reply that uses a control), happenings (ActsOnTickets.act for a ticket,
                     ChangesDocuments.change for a document, PushesEvents.happen for a message), directions by
                     message (say),
                     bookings (BooksWakes.deliver_booking, .advance_booking), the next Polled tick
      WakeRequest to each driver that must hear of it; AgentDriver.settled() waits until not WORKING
      read the new events: an agent message to a person, as its text reads when the wake ends
                           -> Replier.decide -> a pending reply
                           an agent ticket assigned to a person with a TicketFate -> a pending fate
      AgentReport.next_wake replaces the agent's previous DUE wake
      the last next wake the agent marked in the wake (`minutehand.agent.wake`) replaces its next wake
      checkpoint: a Checkpoint row in the log, carrying the agent's report and its memory's digest
      stop on DONE (AGENT_DONE), the wake limit (WAKE_LIMIT), AgentFailed (AGENT_FAILED)
  RunRecord -> Scorer (the team's rules and checks, the core's checks) -> Telemetry.found, Telemetry.run_ended
```

- A person answers a message as it reads when the wake ends. A placeholder the agent edits into its question within the wake is never put to anyone; the question is, once. An edit in a later wake that changes the text is put to the person again unless they have already answered that message, and a reply to the old text still on its way is withdrawn. The withdrawn reply stays in the `reply` table and its position is kept in every later `Checkpoint.withdrawn`: the ledger reads it as the replier's decision that the message asked something, and never as an answer, so a wait whose only reply was withdrawn stays open (`test_a_reply_withdrawn_by_an_edit_that_gets_no_answer_settles_no_wait`). Before, the ledger read the latest reply to a message, and an edit that got no answer was settled by the withdrawn one, and then `slow_to_react` failed the agent for never acting on an answer that never arrived.
- A `Reported` wake is asked for its report after `report_first_after`, then at doubling intervals up to `report_at_most_every`; one still WORKING after `working_limit` stops the run as `AGENT_FAILED`, and `RunRecord.failure` says which limit it hit.
- When one jump fires several things, the wake carries the reason that matters most: `PERSON_REPLIED`, then `DIRECTION`, `DUE`, `TICK`.
- A wake made only of bookings sends no `WakeRequest`: the scheduler's delivery is the wake. The loop still polls the agent's main driver (if it has one) until it is not `WORKING`, adopts its report and counts what it wrote in that wake. It cannot see whether the agent's own poll of its queue has picked the delivery up yet: an agent that answers `IDLE` before it has is moved on past it.
- A wake whose only news is a pushed event, to an agent with no wake endpoint, sends nothing either: the push is the wake.
- Every `Polled` tick is a wake and counts toward the wake limit (`domain/run.wake_limit`). The scenario's `max_wakes` sets it. Without one, a scenario with a deadline and an agent with a rhythm of its own (a polled wake's `every`, or the agent file's `tick` for an agent that reports or books its next wake) gets every tick up to the deadline and 20 more, for the wakes replies and happenings bring; anything else gets 20. `RunRecord.wake_limit` keeps the number and where it came from, and a run stopped there says both and how to raise it. What it gets wrong: an agent that reports hourly wakes and declares no `tick` is held to 20 wakes, less than a day; the message says to declare it.
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
<state>/runs/<run_id>/result.json    `RunResult`: findings, blocked checks, notes, the scorecard, what judged it
                                     (`assessed_by`)
<state>/runs/<run_id>/scenario.json  the scenario as this run played it (a fork's, with its changes)
<state>/runs/<run_id>/agent.json     `AgentUnderTest`
<state>/runs/<run_id>/agent.log      what the agent's own process printed, when Minutehand started it
<state>/runs/<run_id>/captured.jsonl every call the run captured to a host no provider claims, redacted, for
                                     a later replay (`docs/capture.md`)
<state>/runs/<run_id>/own/           a fresh empty SQLite file per variable the agent file names under
                                     `own_databases`, outside forks
<state>/runs/<run_id>/restore.json   for a fork: how its agent was found at its start (its memory, its report)
```

`world.db` holds ten tables: `run` (each run and the seq, call count and wake it was forked at), `event`, `entity_version`, `exchange`, `reply`, `span` (the agent's spans, see "Telemetry"), `wake_edge` (the real moment each wake began and ended), `forward_failure`, and two that keep bytes once: `content` (stored bodies) and `span_body` (which span attribute values are stored bodies). The agent's memory is entity versions like any other.

- `entity_version` is the world: append-only, one row per change, read "as of" a sequence number. Providers page through it with `Store.children`; nothing is held in process memory between requests.
- `event` and `exchange` are append-only too. A call that produced no event is recorded with `first_seq > last_seq`.
- `reply` stores every person's reply the first time it is decided. A fork copies the parent's replies up to its checkpoint, so a rerun asks no one again.
- One file per root run isolates parallel runs. A fork lives in its root's file.
- One `sqlite3` connection per store, shared across threads behind one lock: a provider served from a worker thread writes through it.
- `span` is append-only and keyed like `exchange`: each row carries the wake it is placed in, the wake it arrived in and `after_seq`, the head of the log when it arrived. A fork sees its parent's rows placed in wakes up to the one whose checkpoint it was forked at (`run.forked_wake`), however late they arrived.
- `SCHEMA_VERSION = 8` is stamped into `user_version`; a file with tables and another version is refused, not guessed at. Changes: 5 let an exchange carry `captured` and a message `answerable`; 6 moved every body of 512 bytes or more into `content` and the agent's snapshots into manifests; 7 kept a body that is not UTF-8 as its bytes; 8 dropped the snapshots and their pool with the state hooks that wrote them. An older file is refused; there is no migration.

#### Bytes kept once

The rule: what goes in is what comes out, and the record is exactly that. Every body is kept verbatim (after redaction, which applies to the stored copy only); nothing is dropped or summarised to save space. Space is saved only by not storing the same bytes twice.

- **Bodies.** Every body passes one door, `SqliteStore._keep`: an entity version's body, an event's snapshot (as its JSON), an exchange's request and response bodies, and each string value of a span attribute. Under `INLINE_LIMIT` (512) bytes of UTF-8 it stays in its row. At 512 or more it goes into `content` under the SHA-256 of its UTF-8 bytes, once per file whoever wrote it, compressed with zstd (level 3) when that is smaller, and the row holds the 32-byte hash. The hash is over the uncompressed bytes. Every reader (`get`, `children`, `versions`, `events`, `calls`, `spans`, and through them the viewer, the MCP tools and the control API) gets back the exact text written: `test_an_awkward_body_reads_back_identical_from_every_reader` writes an empty body, odd JSON spacing, all 256 byte values as the proxy decodes a binary body, bodies one under, at and one over the limit (in one- and two-byte characters), NULs, and 2 MB, through every reader.
- **Where the bytes live.** A body in a table of the same file, not in files beside it: one file to copy, and the body commits in the same transaction as the row that refers to it, so a sweep in another connection never sees it unreferenced in between. The largest body the proxy keeps is bounded (`body_limit`, 1 MiB by default for a captured host; a provider's own limits, 5 MiB for a Drive file), so SQLite's per-value limit is never near.
- **The threshold**, measured on run A below: a body under 512 bytes compresses to about its own size (49,577 bytes of 712 distinct bodies under 256 became 52,542), and a stored body costs about 110 bytes of hash, index entry and row header, so moving it saves nothing unless it repeats; between 512 and 1,024 bytes zstd halves a body (564,278 → 303,954 bytes over 1,000 bodies), so even one that never repeats is smaller stored.
- **Compression.** zstd rather than zlib: within 6% of zlib's size on small JSON and three times faster (2.6 ms against 8.0 ms over those 1,000 bodies); `zstandard` was already installed under mitmproxy and is now a direct dependency.
- **Redaction comes first.** The proxy redacts before it hands a body to the store, so a secret never reaches `content` and two bodies that differ only in a redacted value are one row (`test_bodies_that_differ_only_in_a_redacted_secret_are_stored_once_and_no_secret_is_kept`). The stored-bytes searches read every stored body and pooled file decompressed (`tests/support/stored.py`), since a compressed secret would not appear in the raw bytes.
- **Forks** share their parent's bodies: a body is per file, not per run, and a fork lives in its root's file.
- **Discarding** a run deletes its rows and every body no remaining row in any run refers to, in one transaction (a sweep over the five reference columns, not a reference count, so no write path can get a count wrong): a process killed halfway rolls back and every row still reads its body (`test_a_process_killed_while_discarding_deletes_no_body_a_row_still_refers_to`).
- **The write-ahead log** is cut back to 1 MiB after each checkpoint (`journal_size_limit`) and to nothing when a store closes (`wal_checkpoint(TRUNCATE)`, which also runs when a `play` finishes, a fork ends, and a standing world closes).

#### Measured: before and after

`uv run pytest -q -m benchmark tests/benchmarks -s` (`tests/benchmarks/test_storage_size.py`) builds three runs through the real proxy and store: **A** 5,000 calls to Slack, 4,500 of them `users.list` answering the same 20,044-byte listing and 500 `chat.postMessage`; **B** 50 uploads to Drive of a 4 MiB text file with 10 distinct contents, each read back 5 times; **C** (as measured then) 200 wakes, each ending in a checkpoint whose snapshot command copied a 30 MB directory of 300 files of random bytes, 3 of which changed per wake; snapshots are gone, and the benchmark's run C now writes an agent's memory instead, not measured here. Before is `integration-main` at `465507a`; the runs were interleaved, three of each, on a shared machine with a load average of 13 to 17, and the medians are shown. On disk is the world file, its log and the snapshots once the store has closed.

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

`minutehand runs` shows each run's size on disk in two parts that do not overlap: its rows and the stored bodies only it refers to. `minutehand checkpoints <run>` lists each checkpoint and whether a fork can start at it. `minutehand gc` sweeps every world file under the state directory of stored bodies nothing refers to and prints what it freed. A standing server's retention of closed worlds is the same function (`session.collect`): it removes the directories of worlds beyond `keep`, then sweeps the rest. `minutehand rm <run>...` removes runs with every fork of each.

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
      NO_PROXY: localhost,minutehand
      SSL_CERT_FILE: /ca/minutehand-ca-bundle.pem          # httpx, requests, slack_sdk
      REQUESTS_CA_BUNDLE: /ca/minutehand-ca-bundle.pem
      HTTPLIB2_CA_CERTS: /ca/minutehand-ca-bundle.pem      # googleapiclient
      NODE_EXTRA_CA_CERTS: /ca/minutehand-ca-bundle.pem
    volumes: ["minutehand-ca:/ca:ro"]
```

Three limits of the design:
- **The agent's own database is not a SaaS fake.** What the agent remembers goes through `minutehand.agent.store` to the run; a database it keeps beside that (Firebase's emulator, a PostgreSQL) stays a container of the team's.
- **The agent's container must trust the CA.** One environment variable per HTTP library, as above, each naming the bundle: certifi's public roots and then the proxy's CA. A file holding the proxy's CA alone replaces a library's roots, and every call the proxy tunnels to a real host, a model API, fails verification.
- **A client that ignores proxy settings** is given a base URL instead (`AgentUnderTest.base_urls`, below), which is a configuration change in the agent, or, in a Linux container, captured with no change at all ("Transparent capture", below). Node's built-in `fetch` reads the proxy on Node 24 and later once `NODE_USE_ENV_PROXY=1`, which every agent is handed.

### Base-URL mode

Built and tested (`adapters/proxy/base_url.py`; `tests/proxy/test_base_url.py`, one `test_*_base_url.py` per provider
that answers with URLs, `tests/serve/test_base_url.py`). Some clients cannot be given a proxy but can be given a
base URL. The proxy's own listener also answers `http://<minutehand>/_host/<real-host>[:<port>]/<path>`, and the
same over HTTPS on a certificate its CA mints for the name the client used. Before any other hook reads it, the
request is rewritten into the call it stands for (`https://<real-host>/<path>`, the real host in `Host`), so the
provider's answer, the capture declarations, the record (with the real host's name) and a standing world's routing
are those of a proxied call. A request is taken for one when it is not inside a `CONNECT`, its path starts with
`/_host/`, and its own host is one nothing routes.

The agent file names the hosts and the variable each base URL goes in; `minutehand env` prints them and an agent
Minutehand starts is handed them:

```yaml
base_urls:
  - {host: api.github.com, env: GITHUB_API_URL}
  - {host: app.asana.com, env: ASANA_BASE_URL, path: /api/1.0}   # http://127.0.0.1:8080/_host/app.asana.com/api/1.0
```

What the client is answered is rewritten for it: every absolute URL in `Location`, `Content-Location` and `Link`,
and in a text body the proxy answered itself, whose host a provider claims and whose path is under that provider's
`path_prefix` (or, for a host no provider claims, the call's own host), is put in the same form, so following it
comes back through the proxy. The record keeps the answer as the provider wrote it. What each provider emits:

| Provider | URLs rewritten | Test |
|---|---|---|
| GitHub | `Link` pages | `tests/providers/github/test_github_base_url.py` |
| Jira | every `self` | `tests/providers/jira/test_jira_base_url.py` |
| Microsoft | `@odata.nextLink`, `@odata.deltaLink`, the content 302's `Location`, `@microsoft.graph.downloadUrl`, an upload session's `uploadUrl` | `tests/providers/microsoft/test_microsoft_base_url.py` |
| Google Drive | a resumable upload's session `Location` | `tests/providers/google_workspace/test_drive_base_url.py` |
| Slack | a file's `url_private` and `url_private_download`, the sign-in redirect | `tests/providers/slack/test_slack_base_url.py` |
| Asana | `next_page.uri`; a `permalink_url` (outside `/api/1.0`) is left alone | `tests/providers/asana/test_asana_base_url.py` |
| AWS | SQS's `QueueUrl` | `tests/providers/aws/test_aws_base_url.py` |
| Notion, YouTrack | none: Notion's URLs are on `www.notion.so`, which no provider claims; YouTrack writes none | |

### Transparent capture

Built and tested for an agent in a Linux container (`adapters/proxy/redirected.py`; `tests/proxy/test_redirected.py`,
`tests/providers/slack/test_slack_redirected_container.py` marked `docker`, CI job `transparent`; `docs/containers.md`).
`--transparent-port` opens a second mitmproxy listener whose connections start with the `Redirected` layer: it holds
what the client sends until the TLS ClientHello's server name (port 443) or the plain request's `Host` (its port, else
80) names the host, sets that as the destination, and hands the connection to the next layer as the inside of a
`CONNECT`, so routing, answering, tunnelling and recording are unchanged. A connection that names no host is closed.
`minutehand env --format redirect` prints the `iptables` DNAT rules the agent's container runs as root; the Compose
override mounts them and adds `NET_ADMIN`. mitmproxy's own transparent mode reads the original destination from the
kernel (`SO_ORIGINAL_DST`), which knows it only in the namespace that rewrote the connection, so it cannot serve a rule
in the agent's container. Not built: the same for the `Contained` sandbox (a `PREROUTING` rule on the sandbox's link),
IPv6, ports other than 80 and 443.

When nothing captures such a client, the run says so (`application/around_proxy.py`, `checks/around_proxy.py`;
`tests/checks/test_around_proxy.py`, `tests/e2e/test_around_proxy_run.py`). An HTTP client span the agent exported for
a host a provider claims is matched to the proxy's record of it by the `traceparent` the call carried (the client
span's own id); spans left unmatched are counted against the host's records that carried no traceparent, or one naming
a span the agent never exported, and any left over fail the run. An agent that was woken and called none of the
providers the run names is a `review` finding. Rejected: steering name resolution for the agent alone
(`HOSTALIASES`, `LD_PRELOAD`), which a static Go binary and macOS without root ignore, and polling the agent's sockets,
which misses a short call.

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

- **Driven from outside.** A harness that drives the agent itself marks steps (`application/steps.py`: recorded as the
  run loop records a wake's edges, or inferred from forward clock moves) and opens its worlds under one case label
  (`application/cases.py`: one run, one timeline renumbered by simulated time then record order, one set of people
  by email, its own store for steps, model traffic and spans that arrive with no trace link inside its real-time
  window). `docs/serve.md`, "Scoring a run you drive yourself"; `tests/e2e/test_driven_case.py`.
- **Messages.** Every message event carries its `MessageSnapshot`, a delete what the message said when it went
  (Slack and Teams); the scorecard counts rewrites in place and deletes (`messages_edited`, `messages_deleted`);
  the viewer lists every message under the timeline ("rewrote the message to Dani from '…' to '…'"), the model
  calls by step with the message each wrote, and one line per model host relayed on tunnels.

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
- **An unmapped resource is recorded as `RecordSnapshot(resource, text)`.** The AWS provider and the Drive provider's permissions and comments emit it today. `near_miss_name` reads it, needing only the text the agent wrote, and a rule counts it among `writes` (`things: [record]`), needing only that a write happened. `repeats_open_ticket` never holds of it, and it is no message: a record has no title, project or channel to compare.
- **Mapping is data.** Which resource is a ticket and which field is its title is a short mapping file per service, drafted by a coding agent and reviewed.
- **Prior art:** FetchSandbox generates a stateful sandbox from an OpenAPI document and is hosted; Prism and Microcks serve examples without state. Nango's provider catalogue (1,000+ APIs) is under the Elastic License and cannot be copied into this repo.
- **Unproven:** how much of a real service's behaviour create-read-update-delete over its description actually covers. This needs measuring on five services before the tier is promised.

### Hosts the proxy does not own

Built and tested (`tests/proxy/`). `Routing.policy(host)` decides by host alone:

| `HostPolicy` | When | What happens |
|---|---|---|
| `ANSWER` | A provider claims the host | Its app answers with the manifest's `path_prefix` stripped; the call is recorded. A provider that fails to load answers 500; the call never reaches the real host. A gRPC call, and a WebSocket upgrade to a provider that serves sockets, go to the provider's own loopback server instead (below). |
| `TUNNEL` | A model host (`DEFAULT_MODEL_HOSTS`: `api.openai.com`, `api.anthropic.com`, `generativelanguage.googleapis.com`) with no edit for this run, or a model vendor's own infrastructure (`MODEL_INFRASTRUCTURE_HOSTS`: `mcp-proxy.anthropic.com`, Claude Code's relay to the account's remote MCP connectors; `*.mcp.claude.com`, Anthropic's own MCP servers; `platform.claude.com`, the sign-in's token refresh), which no edit ever applies to | Bytes pass through, never decrypted. That the call happened is recorded, never what it said: one `RecordedCall` per burst, its `Exchange.tunnelled` holding the port, the connection, the burst's number on it, when the connection opened, the burst began and ended and the connection closed (real clock), and the bytes each way; no body is kept. Under `serve` it is kept in the world that declared the host a model host, else in the lobby, where `GET /v1/unmatched` lists it |
| `EDIT` | A model host the run edits | Decrypted, edited, sent on with the upstream certificate verified; recorded as a span only with `--record-model-calls`. An edit that fails answers 502 rather than sending the request unedited. |
| `RECORD` | A model host, with no edit, in a run started with `--record-model-calls` | Decrypted, sent on unchanged with the upstream certificate verified, and kept as a span (`adapters/proxy/model_calls.py`) whose `minutehand.request.body` and `minutehand.response.body` hold both bodies byte for byte: UTF-8 text as a string, credentials redacted, anything else as its bytes; a streamed answer is passed to the agent chunk by chunk as it arrives |
| `REFUSE` | Anything else | Captured when the call's world declares the host outbound (below); else passed through and kept as `discovered` under `--capture-unknown` (every call) or `--capture-unknown reads` (a GET, HEAD or OPTIONS); else 502 and recorded with no provider, surfacing as an `unmatched_call` finding that says how to declare it |

A tunnelled call is recorded per burst, not per `CONNECT`: a client's pooled connection to its model API is opened
once and reused for every later call, across wakes, so a record per connection would put every call it ever carried
in the wake it opened in. A burst is what moved on the connection from the first byte after it opened, or after its
previous record was written, until the server had answered (`tunnel.Tunnel`) and nothing moved for `BURST_QUIET`
(0.5 s), or the connection closed, or the run ended (`Mounts.flush`), or under `serve` its world closed. It is
written then, stamped with the wake and simulated time in progress when its first byte moved (`Store.attach(began=…)`),
so a burst still answering when the wake ends stays in the wake it began in; it is listed after the calls recorded
while it was in progress. A burst of nothing but TLS alerts (a connection's `close_notify`) is no call and is
dropped. The run output's per-host summary counts the bursts, their connections and the bytes; the viewer lists
each beside the captured calls (`tests/proxy/test_tunnelled_calls.py`, `tests/serve/test_tunnelled_calls.py`).

A host left to `REFUSE` is decided per world, by the declarations of the world the call belongs to (`Mounted.capturing`; `docs/capture.md`):

| Declared | What happens |
|---|---|
| `acknowledge` | Never leaves the machine: answered with the declared status, headers and body (or a route's); kept as an `Exchange` carrying `Captured`; with a `message` reading, also a world event, a message from the agent to the person the body names |
| `pass_through` | Sent to the real host unchanged, upstream certificate verified; the answer reaches the agent chunk by chunk through the tee `RECORD` uses, and both sides are kept |
| `replay` | Answered from an earlier run's `captured.jsonl`, marked `x-minutehand-replayed`; a miss is passed through and kept, or refused with 502, as declared |

A declared host a provider claims, or a model host, is refused when the run or world is created, naming both.

A model host that overlaps a provider's claim is refused when `Routing` is built. The model-host list is a `Routing` argument; the CLI uses the default. Upstream connections open only when a request is forwarded (`connection_strategy="lazy"`) and the certificate shown to the client is minted, not copied (`upstream_cert=False`); `test_claimed_host_is_answered_without_contacting_it` and `test_unclaimed_host_is_refused_and_recorded_without_contacting_it` watch a listener receive no connection.

Capture of hosts no provider claims is built (above). Passing a host a provider claims through to the real service, to measure the provider against it, is not.

### gRPC and WebSockets

Built and tested (`tests/providers/google_cloud_tasks/test_cloud_tasks_grpc.py`, `tests/providers/slack/test_slack_socket_mode.py`, a whole run of each in `tests/e2e/`). mitmproxy's app host (`asgiapp.serve`) buffers an answer whole and cannot send HTTP/2 trailers, so it can answer neither gRPC (whose status is in the trailers) nor a WebSocket (which outlives its request). Google's client libraries speak gRPC by default (Cloud Tasks, Pub/Sub, Firestore); Slack's Socket Mode and realtime model APIs hold a WebSocket open. Both go to a real server instead, which `adapters/proxy/local.py` starts on `127.0.0.1` for a provider and world on the first call that needs it, and stops when the proxy moves to the next run, the world is closed or reset, or the proxy stops:

| What | Server | How the proxy sends it there | What is recorded |
|---|---|---|---|
| A call to a claimed host with `content-type: application/grpc` (HTTP/2, decrypted as any claimed call) | `grpc.aio` serving the provider's `ServesGrpc.grpc` methods, each answering under the world's lock as a REST call is answered | Re-aimed at the server, which the proxy speaks HTTP/2 to without TLS (`ProxyAddon.server_connect` sets the connection's protocol); the answer and its trailers pass back unchanged | One `Exchange` per call once its trailers have passed: `path` the method, the request and answer messages as proto3 JSON (the server leaves them, and the range of events its method wrote, under an id the proxy added as a header), `grpc` its status and message, `outcome` from the status, `failure` when Minutehand answered in the fake's place |
| A WebSocket upgrade to a claimed host whose provider `ServesSockets` | uvicorn (WebSocket protocol wsproto) serving the provider's `sockets` app | Re-aimed at the server; mitmproxy relays the connection after the 101 | The upgrade as an `Exchange` (status 101, or the refusal), then every message either way as one, `frame` saying which connection, its number on it, who sent it and whether it is text |

A gRPC call to a claimed host whose provider serves no gRPC is answered by the proxy itself in gRPC's Trailers-Only form, UNIMPLEMENTED, saying so; without `minutehand[grpc]` every gRPC call is. A provider's gRPC methods raise `GrpcRefusal` where the real service refuses; `answering.grpc_ended` is the converter, as `convert` is for HTTP. Google Cloud Tasks serves its gRPC API from the operations its REST routes use, reading each request as the proto3 JSON its REST call carries, so a booking made over gRPC fires as one made over REST. Slack's Socket Mode (`slack/socket_mode.py`): `apps.connections.open` hands out a one-use ticket in a `wss://wss-primary.slack.com/link/` URL, recorded in the log; the connection is told `hello`; an event for an agent whose Slack target is `delivery: socket_mode` goes on the agent's newest connection as an `events_api` envelope, sent again until it is acknowledged. Only unary gRPC methods are served, and in `minutehand serve` a WebSocket upgrade, which carries no credential, reaches only the world its host alone selects.

### What the agent reaches directly

The agent's `NO_PROXY` (also `no_proxy` and `no_grpc_proxy`) is `Listen.direct()`: `127.0.0.1`, the hosts named with
`--no-proxy`, the receiver's host when the agent reaches it by another name, and `localhost` only for an agent
that runs elsewhere (`--agent-host` a name that is not this machine's, as in a container). The rule is that an
entry is an address, never a name, wherever that can be:

| Client | How it reads a `NO_PROXY` entry |
|---|---|
| `requests` | An IPv4 host against an address or CIDR exactly; any other host (a name, an IPv6 literal) as a plain suffix: `localhost` covers `search.localhost`, `::1` covers `2001:db8::1` |
| `urllib`, `aiohttp` (`proxy_bypass_environment`) | The host, or any name ending in `.` and the entry: `localhost` covers every name under it |
| `curl` | The host, or a domain it is under; an address as an address |
| `httpx` | The host exactly (`*.` and a domain for names under it) |
| Node: undici's `EnvHttpProxyAgent`, `proxy-from-env` (axios), and the built-in `fetch` with `NODE_USE_ENV_PROXY=1` (Node 24 and later; handed to every agent) | The host exactly, unless the entry starts with `.` or `*` |

`127.0.0.1` is read exactly by every one of them, so no host beyond this machine is sent direct
(`tests/proxy/test_what_goes_direct.py`, with each library's own code, and with `curl` itself). `localhost` itself
then reaches the proxy, which forwards it, plain or tunnelled, to this machine untouched, unrecorded and unseen by
settling, as if it had gone direct (`ProxyAddon.forwarded`), unless a provider, a model host or a declaration claims
it; a name under `localhost` is a host like any other. An agent elsewhere cannot be forwarded to: its `localhost` is
its own, so it is handed `localhost` by name, and every `*.localhost` host it declares goes direct under the suffix
readers. A call to `localhost` forwarded so is not recorded: it stands for a call that went direct before, which no
record held, and recording it would make an agent's record depend on whether its HTTP library honours `NO_PROXY` for
`localhost`. `minutehand doctor --agent agent.yaml [--agent-host H] [--no-proxy H]` checks each declared and model host
against the `NO_PROXY` that run would hand out, by each library's own code in the agent's interpreter (requests,
urllib, httpx, aiohttp) and by the documented rules above (curl, Node), and names each that would go direct.

httpx asks a proxy for a tunnel to an IPv6 literal without its brackets (`CONNECT ::1:8443`), which no proxy can
parse. mitmproxy refuses the request line with a bare 400 before any hook sees a flow; the proxy's `next_layer` hook
sees the connection's first bytes and answers it instead, a 400 naming the address and the cause
(`adapters/proxy/connect.py`). `minutehand doctor` names each declared IPv6 literal when httpx is installed.

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

Built and tested; the whole of it is in `docs/inboxes.md`. Some of what a person does never touches a SaaS: approving
an operation in the agent's own web app, answering a question on its own page. The agent file declares each such
place as an inbox (`AgentUnderTest.inboxes`, `domain.inboxes.HttpInbox`): how Minutehand signs in as a person
(`as_person`, from `Person.credential`), how to list what waits on them (`pending`) and the decisions they can make
(`decisions`), each a template or an operation of an OpenAPI document.

- **Seeing the ask.** At the end of every wake and before the clock moves, every inbox is read as every person
  Minutehand can act as. A new item is the agent asking that person (`InboxItemSnapshot`, actor AGENT); one gone
  undecided is withdrawn. The reads are Minutehand's own calls (`Exchange.inbox_call`), never the agent's.
- **The person decides, never by default.** The replier seam decides as for a reply (`PersonReply.decides`): a
  script's decision for the nth or every item, a model's pick, or nothing from a `Silent` person. A person who can
  receive items and whose script says nothing of them refuses the run. When due, the declared call is made as them;
  the product taking it is their change (`DECIDED`); its refusal is recorded and the wait stays open.
- **Scored by reuse.** An item is an `ANSWER_FROM_PERSON` wait in the ledger, so every rule that counts `follow_ups`
  or `touches` on an ask reads it. Going ahead with an operation the item `gates`, while pending, rejected or
  withdrawn, is the fact `writes: {gated: true}`; a team that holds that against its agent writes a rule.

### Pillar one: proactive effectiveness

A run ends with the facts of it and, when the team declared any, its judgement. Nothing in either is the agent's own account of itself, and nothing in the facts is measured against what the agent should have done.

**Facts in the core, judgement with the team.** Minutehand holds no opinion of how an agent should behave. It states what happened: the waits it opened and when each settled, every follow-up with its time, every message and to whom, every change to the world, every wake and the wakes the agent planned, and what the agent reported. Whether a follow-up was late, a reminder one too many, or an answer acknowledged too slowly is a team's policy; the team writes it as rules (`docs/assessments.md`) and nothing else judges how its agent behaves. Before this, fourteen checks and two verdict rules held Minutehand's own opinion: a real agent that by design never acknowledges an answer, follows up twice and then escalates and stops, failed `slow_to_react` on every run and was asked by `no_follow_up` for a third reminder, and its team could not say otherwise. Each of those checks is now a rule a team may copy (the table in `docs/assessments.md`), and `tests/orchestrator/test_team_rules.py` judges a scripted copy of that agent by its team's policy in YAML.

| Layer | Answers | Whose | State |
|---|---|---|---|
| Facts (`checks/facts.py`, the ledger, `RunView`) | What happened, and when | Minutehand's: stated, never judged | Built and tested |
| Scorecard (`Effectiveness`, `checks/effectiveness.py`) | The facts counted, in numbers that compare across runs, prompts and models | Minutehand's | Built and tested; ends every run |
| Assessments (`assess:`, `checks/assessments.py`) | Did the agent behave as the team wants | The team's rules, in the agent file and the scenario | Built and tested |
| Expectations (`expect:`, `checks/expectations.py`) and protected names (`near_miss_name`) | Did the world end up right | The scenario author's words | Built and tested |
| The agent's own checks (`checks:`) | Anything a rule cannot say, in Python over the same facts | The team's | Built and tested |
| Integrity (`around_proxy`, `unmatched_call`, `agent_contract_changed`) | Can the run be trusted at all | Minutehand's: about the run, never the agent's behaviour | Built and tested |
| Patterns (`checks/patterns.py`) | What design fixes it | Named by a rule's `pattern` | 10, each with a page in `docs/patterns/` |
| Verdict (`Verdict`) | Did what the team declared hold, and did the agent finish | Read from findings and how the run stopped | Built and tested; ends every run and sets the exit code |
| Stability (`Stability`) | How often, over several samples | | Built: `--samples N` reports "passed k of N"; a sample that did not finish did not pass |

#### The scorecard: facts only

```python
class Effectiveness(Model):
    expectations_met: int = Field(ge=0)
    expectations_total: int = Field(ge=0)
    waits_opened: int = Field(
        ge=0,
        description="Asks and hand-offs the agent is owed an answer or work on; the scenario's deadline is not one",
    )
    waits_open_at_end: int = Field(ge=0, description="Of those, the ones the world had not settled when the run ended")
    follow_ups_made: int = Field(
        ge=0, description="Agent writes the person could see on a wait while it was still open"
    )
    waits_settled: int = Field(
        default=0, ge=0, description="Waits the world settled that name a person or entity, so a reaction can be timed"
    )
    slowest_reaction: timedelta | None = Field(
        default=None,
        description="The longest stretch from a wait settling to the agent's next write on it, or to the run's end "
        "when there was none",
    )
    messages_to_people: int = Field(default=0, ge=0)
    ...
    wakes: int = Field(ge=0)
    idle_wakes: int = Field(ge=0, description="Wakes that changed nothing")
    failed_checks: int = Field(ge=0)
```

Every number is a count or a stretch of time. What it no longer holds is what was relative to someone's policy: when a wait "fell due" and whether a follow-up was "late" or "early" (`follow_ups_due`, `follow_ups_late`, `follow_ups_early`), the time the agent "lost" past a grace of an hour (`time_lost`, `slowest_follow_up`), and reactions "slow" past that grace (`reactions_slow`). A team that wants those writes the rule that defines them. The viewer's `/obligations` answers each wait as facts (opened, followed up, settled) and draws nothing as overdue on its own.

What a follow-up is, as a fact (`checks/facts.asks`): an agent write the person could see while the wait was open, a message to them or their delegate, or a change to the ask's thread or ticket. A read is not one: looking at the channel tells nobody anything. One message chasing two waits is one follow-up in `follow_ups_made`.

What it gets wrong: `idle_wakes` counts wakes that wrote nothing and changed no commitment, so a wake that learned something it kept in its own memory reads as idle. `slowest_reaction` runs to the end of the run when the agent never came back, so a run stopped early makes it shorter.

#### The verdict

Built and tested (`checks/runner.verdict`, `tests/checks/test_verdict.py`, `tests/e2e/test_unfinished_verdict.py`). The verdict reads only the findings of what the team declared (its rules, the scenario's expectations and protected names, its own checks), the core's integrity checks, and how the run stopped. It holds no rule of its own about how an agent should behave. `RunResult.verdict` is a `Verdict` (`domain/run.py`): its kind, how the run stopped, how many waits and commitments were still open, and one sentence that the command, the viewer, `list_findings`, `run_scenario` and `list_runs` all print as it is. `RunResult.assessed_by` records what judged the run.

| `VerdictKind` | When | Exit |
|---|---|---|
| `ENVIRONMENT_FAILED` | The run stopped `ENVIRONMENT_FAILED`: an external emulator it forwarded to was unavailable (`docs/external-emulators.md`). Not the agent's failure; the checks still run and are listed | 2 |
| `FAILED` | Any finding is `FindingKind.FAIL` | 1 |
| `NOT_JUDGED` | No finding failed, and nothing was assessed: neither the scenario nor the agent file declares `assess`, `expect`, `protected_names` or its own `checks` ("Not assessed"; the facts are still reported); or a check that reads wakes had none (a standing world nobody stepped). `Verdict.unjudged` lists each reason; it is never `PASSED` | 5 |
| `PASSED` | No finding failed, and the agent reported `DONE`, or nothing was left open: no wait the world had not settled and no commitment its last report held `OPEN` | 0 |
| `UNFINISHED` | No finding failed, the run stopped any other way (`WAKE_LIMIT`, `DEADLINE_PASSED`, `NOTHING_PENDING`, `AGENT_FAILED`, or a captured run that does not say), and a wait or a commitment was still open | 3 |
| `TOOL_FAILED` | Minutehand failed answering any call (`CallOutcome.INTERNAL_ERROR`, below): the run says nothing about the agent, whatever the checks found, and the verdict names the first such call | 4 |

```
run 5c1e0a9f2b77: partner_pricing
  Not finished: no check failed, but the agent never reported it was done; the run stopped at its wake limit, with 1 wait still open.
  the wake limit was 20: the default: the scenario sets no max_wakes and has no deadline to size one from; set `max_wakes` in the scenario, or declare the agent's rhythm (`tick` in the agent file) so the deadline sizes it
```

Exit 3 is not a failure: a scenario whose point is that nobody answers ends at its deadline with the agent's question open, and is `UNFINISHED` rather than `FAILED` (or declares `expect_outcome: unfinished`, which `minutehand run-all` reads). A CI job that wants such a scenario green accepts 3 for it; one that wants every agent to close its work accepts only 0. 2 is also "could not be performed", which has no verdict: both are the environment's, never the agent's.

What the rule gets wrong:

- **It takes `DONE` at the agent's word.** An agent that reports done with a question it asked unanswered passes, unless a rule says otherwise. The verdict once held two rules of its own here (done with an ask never followed up was `UNFINISHED`, and a "follow-up" in the same wake as the ask did not count); both were opinions, and both are now rules a team may write: `when: {stopped: [agent_done]}`, `count: {asks: {open_at: end}}`, `at_most: 0`, and `each: ask`, `count: {follow_ups: {}, until: ask+PT1H}`, `at_most: 0`.
- **A wait on a `Silent` person is never settled,** since every message to them is an unanswered question. The one exception: once every expectation is met, a message to a `Silent` owner sent with or after the last of them is the result being reported, and keeps nothing open. A silent person other than the owner still leaves the run `UNFINISHED`.
- **Nothing open is read as finished.** An agent stopped at a limit that reports no commitments and has no wait open passes, though its goal may be untouched; only the expectations can say the goal was not met.
- **A commitment counts only as the agent reported it.** An agent that reports none is judged on waits alone.


#### The checks left in the core

Discovered by `checks/runner.py` (any class in a module of `checks/` with `id`, `needs` and `run`; no registration). An agent's repository adds its own the same way: the agent file names Python files (`AgentUnderTest.checks`, a relative path read from the file's folder and kept absolute, so a fork finds them), each class in them is loaded as a check (`load_checks`), run after Minutehand's on every run and fork, and counted as they are; a file that cannot load, defines no check, or holds a check with one of Minutehand's ids refuses the run before it starts, and `minutehand validate` says so (`tests/checks/test_own_checks.py`, `tests/e2e/test_cloud_tasks_run.py`). A rule may not take a check's id. Standing worlds (`serve`) run the scenario's rules but not the agent's own checks.

| `id` | What it states | Whose words | `Pattern.key` |
|---|---|---|---|
| `assessments` | One finding per broken bound of each of the team's rules, named by the rule's `id`, with its `severity` and `message`; a note for each rule not read for want of a moment the run never reached | The team's | the rule's `pattern` |
| `expectations` | `FAIL` per unmet expectation; `INFORMATIONAL` per met one, quoting what met it | The scenario author's | `honest_closure` |
| `near_miss_name` | `FAIL`: a name the scenario protects written one letter off | The scenario author's | `confirm_names` |
| `agent_contract_changed` | `FAIL`: the agent's own product answered Minutehand's call against its own API description | The agent's own document | none |
| `around_proxy` | `FAIL`: the agent's own HTTP client spans name calls to a host a provider claims that the proxy never saw; `REVIEW`: the agent was woken and called none of the providers the run names. Both name the fixes ("Transparent capture") | none: the run's integrity | none |
| `unmatched_call` | `REVIEW`: a call to a host no provider claims | none: the run's integrity | none |

Not built:

- **The earliest the work could have finished,** given how the people and systems behaved. With it, a rule could count "finished four days later than was possible". It needs to know which waits depend on which.

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
- **A fork starts only at a checkpoint.** At the end of every wake, and at setup and at the deadline the clock runs on to, the run loop appends a `Checkpoint` (the clock, the decided reply count, scheduled fates, commitments, everything pending, and the agent's last report and its memory's digest) as an entity in the same log (`application/checkpoint.py`). `fork_run` refuses any other `at_seq`; `minutehand run` and `findings` list the ones that exist and say which are restorable.

What a rewind needs beyond the world:

| Part | How it comes back | State |
|---|---|---|
| The clock and everything pending | The `Checkpoint` row at the fork's seq | Built |
| People's replies already given | The `reply` table; copied up to the fork, decided fresh after it | Built |
| The agent's memory | It is the same log: a fork reads its parent's up to the checkpoint, and proves it by its digest (below) | Built and tested |
| What the agent keeps outside its memory | Not at all: a database it names is handed fresh and empty; anything else is the agent's. A fork whose agent's report differs from the checkpoint's is refused, naming it (below) | Refused when the report shows it; silently wrong when it does not |
| AWS's own queues and schedules | Not at all: moto keeps them in process memory, outside the log, and the fork's app takes a fresh account. The manifest says so (`Manifest.state_outside_log`), and a fork at any checkpoint after the parent first called AWS, or wrote an AWS record, is refused, naming the provider, its first call and what it keeps (`application/rewind.py`; `tests/e2e/test_booked_on_aws.py`). A fork before the first call runs. | Refused, never silently wrong |

#### The agent's memory

Built and tested (`src/minutehand/agent/`, `domain/memory.py`, `application/memory.py`, the receiver's `/minutehand/agent` routes; `tests/agent/test_store.py`, `tests/e2e/test_memory_run.py`, `tests/orchestrator/test_rewind.py`, `tests/orchestrator/test_fork_start.py`). Minutehand owns the agent's time and its memory; recording and rewinding follow from that. An agent's whole contact with Minutehand is one import, inert in production:

```python
from minutehand.agent import store, wake

store.configure(store.SqliteBackend("agent.db"))  # production: where the memory lives; ignored under Minutehand


def on_wake(now: datetime) -> None:
    for key, ask in store.query("asks/", where={"status": "asked"}):
        if datetime.fromisoformat(ask["expected_by"]) <= now:
            follow_up(key)
            store.put(key, {**ask, "status": "chased", "expected_by": (now + timedelta(days=2)).isoformat()})
    wake.at(earliest_expected_by())  # the next wake; does nothing in production
```

| | Production (MINUTEHAND_ON unset) | Under Minutehand (`minutehand run` sets MINUTEHAND_ON and MINUTEHAND_AGENT_URL) |
|---|---|---|
| `store` | A pass-through to the backend `configure` named: a three-method adapter (`Backend`: `get`, `scan`, `write` over JSON text). `SqliteBackend`, shared by every process opening the same file, and `MemoryBackend` ship; any other database is an adapter of its own (`docs/agent-contract.md`, "Writing an adapter"). Nothing is recorded. | The run's memory, over HTTP to the receiver (`POST /minutehand/agent/store`, the receiver's host being in the agent's `NO_PROXY`). The backend `configure` named is never called, so the agent's real database is not opened (`SqliteBackend` opens its file at its first call). |
| `wake` | Does nothing; the agent's own scheduler does the work. | `wake.at(moment)` is recorded as the agent's next wake (`EntityKind.NEXT_WAKE`, actor AGENT): the last said in a wake replaces any it marked or reported before; `wake.clear()` says none. |

- **The API.** `get(key, default)`, `put(key, value)`, `delete(key)`, `list(prefix)` (ordered by key), `query(prefix, where={field: value})` (a field may be a dotted path), `batch()` (all or none, `with` or `async with`), `collection(name)` (a namespace of its own keys), and `aget`, `aput`, `adelete`, `alist`, `aquery`. Values are JSON; anything else is refused when written. The package imports the standard library only (`test_the_agent_package_imports_nothing_but_the_standard_library`).
- **What a run records.** Each write is an entity version in the world's log: `EntityKind.MEMORY`, provider `memory`, external id `<collection>/<key>`, actor AGENT, the wake and simulated moment it was made at, the value as canonical JSON in its `MemorySnapshot`. A batch is one transaction (`Store.apply_all`). Each read is an event with no version (`READ` for a get, `SEARCH` for a listing), so `WakeRecord.memory_reads` and `memory_writes` count both per wake. Neither counts as a change to the world (`world_changes`, `writes`), the viewer's events or a fork's first difference: they are the agent's own memory, not something a person could see.
- **Where a run's memory starts.** From the scenario's `memory:` (`SeededMemory`: a key, its JSON value, an optional collection), written as actor SCENARIO before the first wake, and nothing else. Each sample is a run of its own, so samples are independent in their memory.
- **Robustness.** A call that never reached Minutehand, or one answered 502, 503 or 504, is tried again, six times over about three seconds; then `MinutehandUnreachable`, naming the URL. With MINUTEHAND_ON set the store never falls back to the agent's own database. Any other refusal is `MinutehandRefused` with what the receiver said. `minutehand env` hands MINUTEHAND_ON always, and MINUTEHAND_AGENT_URL when the receiver's port is fixed (`--telemetry-port`; the receiver runs whether or not telemetry is received); an agent handed the first without the second refuses to use its store. Under `minutehand serve` neither is handed: a standing world holds no agent's memory, and the store stays the team's own.
- **A fork** reads its parent's log up to the checkpoint, so its memory is exactly the parent's there, with nothing copied, replayed or restarted. Every checkpoint keeps the memory's digest (`Remembered.memory`) and the agent's last report. A fork (`application/restore.start_fork`) proves the memory it sees digests the same, starts the agent's program once the fork exists when Minutehand runs it (so whatever it reads as it starts is the fork's: the receiver holds the agent's calls until then), and asks for the agent's report, which must equal the one recorded at the checkpoint (`differences`). A report that differs is refused, naming the likely cause: state outside the store. An agent with no report endpoint is forked with its memory proven and its report unverified, and `restore.json` says so.
- **A fork that changes the agent's memory** (`MemoryEdit`: keys `put` and `delete`d, written as actor SCENARIO once the fork's memory is proven the checkpoint's) leaves the agent's plan at the checkpoint, made from the old memory, possibly wrong: a follow-up planned for a person the edit marks as answered. So such a fork asks the agent for its report again, and its own planned wakes in the table (its reported or marked next wake, its sandbox timer, a booking) are replaced by the next wake it now names, each closed `REPLACED` in the log; what the scenario owes (replies, happenings, directions, fates, machine commands, late or second deliveries) and a `Polled` agent's declared rhythm stay as the checkpoint holds them. Its report is then not compared, and `restore.json` keeps it as `replanned`. An agent with no report endpoint cannot be asked, and a fork that edits its memory is refused. A fork without memory changes needs no re-ask: its memory and its plan match by construction, and its report is compared as above (`test_a_fork_that_marks_the_person_answered_in_memory_drops_the_follow_up_the_agent_had_planned`).
- **A checkpoint a fork cannot start from.** One after which the agent went on writing its memory in the same wake (it reported IDLE while a process of its own still wrote) holds memory it had not finished writing: `minutehand checkpoints` and `findings` list it as not restorable, naming the first late write, and a fork from it is refused.

**What is not part of a run.** Only state written through the store is. Whatever the agent writes anywhere else, its own database, files, a cache, a variable in a process that outlives a wake, is not simulated, not kept apart between runs, and not rewound by a fork, so the agent's later calls can depend on state from another moment or another run. What Minutehand can see of it, it reports:

| Detected | How |
|---|---|
| What the agent read and wrote through the store, per wake | `WakeRecord.memory_reads`, `memory_writes`; the `memory` count of the assessment language reads the keys at any moment (`docs/assessments.md`) |
| A database of the agent's own | The agent file names the variable it reads the location from (`own_databases: [{env: AGENT_DB, form: file \| sqlite_url}]`); every run and every fork is handed a fresh empty SQLite file in it (`<state>/runs/<run_id>/own/<VARIABLE>.sqlite`), so no run reads another's or production's. The run's notes say it is outside forks, and each checkpoint at which the file holds anything says so (`Remembered.outside`), which `checkpoints`, `findings` and the fork's account repeat. A PostgreSQL or other server is the team's to provide per run. |
| State outside the store that the agent's report reflects | The fork's report comparison, above |
| State outside the store that its report does not reflect | Nothing. A fork from it plays on silently wrong. |

What no fork can rewind, beside that, said in the refusals: what a real third-party service the run reached keeps (the proxy refuses unclaimed hosts, but a tunnelled host, a model API, is reached for real); what a model provider keeps on its side (a stored conversation or response, a cache, a batch, an uploaded file); the AWS provider's queues and schedules, in moto's memory.

Replaced: user-written `snapshot`, `restore`, `stop`, `start`, `busy` and `fingerprint` commands (`StateHooks`), snapshots kept in a pool beside the world file, settling on the proxy's quiet before each checkpoint, and a PostgreSQL relay that recorded the agent's committed transactions and replayed them onto a base for a fork. They asked the team to write and maintain the procedure that put its agent back, could not see a process that held another moment, and covered one database engine; the store asks one import and covers any agent that keeps its state through it.

A fork can change something, and none of it touches the agent's code:

```python
Override = Annotated[
    PromptPatch | ModelSwap | PersonChange | TicketEdit | DeadlineShift | DispatchChange | MemoryEdit,
    Field(discriminator="kind"),
]


class Fork(Model):
    parent_run: str
    at_seq: int = Field(ge=0, description="The last WorldEvent.seq the fork shares with its parent")
    overrides: list[Override] = []
    samples: int = Field(default=1, ge=1)
```

| Override | What changes | Where | Tested |
|---|---|---|---|
| `PersonChange` | A person's `ReplyBehaviour` from the fork onward; every message to them not answered by the fork is put to them again; a reply decided before the fork that had not landed by it is withdrawn first, since it was never said | `changed_scenario`, `_ask_again` in `application/rewind.py` | Through a whole run (`tests/e2e/test_fork_calls_telemetry.py`) |
| `TicketEdit` | A ticket's state or assignee, as actor `SCENARIO` | `EditsTickets.edit` | `tests/orchestrator/test_rewind.py` |
| `DeadlineShift` | The scenario's deadline | `changed_scenario` | `tests/orchestrator/test_rewind.py` |
| `MemoryEdit` | Keys of the agent's memory set or removed at the fork; the agent's own planned wakes replaced by the report it gives after | `memory.edit`, `start_fork`, `Orchestrator.resume(replan=)` | Through a whole run (`tests/e2e/test_memory_run.py`) |
| `DispatchChange` | The scenario's dispatch rules, from the fork on: the same run with the agent's wakes delivered late, twice or dropped; an nth counts the wakes before the fork | `changed_scenario` | `tests/orchestrator/test_dispatch.py` |
| `PromptPatch`, `ModelSwap` | The agent's prompt or model | On the wire: the proxy's `EDIT` policy rewrites the body of the agent's request to its model API | At the proxy only (`tests/proxy/test_model_hosts.py`); not through a whole run |

- `adapters/proxy/edit.py` knows three wire shapes that carry a system prompt: OpenAI chat completions (`messages[0]` with role `system` or `developer`), OpenAI responses (`instructions`), Anthropic messages (`system`). A body no edit applies to goes on byte for byte. Edits match the request as the agent sent it, so a model swap cannot change which prompt patches apply.
- `CallMatch` picks which of an agent's several prompts a patch applies to (`host`, `model`, `system_contains`). How reliably `system_contains` singles one out in a real agent is untested.
- The agent's model calls are never replayed. A patch changes the request and the real model answers it. They are recorded, as spans, only when the agent exports its own telemetry or the run is started with `--record-model-calls`.
- Patching means the proxy opens model traffic it otherwise only tunnels, so it sees prompts and the API key. Locally that stays on the developer's machine. Hosted, it is a trust decision for the customer.
- `Routing.apply` sets the edits for the run about to play; one run plays at a time through one proxy.

A fork is told the same way on every surface (`application/forks.py`, `ForkAccount`; `minutehand findings` and
`runs`, the viewer's `GET /api/runs/{run_id}` as `fork` and its run list as `changed`, and MCP `list_runs` and the
run results as `fork`): the checkpoint it split from, after which wake and at what simulated moment; each override
in words, from what to what (the `Fork` is kept as `fork.json` beside `restore.json`); whether its agent was
verified and by what (`Restored.verified_by`: its memory, its report) or why not; and, once both runs have
finished, the two verdicts, each scorecard line that differs, findings gained, lost and changed, and the first
change in the world after the split at which the two records part (`tests/web/test_fork_account.py`). The viewer
shades the parent's shared record left of the split, fades its marks, and draws what the parent did after the
split in a lane of its own. `scripts/screenshot.py` screenshots a viewer page with headless Edge or Chrome
(`?open` opens every collapsed section) and ends the browser itself: on a profile of its own, headless Edge
writes the file and never exits, on any page.

The parent repository's nearest equivalent restores one captured model step (its inputs and message history) and reruns that step under the current prompt. It restores nothing in Slack or the trackers.

### Hosted

Designed, not built. Hosted Minutehand keeps every run's log and can rewind or fork any of them on request. Each run executes in its own small virtual machine whose only route out is the proxy.

| Problem locally | Why the virtual machine removes it |
|---|---|
| State the agent keeps outside `minutehand.agent.store` is not rewound | The whole machine is snapshotted: the agent, its database, its files |
| A faked date must stay near the real one | The machine's clock is set to the simulated time; the proxy outside holds the real clock and issues certificates valid for the simulated date |
| An agent that reads the clock without the system library is out of reach | Every process on the machine sees the same clock |

It is the reason the hosted service is more than the open-source tool run for you.

### How the clock knows what is next

The clock jumps to the earliest `Due` (`AGENT_WAKE`, `PERSON_REPLY`, `DIRECTION`, `TICKET_FATE`). Four sources produce one, and a run may use several at once (`adapters/agent/reach.py` assembles them into `Reach`).

| Source | What the agent must do | Exact? | Cost of a quiet fortnight | State |
|---|---|---|---|---|
| **Replies and pushed events** | Nothing. The monitor plays the people and delivers through the provider: pushed (Slack, Teams), or landed where the agent reads them and found on its next poll (a Gmail reply, a Calendar guest's answer: `LandsReplies`, which wakes nobody) | Yes | None | Built (Slack, Microsoft, Google Workspace) |
| **`Booked`**: the agent books wake-ups with a scheduler | Nothing. The booking is an outbound call the proxy already intercepts; a scheduler provider (`Manifest.books_wakes`) records the time and delivers when the clock reaches it. | Yes | None | Built and tested through a whole run on AWS (`tests/e2e/test_booked_on_aws.py`) |
| **`Reported`**: the agent answers `next_wake` at `report_url` | An endpoint, or an adapter beside its tests | Yes | None | Built and tested |
| **`Marked`**: the agent marks its next wake with `minutehand.agent.wake`, and is woken at `wake_url` | One import, and a wake endpoint whose call returns when the wake is done | Yes | None | Built and tested (`tests/orchestrator/test_wake_marks.py`); a mark is also the next wake of any other source, replacing what it reported in that wake |
| **`Command`**: one process per wake, `WakeRequest` on stdin, `AgentReport` on stdout | A command | Yes | None | Built and tested |
| **`Polled`**: the agent is invoked every `every` (default 5 minutes) and decides for itself | Declare the rhythm | Yes, at that rhythm | One call per tick: 4,032 calls for 14 days at 5 minutes, each a wake counted against the wake limit, which a deadline and this rhythm size | Built and tested |

- `Polled` never skips a tick, never names a next wake and never reports `DONE`. Skipping is only safe when the agent says when it next matters, which is `Reported`.
- An agent may declare one `Reported` or `Command` source and one `Polled` source; `reach_for` refuses two of either. An agent with only `Booked` wakes must take its goal by message.
- An agent whose scheduler is an in-process loop over its own database is `Reported` through an adapter: nothing can intercept that loop.
- `Booked` is the source that makes a stranger's agent work with no adapter. `tests/providers/aws/test_aws_provider.py` shows it at the provider: a stock `boto3` client with no endpoint override creates an EventBridge Scheduler schedule targeting an SQS queue; `ReceiveMessage` finds nothing before the booking fires and the schedule's input after.

Every scheduler is translated into one internal shape (`Due`, booked through `Wakes`), so the clock knows nothing about any vendor:

| Scheduler | How the agent books | How the wake is delivered | State |
|---|---|---|---|
| AWS EventBridge Scheduler | `CreateSchedule`, `UpdateSchedule`, `DeleteSchedule` with `at(...)`, `rate(...)` or `cron(...)` (no `L`, `W`, `#`), a timezone, start and end dates, state, `ActionAfterCompletion` | Into the target SQS queue (with `MessageGroupId` for FIFO), where the agent's own poll finds it; taken (`ConfirmsDelivery`) when the agent deletes the message (`DeleteMessage`, `DeleteMessageBatch`, JSON or query protocol), and the run waits for that | Built and tested at the provider and through a whole run. Any other target raises when it fires. Each booking, delivery and the agent's delete of a delivery is in the log; the queues themselves are in moto's memory. |
| SQS delay | `DelaySeconds` | moto's own, on the machine clock | Not on the run's clock |
| Google Cloud Tasks | `CreateTask` with an HTTP request and a `scheduleTime`, on the client's default gRPC transport or its REST one (`transport="rest"`, Node's `fallback: true`) | An HTTP call to the task's URL with Cloud Tasks' headers; a non-2xx answer retried with the queue's backoff; taken once the handler answers (`ConfirmsDelivery`) | Built and tested through whole runs on both transports (`google_cloud_tasks`). gRPC is answered from a gRPC server of Minutehand's ("gRPC and WebSockets") and needs `minutehand[grpc]`. |
| A fixed schedule set at deploy time (Cloud Scheduler, a Kubernetes CronJob, Vercel cron) | Not booked at run time at all | `Polled`, with the schedule written in the agent file | Designed |
| A workflow engine's timers (Temporal, Inngest) | Inside the engine | The engine's own time-skipping test server would have to be driven | Not designed |

#### The table, as the log records it

Built and tested (`application/dues.py`, `tests/orchestrator/test_dues.py`, `tests/checks/test_planned_wakes.py`). What the run loop holds pending is a table it dispatches from (`Dues`), and every change to it is written to the world's log as it happens: an entity of `EntityKind.DUE` per entry, actor SCENARIO as the checkpoint is, a version when the entry enters and one when it leaves (`DueEntry`). Each says what is due and when, its source (`DueSource`: the agent's report, its booking, its declared rhythm, a person's reply, a ticket's fate, a happening, a direction), and how it left (`DueClosed`: fired, replaced by another moment from the same source, or cancelled undispatched).

| Rule | Why |
|---|---|
| A next wake the agent names again, the same moment, is the entry already there | An agent asked for its report after every wake would otherwise read as rescheduling every wake |
| A next wake of none cancels the one before; a booking deleted is cancelled; a reply withdrawn is cancelled | Each is the plan changing, and a check can see when |
| A fork takes up its checkpoint's table against the log it shares: an entry the checkpoint dropped (a reply a `PersonChange` withdrew) is cancelled at the fork, and one it added is entered | The fork's table and its log agree from its first event |
| The rows are left out of the viewer's event list, as checkpoints are, and no check counts them as the agent's | They are the run loop's own record, not the world |

`RunView.dues` carries every entry as it last stood; None for a run that kept none (a captured run, a standing world, whose clock is driven from outside). `checks/facts.planned_wakes` reads the agent's own plan from it: each wake the agent asked for itself (reported, booked, its rhythm, its timer) at the moment it was due, a late or second delivery left out, since that is the scenario's. What it cannot see: a plan the agent holds and never reports or books (an in-process scheduler), which reads as no plan. 
#### Deciding what to dispatch

Built and tested (`DispatchRule`, `Dues.dispatch`, `tests/orchestrator/test_dispatch.py`). Real schedulers deliver late, twice (an at-least-once queue) and not at all, and a scenario can say so of the agent's own wakes:

```yaml
dispatch:
  - {wakes: reported, nth: 2, fault: late, by: PT3H}   # the agent's second reported wake comes 3 hours late
  - {wakes: polled, fault: twice, by: PT1M}             # every tick is delivered again a minute after
  - {wakes: reported, nth: 4, fault: dropped}           # the fourth never comes
```

The decision is made when the wake's moment comes, and recorded on its entry (`DueEntry.fault`, closed `DELAYED` or `DROPPED`, or `FIRED` for the first of two). A late or second delivery is an entry of its own carrying the moment the agent asked for (`asked_for`): it is on its way, so a new report from the agent does not take it back, and a tick of it books no next tick. A late or dropped tick leaves the rhythm going from the moment it was due. A rule for the nth wake of a kind wins over one for each; a fork counts the wakes that fell due before it from the log it shares.

| Wakes | late | twice | dropped |
|---|---|---|---|
| `reported` | ✓ | ✓ | ✓ |
| `polled` | ✓ | ✓ | ✓ |
| `booked` | ✓ | ✓ | ✓ |

A scheduler provider delivers and finishes an occurrence in two steps (`BooksWakes.deliver_booking`, `advance_booking`), so a booking can go wrong as a real scheduler's does: delivered twice (`deliver_booking` now and again, `advance_booking` once, after the second), or dropped (`advance_booking` only, so a recurring schedule still books its next occurrence). `advance_booking` books the first occurrence after now, as a real scheduler skips what it missed. A seed refuses `dispatch` (a standing world's clock is driven from outside). Only the agent's own wakes are covered: a person's pace is their `reply`, a ticket's its fate.

What a team's rules make of it: `planned_wakes` counts a wake the agent planned whether or not the scenario delivered it, so a rule can tell an agent with no plan from one whose plan the dispatch rules broke; `writes` with `in_repeated_wake` are what the agent wrote in a second delivery of one wake. Whether either is a failure is the rule's to say.

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
| `ANSWER_FROM_PERSON` (an item) | An item waiting on a person in the agent's own product is first seen (`docs/inboxes.md`) | First seen plus the person's longest delay | The person decides it and the product takes the decision, or the agent withdraws it |
| `DATE` | The scenario has a deadline | The deadline | The run reaches it |

Whether a message asked anything is the replier's decision, never the ledger's. A person with a reply decided to the message was asked; a `Silent` person is asked by every message, since that is what `Silent` means; anyone else was told something that needs no answer (a thank-you, a report), away or not. A person who is only ever told things is `Scripted` with no replies, not `Silent`: the passing example's owner is one.

A message is the same ask as an earlier one, and so a follow-up on that wait rather than a wait of its own, when it goes to the same person in the same conversation (provider and channel; a thread shares its channel) while the earlier wait is open; an answer to it settles the wait. Nothing is read from the text, which gets two cases wrong: a second, different question in the same conversation before the first is answered is folded into the first, and its own answer settles both; and a reminder sent somewhere else (email after chat, a group channel after a direct message) is a new wait. A `Scripted` person whose script answers only their second message was, by the replier's decision, not asked by the first, so an agent that asked and then chased them is scored as having asked once.

A touch is any later agent event on the ask's entity or channel, or an agent message to the person or their delegate. The facts a rule counts on an ask (`follow_ups`, `touches`) and the scorecard read the ledger; `messages` with `to_away` reads the absences directly.

`AgentReport.commitments` stays optional. The cross-check it allows (the agent believes it is waiting on something the world shows as answered, or the reverse) is a rule over `commitments`: `each: ask`, `count: {commitments: {status: [met], waiting_on: [person]}, since: ask, until: closed-PT1S}`, `at_most: 0`.

### Time, for the agent

From least to most invasive; the fakes are on Minutehand's clock in every case.

1. `WakeRequest.now`. The agent uses it as its "now" for the wake. Built.
2. `GET :8081/clock`. For agents that read the time more than once per wake. Not built.
3. The system clock, faked from outside with `libfaketime`. No code change in the agent. Not built; see "Evidence" for the spike that tried it.
4. The agent in a gVisor sandbox whose clock Minutehand owns: every timer the agent's own scheduler arms is read from the sandbox's kernel, and time inside the sandbox is released to the earliest. No code change in the agent, any in-process scheduler, and the machine's clock untouched. Spiked, not built; see "Evidence".

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
- **Metrics.** `minutehand.findings{check,kind}`, `minutehand.wakes{changed}`, and per run, by scenario, the histograms `minutehand.idle_wakes`, `minutehand.follow_ups_made`, `minutehand.run.sim_seconds`, `minutehand.run.wall_seconds`.

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
- **The join.** `application/model_calls.trace_of(event, world)`: from the `traceparent` the intercepted call carried, the agent's calling span, its ancestors to the root, every span of that trace with a `gen_ai.*` attribute, and the model call that led to the event: of the spans whose `gen_ai.operation.name` is `chat`, `text_completion` or `generate_content` (or that name no operation and carry a model), the last to END before the calling span started. With no `traceparent`, or no model call in the trace, a message is joined BY CONTENT when it can be (`JoinedBy.CONTENT`, `by_content`): its text, trimmed of whitespace at both ends and at least `CONTENT_LEAST` (20) characters, appears verbatim in what a model call placed in the same wake answered (`gen_ai.output.messages`, or any string inside it read as JSON, however deep), the call having ended before the event was written, real time; of several, the last to end. Nothing else is normalised and no likeness is scored. Otherwise it takes the last model call of the same wake that ended before the event, a wire-recorded one only if it was kept before the event's seq, and says so (`JoinedBy.WAKE`: the nearest call, not a proven cause). Every surface says which (`tests/test_model_call_join.py`).
- **Where it shows.** `show_evidence` gives each cited event its `model_call` (model, the messages the span carries, token counts), `joined_by` and the agent's span names, and says in `telemetry` when the run received nothing. The viewer serves `GET /api/runs/{run_id}/model-calls` (each event a finding cites, joined) and `GET /api/runs/{run_id}/traces/{trace_id}`; its page shows "What the model was asked and answered" under a finding, or that no telemetry was received.
- **Privacy.** Received spans often hold whole prompts. They are kept in the run's `world.db` on the machine running Minutehand and go nowhere else except to the endpoint the agent's own environment already named.

#### How telemetry serves the two pillars

| Pillar | What the agent's telemetry adds | Built |
|---|---|---|
| Measuring | A finding says what went wrong in the world; its evidence now says what the agent's model was asked and answered just before, so "followed up 33 hours late" comes with the prompt that chose silence. Every span is placed in the wake its start fell in, so a wake's model calls and tokens can be read beside what it changed. | The join and its surfaces. `RunView.model_calls` holds each wake's model calls, for a check of the team's own. No scorecard number counts tokens. |
| Comparing a fork with its parent | A fork sees its parent's spans of the wakes up to the fork, and keeps its own after it; the same event in parent and child can be read with the model call behind each, so a `PromptPatch` can be judged by what the model was then asked and answered, not only by what the world did. | The fork's view of spans. No side-by-side of a parent's and a child's model calls. |

### What a coding agent calls

Built and tested (`adapters/mcp/server.py`, `minutehand mcp`, `tests/mcp/`): served over stdio, one run at a time per process.

| MCP tool | Returns |
|---|---|
| `list_scenarios(directory)` | names and goals |
| `run_scenario(scenario, agent, command, samples)` | `run_id`, counts by `FindingKind`, the verdict, the scorecard, the checkpoints |
| `list_findings(run_id)` | `list[Finding]` |
| `show_evidence(run_id, finding)` | the `WorldEvent`s, their `Exchange`s, the wake, the trace id, and for each event the agent's model call that led to it when the run received its telemetry |
| `rerun_from(run_id, at_seq, changes, command, samples)` | a new `run_id` started from that checkpoint with the overrides applied |
| `list_outbound_calls(run_id)` | per declared outbound host its use, and every captured call with the events it wrote |
| `list_runs()` | every run in the state directory, forks saying what they changed |

The command line (`cli.py`) does the same:

```
minutehand run <scenario.yaml> --agent <agent.yaml> [--state DIR] [--samples N] [--json] [-- <command...>]
minutehand findings <run_id> [--state DIR] [--json]
minutehand fork <run_id> --at <seq> --changes <fork.yaml> [--state DIR] [--json] [-- <command...>]
minutehand runs [--state DIR]
minutehand env --agent <agent.yaml> --proxy-port N [--format shell|compose] [--service NAME...] [--ca-path PATH]
minutehand scenarios [show <name> | new <name>...|--all --goal TEXT --owner 'Name <email>' --ask 'Name <email>' ...]
```

`run`, `fork` and `env` take `--proxy-host`, `--proxy-port`, `--agent-proxy-host`, `--no-proxy HOST` (repeated), `--telemetry-port`, `--no-receive-telemetry`, `--record-model-calls`, `--capture-unknown` and `--upstream-ca FILE` (`serve` takes the last two as well). `run`, `fork` and `findings` print an "outbound calls" section, per host no provider claims, and a declaration for each host nobody declared. `env` prints the environment an agent Minutehand does not start needs, for every run on that port under that state directory: `export` lines, or a Compose override. It makes the proxy's CA if there is none yet, and refuses a port left to the system and a signing secret generated per run.

`run`, `fork` and `findings` exit by the verdict (see "The verdict"): 0 passed, 1 failed, 3 not finished, and 2 when the run could not be performed. With samples: 1 when any sample failed, else 3 when any did not finish, else 0. The state directory defaults to `$MINUTEHAND_STATE`, else `.minutehand`. A fork's changes file holds `overrides` and optionally `samples`; one that names `parent_run` or `at_seq` itself is refused.

### Distribution: a tool beside the codebase, never a dependency of it

The target is one import in the project under test, inert in production (`minutehand.agent`: what the agent remembers and when it next wakes), and nothing else changed. Minutehand itself is installed and run the way a linter is, outside the project's own dependencies.

| Channel | For | Touches the project | State |
|---|---|---|---|
| PyPI, run as `uvx minutehand …` | Anyone with Python 3.12 available | Nothing. `uvx` runs it from its own environment. | Published on PyPI as `minutehand`, released from `main` by publishing a GitHub Release (`docs/releasing.md`). An agent imports `minutehand.agent` from the same package |
| Docker image, built from the same release | Any stack, and CI | Nothing | Not built |
| The git repo | Contributors and provider authors; also `uvx --from git+https://…` before the first release | Nothing | Exists |

PyPI and the repo are not alternatives: the repo is the source, PyPI and the image are how a release reaches a user. The names `minutehand` and `minute-hand` were unclaimed on PyPI and npm on 2026-10-04 and stay claimable by anyone until a first upload.

One command wraps the agent's own start command and injects everything through the environment:

```
minutehand run scenario.yaml --agent agent.yaml -- python -m my_agent
```

| What the run needs | How it gets there with no code change | State |
|---|---|---|
| Outbound calls reach the fakes | The wrapped command gets `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY=127.0.0.1` (each in lower case too; "What the agent reaches directly"), and the CA bundle in `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `HTTPLIB2_CA_CERTS`, `AWS_CA_BUNDLE`. An agent Minutehand does not start gets the same from `minutehand env` | Built. A client that pins certificates is out of reach. `httplib2`, which `googleapiclient` uses, reads `HTTPS_PROXY` only when PySocks is importable and otherwise connects to Google directly, in silence: an agent on `googleapiclient` needs `pysocks` installed (`tests/providers/google_workspace/test_drive_through_proxy.py` runs with it; its `offline/` guard is what turns the silent bypass into a failure). Node's built-in `fetch` reads the proxy with `NODE_USE_ENV_PROXY=1`, which every agent is handed (`tests/providers/slack/test_slack_node_fetch.py`, a real Node 24+ process; without the variable it reached the real slack.com). The Compose override is tested as text, and was run by hand once with the follow-up agent in a container (`docs/containers.md`). The Docker CLI replaces a container's `NO_PROXY` with the client config's `proxies.noProxy` (Docker Desktop writes `*`) unless it is passed with `-e` or Compose; `minutehand env` and `doctor` warn of it. |
| The agent is up before the run starts | Minutehand waits up to 30 seconds for its wake URL, or else its first inbound URL, to accept connections, and fails the run if the command exits first; its output goes to `agent.log` | Built |
| Pushed events reach the agent | The agent's event URL and where its signing secret comes from are in the agent file: generated per run and handed to the command, or the agent's own, read from a variable of Minutehand's | Built |
| The agent wakes at the right moments | Replies, pushed events and `Booked` wake-ups need nothing. `Polled` needs a URL in the agent file. | Built. `Reported` needs an endpoint or an adapter, which is code, though it can live outside the project. |
| The agent agrees on what time it is | `WakeRequest.now`; `libfaketime` preloaded through the same wrapper | `WakeRequest.now` built; `libfaketime` not built |
| Scenarios and the agent file | Plain YAML or JSON files, in the project or anywhere else | Built |

- An agent's whole contact with Minutehand is one import, `minutehand.agent` (`store`, what it remembers; `wake`, when it next wants to be woken), inert in production unless MINUTEHAND_ON is set ("The agent's memory"). An agent that keeps no state across wakes and reports its next wake needs none. The package imports the standard library only, but ships inside `minutehand`, whose dependencies (mitmproxy among them) an agent installing it pulls in; a distribution of its own is not built. A suite that uses `minutehand serve` imports `minutehand.testing` (a client and a pytest plugin), which is test tooling, never a dependency of the code under test.
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
| `mitmproxy` 12.x (MIT) | Proxy, TLS interception, tunnelling and editing model calls. Embedded through one addon; each provider's ASGI app is served with `asgiapp.serve()`, which does not do WebSockets, trailers or streaming, so gRPC and WebSockets are forwarded to loopback servers ("gRPC and WebSockets"). |
| `grpcio` (Apache-2.0), `google-cloud-tasks` (Apache-2.0), in the `grpc` extra | The providers' gRPC servers and Google's Cloud Tasks messages; the receiver's OTLP over gRPC |
| `uvicorn` with `wsproto` (MIT) | The providers' WebSocket servers, and the receiver's HTTP |
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

Ten, in `checks/patterns.py`; each `Pattern.reference` is its page `docs/patterns/<key>.md`. Each is taken from a mechanism a production agent has. A finding names one when the rule that made it does (`pattern:`, refused when there is no such pattern); the column "A rule that finds it" gives a shape a team may copy (the table in `docs/assessments.md` has more).

| `Pattern.key` | Failure | Design | A rule that finds it | Reference mechanism |
|---|---|---|---|---|
| `expiry_on_every_wait` | Waits on something forever | Every wait carries an expected-by date and the agent wakes on it | `each: ask`, `when: {open_at: due}`, `count: {follow_ups: {}, since: due, until: due+PT1H}`, `at_least: 1` | An expected-by date on every blocker and one "next stale check" time derived from them, which the scheduler books |
| `check_world_before_model` | Spends a wake, and the model calls in it, to learn nothing changed | Spend a wake's model calls only on what changed since the last look; the page lists why a wake can change nothing and what each cause needs | `count: {wakes: {changed_world: false}}`, `at_most: 0`, review | A filter to the waits actually stale, and a cheap preflight that ends the wake when none is |
| `absence_aware` | Chases someone who is away | Know who is away and until when; extend the wait or go to their delegate | `count: {messages: {to_away: true}}`, `at_most: 0` | An absence filter over every follow-up before it is sent, rerouting to the named cover |
| `budgeted_follow_up` | Follows up too often, or too late | Space reminders across the time left before the deadline | `each: ask`, `count: {follow_ups: {}, until: due}`, `at_most: 2` | The next reminder computed from the time remaining and the number already sent |
| `bounded_asking` | Asks for input indefinitely | After a fixed number of attempts, stop asking and deliver the best available version | `each: person`, `count: {asks: {of: [person]}}`, `at_most: 3` | A count of attempts per unmet need and a pivot to best-effort delivery past a threshold |
| `one_open_ask_per_person` | Sends the same question twice | Track what is already open with each person before asking | `count: {writes: {repeats_open_ticket: true}}`, `at_most: 0` | A judge that compares each outgoing question with those already open with the same person |
| `no_double_tick` | Does the weekly task twice | A recurring task has one instance per period | `count: {writes: {in_repeated_wake: true}}`, `at_most: 0`, review | A check for an existing instance in the current period before each cadence tick creates anything |
| `honest_closure` | Reports done when it is not | Closing is decided from the state of the world, not from the agent's last message | `expect:`; `when: {stopped: [agent_done]}`, `count: {asks: {open_at: end}}`, `at_most: 0` | Closure evaluated against the recorded state of every piece of work the goal depends on |
| `act_on_the_decision` | Goes ahead with what a person has still to approve, or turned down | Hold each gated operation until a decision that permits it; on a rejection close the work and say so | `count: {writes: {gated: true}}`, `at_most: 0` | The operation's state read at the moment it is performed, against the approval it needs |
| `confirm_names` | Acts on a name it guessed | A name that matters is carried exactly as given, and an assumption is asked about before it is acted on | `near_miss_name`, over `protected_names` | None. Run `f431fc97f427` is the evidence one is needed. |

- Patterns are documentation in the repo, one page each, and data the tool returns (`pattern(key)`; the CLI prints the pattern under each finding). They are not code the user must import.
- The reference mechanisms are what make them more than advice. Publishing the implementations they point to is the separate, heavier product.
- The scenario library (`docs/scenarios.md`, `minutehand scenarios`) ships eleven situations a team fills with its own goal and people. Grading them from easy to hard, with a pass mark, is the form in which this becomes a standard others measure against; that is not designed yet.

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
| **Proxy runtime**: host routing, lazy load, CA, tunnel, edit and refusal policy, base-URL mode | Built. |
| **Store**: schema, events, checkpoints, bodies kept once, the agent's memory | Built. Checkpoints and the agent's memory are rows in the log; bodies of 512 bytes or more are content-addressed. |
| **Clock and orchestrator**: `next_jump`, wake sources, the run loop, forks | Built. The fork is `minutehand fork --at <seq>`, not `rerun_from`. |
| **Providers**: Slack, YouTrack, Asana, Drive | Built, each written new over the store and the clock rather than mounting the parent repository's fakes |
| **Scheduler provider** (`Booked`) | Built for AWS |
| **Checks and the obligations ledger** | Built |
| **Telemetry export** | Built |
| **Telemetry received from the agent**: receiver, storage, forwarding, the join, `--record-model-calls` | Built |
| **Surfaces**: CLI, MCP, viewer | Built: `minutehand mcp` serves the tools over stdio, `minutehand view` the read-only viewer. |
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

Still true of mitmproxy: its app host buffers each response whole and does not implement WebSockets (its own docstring), and sends no trailers. gRPC and Slack Socket Mode take the different path ("gRPC and WebSockets"): mitmproxy relays HTTP/2 to a plaintext upstream when the server connection's protocol is set to `h2` before it opens, trailers included, and relays a WebSocket after the 101 its upstream answers. A streamed answer from a provider's own app still does not stream.

**A faked system clock** (`libfaketime`, an unmodified Python program in a Linux container, clock set by writing a file). Decides "Time, for the agent".

| Question | Result |
|---|---|
| Does the program's clock follow the file while it runs? | Yes, three jumps across 14 days, each read back within a minute of the value written. The sub-minute difference was not investigated. |
| Does a secure connection to a real model API still work under a faked date? | 14 and 60 days ahead: yes, for `api.openai.com` and `api.anthropic.com`. 200 days ahead: no, "certificate has expired". 60 days back: no, "certificate is not yet valid". |
| Does a program asleep on its own timer wake when the clock jumps? | No. `asyncio.sleep(8)` took 8 real seconds across a two-hour jump. |

**A production Teams and SharePoint client against the Microsoft provider** (2026-10-04, a throwaway driver run in its own virtualenv from that client's requirement files, through the proxy, with only its credential stores stubbed). 75 of 80 checks passed: the Teams messaging adapter's sends, replies, card sends and updates, deletes, DMs, history, members, search and workspace discovery; the Graph client's delegated and app-only tokens, item reads and writes, `delta` and subscriptions with the validation handshake; the SharePoint change watch; and the SharePoint document adapter's probes, moves, shares, deletes and containers. The five that failed are the client's, not the fake's: it reads a group or personal chat's history at `/teams/{group}/channels/{chat}/messages` (answered 404, which it swallows into an empty list); it downloads content with `httpx` without following the 302 Graph answers (the old emulator answered 200, which hid this); and its document adapter refuses a rename itself and fails its content update on the same 302.

**A sandbox whose clock Minutehand owns** (2026-10-07; gVisor `go` branch at `fdfbe30` with a 191-line patch, run with `runsc do` inside a privileged container on Docker Desktop's arm64 Linux VM; the patch, the driver and the test programs are outside this repo). Decides that "the agent's next wake, from any in-process scheduler" can be captured and dispatched with no change to the agent, and what that costs.

The patch adds an offset to the sandbox's realtime and monotonic clocks, applied on the syscall path and in the VDSO parameters, and makes the timekeeper's clocks tell their timers when it moves, so an armed sleep, futex, epoll or poll timeout re-checks at once (`Timekeeper.Advance`). Two control calls expose it: `runsc debug --advance-clock=<d>` and `runsc debug --deadlines`, which reads every task's state and blocking deadline at once (`Kernel.Deadlines`). The driver waits until every task is blocked, asks for the earliest deadline, releases time to it, and repeats.

| Question | Result |
|---|---|
| Is an unmodified program's next wake readable from outside? | Yes, as the earliest blocking deadline: Python `asyncio.sleep(3600)` (an `epoll_pwait` timeout), `threading.Timer(7200)` (an absolute `FUTEX_WAIT_BITSET`), `time.sleep(1800)` (an absolute `clock_nanosleep`), Node `setTimeout` for 90 minutes (an `epoll_pwait` timeout, after two start-up timers of 8.1 s and 0.6 s). Read first from the syscall trace (five Node runs in six; parsing a trace races a thread between two calls), then from `--deadlines` (every run). |
| Does releasing time fire the timer, and only then? | Released to ten seconds short of each deadline: none fired. Released past it: each fired 5 to 14 ms of real time later, reading the box's clock exactly the released amount ahead of the host's, whose own clock did not move. |
| Does the whole loop run unattended? | Python's three: one jump each, 0.2 s of real time for up to two simulated hours. Node: two start-up jumps and one to its timer, 0.5 to 0.9 s, six runs in six. Go (`time.AfterFunc` for two hours): fired every time, but in 120 jumps of about 60 s, the runtime's own periodic wake, 21 s of real time; a fortnight would take about an hour unless jumps that wake only the runtime are merged. |
| Does a real model API still answer after a jump? | `api.anthropic.com` over HTTPS from inside the sandbox: answered 401 (no key was sent) with the box's clock 0, 7 and 30 days ahead; 400 days ahead, "certificate has expired". Terminating the model API's TLS at the proxy, as `EDIT` and `RECORD` already do, removes that horizon. |

What it took beyond the patch: the release's own sidecar binaries do not match a build of the `go` branch (the sentry and its prewarmer are separate binaries now), so the patched sentry (`runsc/cmd/sentry/sentry_main.go`, absent from the `go` branch) and the prewarmer (`runsc/prewarmer/prewarmer.c`) were built from the same source; `runsc do` needs `--ignore-cgroups` nested in a container, and `iptables` and `sysctl` to reach the network. Then through Minutehand itself (`Contained`, 2026-10-07): a stock Python agent under `runsc do`, asking Rosa in Slack on its first wake and following up only from an in-process `threading.Timer` of 36 hours, with Minutehand's proxy on the sandbox's gateway (192.168.10.3) and the two `Contained` commands wrapping `runsc debug`. Three simulated days took 3 seconds; the follow-up landed 36 hours less 0.42 s after the ask (real time spent inside the first wake), in a wake of its own, with no wake endpoint for it. Two things only the real run showed: `http.server`'s 0.5 s poll is a deadline like any other (a timer that fires and does nothing is withdrawn as a wake, and the period it fired at is learned as that task's housekeeping, so the run is not stopped at every poll; `--deadlines` reports each task's deadline for it), and the sandbox prints fields `Contained` does not read. Not tried: a pending real call holding time still, checkpoint and restore as a fork, and x86-64.

## Known issues / limitations

- **The agent under test is a model, and its variance is reported, not hidden.** One run fails on any failed check. `--samples N` runs the scenario N times and reports `Stability(samples, passed)`: "passes 3 of 5" is the finding. Each sample is a run of its own with a memory of its own; what the agent keeps outside the store is carried from one sample into the next.
- **One proxy per process.** mitmproxy keeps its master in a module global; `Proxy` refuses a second and is moved from run to run with `mount`, or, under `minutehand serve`, routes each call to its world (`route`).
- **A standing world isolates only by what the call carries.** Services that hold one fixed credential per provider put every test's calls in one world (`docs/serve.md`).
- **Only state written through `minutehand.agent.store` is part of a run.** Whatever the agent writes elsewhere (its own database, files, a cache, a process that outlives a wake) is not simulated, not kept apart between runs, and not rewound by a fork, so its later calls can depend on state from another moment or run. A fork whose agent's report differs from the checkpoint's is refused naming it, and a database the agent file names is handed fresh and empty and said to be outside forks; state its report does not reflect is not seen at all.
- **A fork starts only at a checkpoint the agent did not go on writing its memory after, in the same wake.** An agent that reports IDLE while a process of its own still writes makes that wake's checkpoints not restorable.
- **AWS cannot be rewound.** moto holds queues, messages and its copy of each schedule in process memory, and every run's app takes a fresh AWS account, so a fork from any checkpoint after the agent first used AWS is refused, naming what it cannot rewind. Re-creating moto's state from the log is not built: queue creation, sends and receives are calls, not log entries, and SQS visibility timeouts run on the machine clock. moto reads the machine clock.
- **Slack's signature timestamp is real time** while message `ts` and `event_time` are simulated.
- **Slack's default workspace accepts any token.** A world whose `SlackSeed.workspaces` declares none has one workspace, `T0WORKSPACE`, in which any `xoxb-` or `xoxp-` token acts as the bot; a world that declares workspaces accepts only their tokens.
- **A Slack event retry is not spaced out.** Slack retries after about a minute and then five; the fake retries at once, since no simulated time passes while the agent is called.
- **A press that means to fill a form waits three real seconds** for the agent to open it with the press's `trigger_id`, as Slack's trigger lives three seconds; an agent slower than that fails the run (`FormNeverOpened`).
- **Most providers accept any token.** Slack treats any `xoxb-` or `xoxp-` token as the bot; Asana and YouTrack accept any bearer token unless their own seeds declare tokens. Drive accepts only tokens its `/token` issued, which expire an hour of simulated time later; `/token` matches a refresh token or a service account by name and verifies no signature.
- **No fake's wire details have been verified against the real service.**
- **A rule cannot read meaning.** It counts facts between moments: a thank-you and a chase under an answered ask are both a message `in_thread`, and two messages minutes apart are close whether or not they ask the same thing. Such a rule is best `severity: review`; telling them apart is a judgement, for a check a model judges (not built) or the team's own Python.
- **The enum-comparison lint judges a field by its name, not its type** (`docs/lints.md`).
- **The store's file carries a schema version and refuses other versions;** there is no migration.
- **Bytes are kept once per world file, not across files.** A root run and its forks share every body; two root runs (two samples, two `minutehand run`s) each keep their own copy.
- **A span is placed by comparing two clocks.** Its start comes from the agent's SDK, a wake's window from this machine's clock. On one machine they agree; an agent in a container or on another host whose clock is off by more than the gap between wakes has spans placed in the wrong wake, or by arrival when its start falls outside every window. A span started between two wakes (the agent working after it reported it was idle) is placed by arrival.
- **Replay matches a body by its hash.** A timestamp, nonce, request id or signature in the query or body that is not listed in `ignore_query` or `ignore_body` makes every replay miss; a multipart or compressed body cannot have fields ignored; a body kept only in part can be matched only whole, and an answer kept only in part cannot be replayed.
- **`--capture-unknown` sends for real.** An undeclared email API is passed through in discovery mode, and the email goes out. `--capture-unknown reads` passes only GET, HEAD and OPTIONS and refuses the rest, at the cost of refusing a read sent as a POST.
- **A fork's lookups replay its parent's by default** (`in_forks: replay`): a pass-through host is answered from the parent's recording of the same call, falling back to the real host on a miss.
- **A fork is proven by its memory's digest and its agent's report.** The report carries status, next wake and commitments; state outside the store it does not reflect, and a process left running with another moment in memory whose report reads the same, are not seen.
- **A tunnelled call is a burst of bytes, not a request.** Within one wake, two requests on one connection less than `BURST_QUIET` apart are one record (the agent sending in a later wake always starts a new one, so wakes never share a record), and requests multiplexed at once on HTTP/2 are one; an answer streamed with a pause longer than `BURST_QUIET` is two records, the second opened by the server. A burst still unanswered at the end of a run is written as far as it had gone.
- **Every read of the agent's memory is an event in the log.** An agent that polls its memory (a worker reading its queue every 50 ms) fills the log with reads, and a read made between two wakes counts in the wake before it (`memory_reads`). The ids a provider derives from the log's seq move when memory events are added. Reads are not aggregated.
- **A checkpoint is taken when the agent's driver says the wake is over.** Work the agent goes on doing after it reported IDLE belongs to the next moment; only its late writes to its memory are seen (the checkpoint is then not restorable). On a tunnel, server bytes are read as an answer by their TLS record headers alone, record by record: after a TLS 1.3 client's `Finished`, the server's ticket flight is its leading run of `application_data` records of one length (35 to 2048 bytes, at most four), and the first record that breaks it answers, in the same read or a later one. A server that sends no tickets and answers the first request on a new connection with one record that alone fits the flight looks like one that sends one ticket alone (as AWS does): the request is held as awaiting until the client sends again or closes the connection, so a checkpoint in that wake is not restorable and the burst is written then, or at the run's end, rather than when it falls quiet. An HTTP/2 server's preface and its acknowledgement of the client's still read as answering the first request on a new connection (`adapters/proxy/tunnel.py`); a sandbox whose clock Minutehand owns waits on that before it is released.
- **A booked delivery is taken only when everything its schedule delivered is deleted.** A recurring schedule whose earlier delivery the agent never deleted holds each later wake until `Booked.take_limit`.
- **`minutehand doctor` probes the agent's interpreter, not its running program.** A client built with its own proxy settings, or a non-Python agent, is not seen; curl and Node are checked by their documented `NO_PROXY` rules, not run.
- **An agent elsewhere (a container) is handed `localhost` by name,** since the proxy cannot forward to its loopback: `requests`, `urllib`, `aiohttp` and `curl` send every `*.localhost` host it declares direct. `minutehand doctor --agent-host` names each. A name given with `--no-proxy` is read the same way.
- **httpx cannot reach an IPv6 literal through any proxy** (its CONNECT omits the brackets): the proxy answers 400 saying so, and `minutehand doctor` names each declared IPv6 literal.
- **A fork's first difference from its parent is found among changes in the world only,** compared in order by actor, operation, entity, snapshot and simulated time: a fork whose agent read or searched differently and changed nothing differently reads as not diverged. Findings are paired by check and kind, so two findings of one check are matched in order.
- **Base-URL mode always reaches the real host over HTTPS,** names no IPv6 literal (`/_host/[::1]` is refused 400), and rewrites only URLs written out in full: one percent-encoded inside a query (`?redir=https%3A%2F%2F…`), one a client assembles from parts, and one in a body the proxy streamed through from a real host are left as they came. A web page's URL on a host that is also an API host with no path prefix (a SharePoint `webUrl`, Slack's workspace `url`) is rewritten too. A proxied plain-HTTP call whose own host nothing routes and whose path starts with `/_host/` is taken for a base-URL call.
- **gRPC is unary, and an extra.** A provider's gRPC methods are unary; streaming methods are not served. The gRPC servers and Google's messages need `minutehand[grpc]`, which the container image does not install: there every gRPC call is answered UNIMPLEMENTED, saying so.
- **A WebSocket connection is routed by its host alone.** Under `minutehand serve` a Socket Mode connection, which carries no credential, reaches only a world its host selects.
- **Out of scope:** browser OAuth flows, certificate-pinned clients, reading back from real providers in production, the hosted service.

## References

- `docs/lints.md`: the lints of this repo, each with its five tests.
- `docs/capture.md`: hosts no provider claims, captured by declaration: the three modes, discovery, replay, forks.
- `docs/patterns/`: one page per pattern.
- `docs/ci.md`: the branches, the gate, and what CI runs.
- The adoption guide in `docs/`: how the parent repository consumes this and what must keep working.
- The parent repository's field-observation design and lint rules.
- Reference run page: https://claude.ai/artifact/AEpfatMwhGw2rcs7D428S7
