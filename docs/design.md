# Minutehand

Reconciled with the code on `integration-main` (`9d86c6c`), 2026-10-04. Every code block below is quoted from `src/minutehand/` as it stands. Each part says whether it is built and tested, built with a known limit, or designed and not built. Results of one-off spike scripts, which are not in this repo, are kept only in "Evidence".

## Abstract

Minutehand is how a team builds its own proactive agent and finds out whether it works. It runs the agent through simulated days of work against fake Slack, trackers and document stores, with people who answer late or not at all, and then measures how well the agent carried the work.

Two pillars, in this order:

1. **Evaluating proactive effectiveness.** Did the work get done, how much time did the agent itself lose, how many follow-ups came late, how often did it wake for nothing, and which known failure did it fall into. Every result names the design that fixes it.
2. **Rewind.** Any run can be restarted from any moment with the prompt, the model, the people or the world changed, and played forward again, with no change to the agent's code.

It is one process: it intercepts the agent's outbound API calls, owns the clock, plays the people and records every change. A coding agent reaches it over MCP, so "simulate it and fix what it finds" is a loop that needs no person in it.

## What exists

Tests are `def test_` functions counted per directory: 608 in all, 743 cases once parametrised. `uv run pytest -q -n auto` runs the 736 that need neither a package index nor Docker, with sockets disabled except to `127.0.0.1`, `::1` and `localhost`; 6 are marked `packaging` and 1 `firestore`.

| Part | What it does | State | Tests | Known limits |
|---|---|---|---|---|
| Contracts: `domain/`, `ports/` | The models and protocols every other part is written against | Built and tested | 9 (`tests/test_scenario.py`, `tests/test_clock.py`) | `HumanAction` and `Inbox` are models nothing reads. No check declares `Needs.COMMITMENTS`. |
| World store: `adapters/store/sqlite.py` | Append-only log of events, entity versions, calls and replies; forks share it; a refused fork is discarded | Built and tested | 19 (`tests/test_sqlite_store.py`) | The file carries schema version 2 in `user_version` and refuses any other. Bodies are stored inline; there is no blob store and no catalog of runs. |
| Proxy: `adapters/proxy/` | mitmproxy embedded in the process: answers claimed hosts, tunnels or edits model APIs, refuses the rest; hands the agent one CA bundle (public roots plus its own CA); remembers the agent's latest call for settling | Built and tested | 32 (`tests/proxy/`) | One proxy per process. No base-URL mode for clients that ignore proxy settings. No capture mode. Model API calls are never recorded. A request on a tunnel that is already open is never seen. |
| Slack provider | 15 Web API methods; message events pushed to the agent, signed with the run's secret or the agent's own | Built and tested | 74 (`tests/providers/slack/`) | Any `xoxb-` or `xoxp-` token acts as the bot. `X-Slack-Request-Timestamp` is real time while `ts` and `event_time` are simulated. |
| Asana provider | 20 routes over users, workspaces, projects, sections, tasks and stories | Built and tested | 55 (`tests/providers/asana/`) | Any bearer token is accepted. |
| YouTrack provider | 14 routes, each at `/api` and `/youtrack/api` | Built and tested | 60 (`tests/providers/youtrack/`) | Any bearer token is accepted. |
| Google Drive provider | 14 Drive v3 routes, Docs v1 `documents.get`, Google's `/token` | Built and tested | 62 (`tests/providers/google_drive/`) | Sign-in is not verified; any bearer token is accepted. Content is capped at 5 MiB per file. |
| AWS provider | moto in the process; EventBridge Scheduler bookings become wakes delivered to SQS | Built and tested at the provider | 17 (`tests/providers/aws/`) | AWS's own state lives in moto's memory and cannot be rewound; each run's app takes a fresh AWS account, so a fork starts with none of its parent's queues. moto reads the machine clock for delays, visibility and timestamps. A target other than SQS raises when it fires. No whole run with a `Booked` agent is tested. |
| Run loop, fork, scripted people, agent drivers, files: `application/`, `adapters/agent/` | Plays a scenario on the run's clock; forks a finished run from a checkpoint | Built and tested | 71 (`tests/orchestrator/`) | A fork starts only at a restorable checkpoint (the end of a wake at which the agent settled). A fork needs `StateHooks`. `PromptPatch` and `ModelSwap` are tested at the proxy, not through a whole run. Only `Scripted` and `Silent` people: `Answers` is refused. |
| Rewinding the agent's own state: `application/restore.py`, `examples/state/` | Settles before every checkpoint, restores as a sequence (`stop`, `restore`, `start`, answer), verifies the report against the checkpoint's; recipes for SQLite and a Firestore emulator | Built and tested | 24 in `tests/orchestrator/` (counted above), 4 in `tests/state/` (1 marked `firestore`) | The verify step sees only `AgentReport`. An agent with no `Reported` wake source is restored unverified, and says so. Settling sees only calls through the proxy. PostgreSQL is described, not tested. |
| Checks, ledger, scorecard, patterns: `checks/` | 12 checks, the obligations ledger, `Effectiveness`, 9 patterns | Built and tested | 60 (`tests/checks/` 53, `tests/test_checks_on_reference_run.py` 7) | `repeated_message` measures its window in wall time. |
| Telemetry: `adapters/telemetry/otel.py` | Spans, a log record per finding, metrics, over OTLP | Built and tested | 18 (`tests/telemetry/`) | World-event spans are emitted when a wake ends, not as calls arrive. |
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

1. **Proxy.** The agent's process gets `HTTPS_PROXY`. Calls to hosts a provider claims are answered by that provider's fake. Calls to model APIs are tunnelled without being decrypted, or decrypted and edited when a fork changes the prompt or model. Anything else is refused with 502 and recorded.
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
  stopped at 2026-09-07 10:00 UTC (simulated) because nothing more was due and the agent asked for no wake

fail (2)
  expectations: owner asked mentioning ['confirmed']: wanted at least 1, found 0
    pattern honest_closure: Honest closure. Closing is decided from the state of the world, not from the agent's last message.
  no_follow_up: wait on sofia expired 11 days 6 hours before the run ended and the agent never came back to it
    pattern expiry_on_every_wait: An expiry on every wait. Every wait carries an expected-by date and the agent wakes on it.

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
minutehand run scenario.yaml --agent agent.yaml -- <command>   -> run id, findings, scorecard, checkpoints; exit 1 on any FAIL
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
    scenario.py       Model, Scenario, Person, Answers, Scripted, Silent, DelayRange, WorkingHours, Absence,
                      SeededTicket, SeededDocument, TicketFate, Direction, PersonAsked, TicketCreated,
                      TicketDeleted, TicketInState
    world.py          WorldEvent, Change, Stored, Exchange, RecordedCall, EntityRef,
                      TicketSnapshot, MessageSnapshot, DocumentSnapshot, RecordSnapshot
    agent.py          WakeRequest, AgentReport, Commitment, AgentUnderTest, Reported, Booked, Polled, Command,
                      GoalByWake, GoalByMessage, HumanAction, Inbox, StateHooks
    people.py         PersonReply, PersonMessage, InboundTarget
    provider.py       Manifest, Tier
    experiment.py     Fork, CallMatch, PromptPatch, ModelSwap, PersonChange, TicketEdit, DeadlineShift
    checks.py         Finding, CheckReport, Pattern, Obligation, Stability, Effectiveness, PersonBurden,
                      WakeRecord, RunView, Check
    clock.py          Due, Jump, next_jump()
    run.py            RunRecord, StopReason
  ports/              Store, Clock, Provider, PushesEvents, HoldsTickets, EditsTickets, BooksWakes, Wakes,
                      AgentDriver, Reports, Replier, Telemetry
  application/        orchestrator.py (the run loop), checkpoint.py, rewind.py, restore.py (settle, restore,
                      verify), replier_scripted.py, run_clock.py, state_hooks.py, files.py, refusals.py
  checks/             one module per check; runner.py, ledger.py, effectiveness.py, patterns.py, _waits.py
  adapters/
    proxy/            server.py, addon.py, policy.py, registry.py, hosts.py, edit.py, redact.py
    providers/<key>/  manifest.py, provider.py, app.py, wire.py, state.py, seed.py
    store/            sqlite.py
    agent/            reported.py, polled.py, command.py, reach.py
    telemetry/        otel.py
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

Six protocols, because most services push nothing, hold no tickets and book nothing, and a method that returns nothing on their behalf would be a stub:

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


@runtime_checkable
class HoldsTickets(Protocol):
    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None: ...


@runtime_checkable
class EditsTickets(Protocol):
    def edit(
        self, ticket: EntityRef, *, state: TicketState | None, assignee_email: str | None, world: Store, clock: Clock
    ) -> None: ...


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
| Slack | `slack` | `slack.com`, `*.slack.com` | `PushesEvents` |
| Asana | `asana` | `app.asana.com` (`/api/1.0`) | `HoldsTickets`, `EditsTickets` |
| YouTrack | `youtrack` | `*.youtrack.cloud`, `*.myjetbrains.com` (none; the app answers `/api` and `/youtrack/api`) | `HoldsTickets`, `EditsTickets` |
| Google Drive | `google_drive` | `www.googleapis.com`, `oauth2.googleapis.com`, `docs.googleapis.com` | none |
| AWS | `aws` | `*.amazonaws.com` | `BooksWakes` |

All five are `Tier.FINISHED`. A person "replying" on a tracker is a `TicketFate`: `HoldsTickets.transition` moves the ticket as actor `PERSON`, and the agent finds it on its next read. `session._services` holds each provider to the ports its manifest claims (`pushes_events`, `books_wakes`) and refuses a mismatch by name.

### Flow of one wake

```
Orchestrator.run():
  every provider seeds the world (actor SCENARIO); directions and the first Polled tick enter `pending`
  checkpoint (wake 0)
  START wake: by message, the owner says the goal (PushesEvents.say); otherwise WakeRequest(START, goal)
  loop:
    jump = next_jump(now, pending)
      None                  -> clock runs on to the deadline, checkpoint, stop NOTHING_PENDING
      jump.now > deadline   -> clock runs on to the deadline, checkpoint, stop DEADLINE_PASSED
    clock.jump(jump.now)
    only ticket fates fired -> HoldsTickets.transition, no wake
    otherwise, one wake:
      fire in order: fates (transition), replies (PushesEvents.deliver), directions by message (say),
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

- A person answers a message as it reads when the wake ends. A placeholder the agent edits into its question within the wake is never put to anyone; the question is, once. An edit in a later wake that changes the text is put to the person again unless they have already answered that message, and a reply to the old text still on its way is withdrawn. The withdrawn reply stays in the `reply` table: the ledger reads the latest reply to a message, so it is outvoted when the edit gets an answer, and is read as the answer when the edit gets none.
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
<state>/runs/<run_id>/wake-<n>/      the agent's snapshot after wake n, when it declares `StateHooks` and
                                     settled in time
<state>/runs/<run_id>/restore.json   for a fork, or a sample after the first: each restore step with its
                                     command's output, and whether the restore was verified
```

`world.db` holds five tables: `run` (each run and the seq and call count it was forked at), `event`, `entity_version`, `exchange` and `reply`.

- `entity_version` is the world: append-only, one row per change, read "as of" a sequence number. Providers page through it with `Store.children`; nothing is held in process memory between requests.
- `event` and `exchange` are append-only too. A call that produced no event is recorded with `first_seq > last_seq`.
- `reply` stores every person's reply the first time it is decided. A fork copies the parent's replies up to its checkpoint, so a rerun asks no one again.
- One file per root run isolates parallel runs. A fork lives in its root's file.
- One `sqlite3` connection per store, shared across threads behind one lock: a provider served from a worker thread writes through it.
- `SCHEMA_VERSION = 2` is stamped into `user_version`; a file with tables and another version is refused, not guessed at.

### One container

Designed, not built. Today Minutehand runs as one Python process. `Proxy` listens on `127.0.0.1` on a port the system picks unless `--proxy-host` and `--proxy-port` say otherwise; `minutehand run … -- <command>` starts the agent's own process beside it, or, with no command, wakes an agent that is already running. An agent in containers is given the proxy as `--agent-proxy-host` names this machine (`host.docker.internal`), and `minutehand env --format compose --service <name>…` prints a Compose override that sets the variables below in each named service and mounts the CA bundle read-only.

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
| `EDIT` | A model host the run edits | Decrypted, edited, sent on with the upstream certificate verified; not recorded. An edit that fails answers 502 rather than sending the request unedited. |
| `REFUSE` | Anything else | 502 and recorded with no provider; surfaces as an `unmatched_call` finding |

A model host that overlaps a provider's claim is refused when `Routing` is built. The model-host list is a `Routing` argument; the CLI uses the default. Upstream connections open only when a request is forwarded (`connection_strategy="lazy"`) and the certificate shown to the client is minted, not copied (`upstream_cert=False`); `test_claimed_host_is_answered_without_contacting_it` and `test_unclaimed_host_is_refused_and_recorded_without_contacting_it` watch a listener receive no connection.

Capture mode, which would pass the last row through and record it to measure a provider against the real API, is designed, not built.

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

`mentions` is a case-insensitive substring match on the message text or the ticket's title and body. "Was Sofia asked about pricing" in the sense of meaning, not words, is a judged check and is designed, not built.

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

What this gets wrong: the patience is the person's longest delay whatever the follow-up said, so a reminder that only adds a detail gives the same allowance as one that re-asks; and a follow-up sent a minute before a due moment moves it on a whole delay, so an agent can keep a wait "followed up" by pinging just before each date. `burden` is where that shows.

The reference run, scored by `tests/test_checks_on_reference_run.py` from the run's own turn files (`timeline.json`):

```
expectations_met       3 of 4      the legal-review ticket was never filed
waits_opened           12          5 still open when the run stopped
follow_ups_due         3           made 3, late 1
time_lost              1 day, 8:43 one reminder, 33 hours after the wait expired
wakes                  20          3 changed nothing
```

These waits are read from the captured agent's own records, which predate the ledger and name no person or entity, so no reaction is timed on this run.

| Layer | Answers | State |
|---|---|---|
| Scorecard (`Effectiveness`, `checks/effectiveness.py`) | How well, in numbers that compare across runs, prompts and models | Built and tested; ends every run |
| Expectations (`checks/expectations.py`) | Did the world end up right | Built and tested |
| Checks | Which known failure, where, with evidence | 12 built and tested (below) |
| Patterns (`checks/patterns.py`) | What design fixes it | 9, each with a page in `docs/patterns/` |
| Stability (`Stability`) | How often, over several samples | Built: `--samples N` reports "passed k of N" |

The checks, discovered by `checks/runner.py` (any class in a module of `checks/` with `id`, `needs` and `run`; no registration):

| `id` | Kind of finding | `Pattern.key` |
|---|---|---|
| `acted_after_deadline` | `FAIL`; `REVIEW` for a wake whose late writes are all messages | `budgeted_follow_up` |
| `chased_absent_person` | `FAIL`: messaged someone away while a delegate covered | `absence_aware` |
| `duplicate_ticket` | `FAIL`: the same normalised title filed twice in one project while the first was open | `one_open_ask_per_person` |
| `expectations` | `FAIL` per unmet expectation | `honest_closure` |
| `idle_wake` | `REVIEW`: a wake that changed nothing in the world and nothing the agent was waiting on | `check_world_before_model` |
| `kept_chasing_after_done` | `REVIEW`: a message threaded under an answered ask, or naming a finished ticket | `one_open_ask_per_person` |
| `late_follow_up` | `FAIL`: a follow-up more than `GRACE` after the wait fell due | `expiry_on_every_wait` |
| `near_miss_name` | `FAIL`: a protected name written one letter off | `confirm_names` |
| `no_follow_up` | `FAIL`: a wait still open fell due and nothing followed; says how many follow-ups came before | `expiry_on_every_wait` |
| `repeated_message` | `REVIEW`: two messages to one channel within five minutes of simulated time, no reply between, sharing rare wording | `one_open_ask_per_person` |
| `slow_to_react` | `FAIL`: an answer landed or work was finished and the agent came back late or never | `expiry_on_every_wait` |
| `unmatched_call` | `REVIEW`: a call to a host no provider claims | none |

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
| AWS's own queues and schedules | Not at all: moto keeps them in process memory, outside the log, and the fork's app takes a fresh account. A fork whose checkpoint holds a pending booking is refused, naming the booking (`application/rewind.py`). | Known limit |

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
```

- **Settle.** A checkpoint is snapshotted only when the agent reports it is not `WORKING` and no outbound call of its has been seen for `quiet`, measured from the later of the moment settling began and its last call. The proxy remembers the latest call it saw (`Proxy.last_call`, `Traffic`): every request it answers, edits or refuses, every `CONNECT`, every new tunnelled connection. An agent with a `Reported` wake source is asked for its report again once quiet (`ports.agent.Reports`); a call made while it is asked starts the quiet again. One that has not settled within `settle_limit` is written as `NotRestorable(reason)`, e.g. "the agent was still making outbound calls when the settle limit (0.5 s) ran out: its last, GET /testchat/inbox, …", and is never snapshotted. A settled one is `Restorable(snapshot_of, wake, report)`: the report is what the restore must bring back. A run with hooks and nothing watching the agent's calls is refused.
- **Restore** is a sequence, each step's output kept in the child's `restore.json`: `stop` (the agent's command, when Minutehand started it with `--`, then the agent's own `stop`), `restore`, `start` (the agent's own, then Minutehand's command), then `answer`: the report endpoint must answer within `answer_limit`. A step that fails, cannot be started or runs past `step_limit` refuses the fork: "the restore of the agent from the checkpoint at seq 18 failed at step `restore`: … exited 5 after 0.1 s. Its output: …". `minutehand fork` names each step on stderr as it is taken. A sample after the first is restored the same way from the first sample's setup.
- **Verify.** The report after the restore is compared with the recorded one (`differences`). Equal means: the same `status`; a `next_wake` that is the same instant, or both none; the same commitments as a set keyed by `Commitment.key`, each with the same `status`, where none reported and an empty list are the same. A difference refuses the fork field by field: `next_wake: 2026-08-26T09:00:00+00:00 at the checkpoint, none after` is a restore that silently did nothing; `…, 2026-08-28T09:00:00+00:00 after` is the snapshot of another wake restored.
- **What the comparison cannot see:** anything the report does not carry (a conversation, a cache, a draft, a commitment's description and dates); a restore that put back another moment whose report reads the same; state in a service the agent uses that was not restored and is not reflected in its report; in-memory state in a process the restore did not stop and start. An agent with no `Reported` source (`Command`, `Polled`, by message only) cannot be asked between wakes: it is restored, and `restore.json` and `minutehand fork` say it was not verified and why.
- **Refusals leave nothing.** No hooks, no checkpoint at the seq, a checkpoint not restorable, no snapshot directory, a booking pending, a ticket edit that cannot land: each is refused before `Store.fork`. A restore that fails after the child exists discards it (`Store.discard`) and `session.fork` removes its directory. Before, every refusal after `Store.fork` left an empty child run in the world file.

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
- The agent's model calls are never recorded or replayed. A patch changes the request and the real model answers it.
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
| **`Booked`**: the agent books wake-ups with a scheduler | Nothing. The booking is an outbound call the proxy already intercepts; a scheduler provider (`Manifest.books_wakes`) records the time and delivers when the clock reaches it. | Yes | None | Built (AWS); not tested through a whole run |
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
| AWS EventBridge Scheduler | `CreateSchedule`, `UpdateSchedule`, `DeleteSchedule` with `at(...)`, `rate(...)` or `cron(...)` (no `L`, `W`, `#`), a timezone, start and end dates, state, `ActionAfterCompletion` | Into the target SQS queue (with `MessageGroupId` for FIFO), where the agent's own poll finds it | Built and tested at the provider. Any other target raises when it fires. Each booking and delivery is a `RecordSnapshot` in the log; the queues themselves are in moto's memory. |
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
| `ANSWER_FROM_PERSON` | The agent messages a person who has a reply decided to it, or is `Silent`, and owes no answer in that conversation already | The message's time plus the person's `DelayRange.longest`; `patience` is that delay | The first reply to any message on the wait, if the run reached it |
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

Built and tested (`tests/telemetry/test_otel_telemetry.py`). OpenTelemetry SDK, exported over OTLP/HTTP; the CLI builds it only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. The SQLite store is the record; telemetry is an export of it.

- **Spans**

  | Span | Parent | Carries |
  |---|---|---|
  | `minutehand.run` | none | `minutehand.run_id`, `minutehand.scenario`, `minutehand.seed`, simulated start; at the end `minutehand.stop`, `minutehand.wall_seconds` |
  | `minutehand.wake` | run | `minutehand.wake`, `minutehand.wake.reason`, simulated time |
  | `<provider> <operation> <kind>`, e.g. `asana create ticket`, `SpanKind.SERVER` | the caller's span; else the wake; else the run | `minutehand.seq`, `minutehand.wake`, `minutehand.entity.id`, `minutehand.actor`, simulated time; with a call, `http.request.method`, `server.address`, `url.path`, `http.response.status_code` |

- **Joining the agent's trace.** When the intercepted request carried a valid W3C `traceparent`, the world-event span is created as its child and linked to its wake, so what happened in the world sits in the same trace as the model call that caused it.
- **Simulated time.** A world-event span starts and ends at its event's `wall_time`, because backends reject or misplace future timestamps. Simulated time travels as `minutehand.sim_time` (ISO 8601 string) and `minutehand.sim_time_unix_nano` (int). World-event spans are emitted when the run loop reads a wake's new events, after the wake.
- **Findings.** One log record per `Finding` (`event_name="minutehand.finding"`), in the trace of the span of its first evidence, with `minutehand.check`, `minutehand.finding.kind`, `minutehand.evidence`, `minutehand.pattern`, and OTel severity from `Finding.severity`.
- **A fake's 4xx is not an error.** A provider refusing bad input is the fake working; the span status stays unset. A 5xx sets status `ERROR`.
- **Bodies stay local** unless `MINUTEHAND_EXPORT_BODIES=1` (`minutehand.request.body`, `minutehand.response.body`).
- **Metrics.** `minutehand.findings{check,kind}`, `minutehand.wakes{changed}`, and per run, by scenario, the histograms `minutehand.time_lost_seconds`, `minutehand.idle_wakes`, `minutehand.follow_ups_late`, `minutehand.run.sim_seconds`, `minutehand.run.wall_seconds`.

### What a coding agent calls

Designed, not built. `mcp` is a declared dependency; no module imports it.

| MCP tool | Returns |
|---|---|
| `list_scenarios` | names and goals |
| `run_scenario(name, agent)` | `run_id`, counts by `FindingKind` |
| `list_findings(run_id)` | `list[Finding]` |
| `show_evidence(run_id, finding)` | the `WorldEvent`s, their `Exchange`s, the wake, the trace id |
| `rerun_from(run_id, wake)` | a new `run_id` started from that checkpoint |

What exists is the command line (`cli.py`):

```
minutehand run <scenario.yaml> --agent <agent.yaml> [--state DIR] [--samples N] [--json] [-- <command...>]
minutehand findings <run_id> [--state DIR] [--json]
minutehand fork <run_id> --at <seq> --changes <fork.yaml> [--state DIR] [--json] [-- <command...>]
minutehand runs [--state DIR]
minutehand env --agent <agent.yaml> --proxy-port N [--format shell|compose] [--service NAME...] [--ca-path PATH]
```

`run`, `fork` and `env` take `--proxy-host`, `--proxy-port`, `--agent-proxy-host` and `--no-proxy HOST` (repeated). `env` prints the environment an agent Minutehand does not start needs, for every run on that port under that state directory: `export` lines, or a Compose override. It makes the proxy's CA if there is none yet, and refuses a port left to the system and a signing secret generated per run.

Exit 0 when no finding is `FindingKind.FAIL`, 1 when any is (with samples, when any sample failed), 2 when the run could not be performed. The state directory defaults to `$MINUTEHAND_STATE`, else `.minutehand`. A fork's changes file holds `overrides` and optionally `samples`; one that names `parent_run` or `at_seq` itself is refused.

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
| Outbound calls reach the fakes | The wrapped command gets `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY=localhost,127.0.0.1` (each in lower case too), and the CA bundle in `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `HTTPLIB2_CA_CERTS`, `AWS_CA_BUNDLE`. An agent Minutehand does not start gets the same from `minutehand env` | Built. A client that pins certificates is out of reach. Node's built-in `fetch` needs `NODE_USE_ENV_PROXY=1`; unverified. The Compose override is tested as text; no container has been run with it. |
| The agent is up before the run starts | Minutehand waits up to 30 seconds for its wake URL, or else its first inbound URL, to accept connections, and fails the run if the command exits first; its output goes to `agent.log` | Built |
| Pushed events reach the agent | The agent's event URL and where its signing secret comes from are in the agent file: generated per run and handed to the command, or the agent's own, read from a variable of Minutehand's | Built |
| The agent wakes at the right moments | Replies, pushed events and `Booked` wake-ups need nothing. `Polled` needs a URL in the agent file. | Built. `Reported` needs an endpoint or an adapter, which is code, though it can live outside the project. |
| The agent agrees on what time it is | `WakeRequest.now`; `libfaketime` preloaded through the same wrapper | `WakeRequest.now` built; `libfaketime` not built |
| Scenarios and the agent file | Plain YAML or JSON files, in the project or anywhere else | Built |

- There is no client package and nothing to import.
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
| `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | Telemetry |
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
| `check_world_before_model` | Spends a model call to learn nothing changed | On waking, look at the world with plain code first; involve the model only when judgement is needed | `idle_wake` | A filter to the waits actually stale, and a cheap preflight that ends the wake when none is |
| `absence_aware` | Chases someone who is away | Know who is away and until when; extend the wait or go to their delegate | `chased_absent_person` | An absence filter over every follow-up before it is sent, rerouting to the named cover |
| `budgeted_follow_up` | Follows up too often, or too late | Space reminders across the time left before the deadline | `acted_after_deadline` | The next reminder computed from the time remaining and the number already sent |
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
| **Store**: schema, events, checkpoints | Built. Checkpoints are rows in the log. Blobs not built. |
| **Clock and orchestrator**: `next_jump`, wake sources, the run loop, forks | Built. The fork is `minutehand fork --at <seq>`, not `rerun_from`. |
| **Providers**: Slack, YouTrack, Asana, Drive | Built, each written new over the store and the clock rather than mounting the parent repository's fakes |
| **Scheduler provider** (`Booked`) | Built for AWS |
| **Checks and the obligations ledger** | Built |
| **Telemetry export** | Built |
| **Surfaces**: CLI, MCP, viewer | CLI built. MCP and viewer not built. |
| **Composition and end-to-end**: `session.py`, files, agent drivers, a real agent process on stock `slack_sdk` | Built |
| **Adoption in the parent repository**: one container in compose and CI, the adapter, the deletions | Not built here; see the adoption guide in `docs/` |
| **People written by a model** | Not built |
| **Generated providers** (the `GENERATED` engine), then **judged checks** | Not built |

The remaining five fakes of the parent repository (Jira, Teams, Graph, Notion, GitHub) are five more parallel pieces, following the four providers' pattern.

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
- A provider is tested over its ASGI app, against the store as its only state, through its refusals (`test_*_refusals.py`; AWS's are in `test_aws_provider.py`), and through the service's own client library over a real socket (`slack_sdk`, `asana`, `google-api-python-client`, `boto3`; YouTrack over plain HTTP). Response validation against a provider's OpenAPI document is not built.
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

## Known issues / limitations

- **The agent under test is a model, and its variance is reported, not hidden.** One run fails on any failed check. `--samples N` runs the scenario N times and reports `Stability(samples, passed)`: "passes 3 of 5" is the finding. Each sample after the first starts from the agent's state at the first sample's start, restored and verified as a fork's is; without hooks the samples are not independent.
- **One proxy per process.** mitmproxy keeps its master in a module global; `Proxy` refuses a second and is moved from run to run with `mount`.
- **A fork starts only at a restorable checkpoint,** and only for an agent with `StateHooks`. A checkpoint at which the agent did not settle within `settle_limit` is not restorable.
- **A restore is proven by the agent's report alone.** Two moments with the same status, next wake and commitments are indistinguishable to the verify step; an agent with no report endpoint between wakes is restored unverified.
- **Settling sees only calls through the proxy.** A call to `localhost` or a request on a tunnel already open is invisible, so background work there can outlive the quiet period unseen.
- **A Firestore emulator restore is a restart:** Google's emulator imports only as it starts; measured at 4.5 to 16.7 s over four restores here, about 58 s on a more loaded machine.
- **AWS cannot be rewound.** moto holds queues, messages and its copy of each schedule in process memory, and every run's app takes a fresh AWS account, so a fork sees none of its parent's queues, and a booking pending at the fork raises when it fires. moto reads the machine clock.
- **Slack's signature timestamp is real time** while message `ts` and `event_time` are simulated.
- **Every provider accepts any token.** Slack treats any `xoxb-` or `xoxp-` token as the bot; Asana, YouTrack and Drive accept any bearer token; Drive's `/token` verifies nothing.
- **No fake's wire details have been verified against the real service.**
- **`PromptPatch` and `ModelSwap` are tested at the proxy, not through a whole run.**
- **A booking's wake is not awaited.** A wake made only of bookings sends no request and polls no report, so the clock may move on before a `Booked` agent acts on the delivery.
- **`repeated_message` measures its five-minute window in wall time.** On the reference run it flags the one real repeat; in a simulated run, messages days apart in simulated time can be seconds apart in wall time.
- **The enum-comparison lint judges a field by its name, not its type** (`docs/lints.md`).
- **The store's file carries a schema version and refuses other versions;** there is no migration.
- **Out of scope:** browser OAuth flows, certificate-pinned clients, Slack Socket Mode, reading back from real providers in production, the hosted service.

## References

- `docs/lints.md`: the lints of this repo, each with its five tests.
- `docs/patterns/`: one page per pattern.
- `docs/ci.md`: the branches, the gate, and what CI runs.
- The adoption guide in `docs/`: how the parent repository consumes this and what must keep working.
- The parent repository's field-observation design and lint rules.
- Reference run page: https://claude.ai/artifact/AEpfatMwhGw2rcs7D428S7
