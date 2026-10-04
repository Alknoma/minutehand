# Minutehand

Draft, 2026-10-04. Code quoted here is in `src/minutehand/` and `lints/`, type-checks clean under pyright, and is tested against captured run `f431fc97f427` (`tests/data/partner_pipeline/`). The spike scripts named below (`spike/…`) were one-off proofs and are not in this repo; their results are recorded here.

## Abstract

Minutehand is how a team builds its own proactive agent and finds out whether it works. It runs the agent through simulated days of work against fake Slack, trackers and document stores, with people who answer late or not at all, and then measures how well the agent carried the work.

Two pillars, in this order:

1. **Evaluating proactive effectiveness.** Did the work get done, how much time did the agent itself lose, how many follow-ups came late, how often did it wake for nothing, and which known failure did it fall into. Every result names the design that fixes it.
2. **Rewind.** Any run can be restarted from any moment with the prompt, the model, the people or the world changed, and played forward again, with no change to the agent's code.

It is one process: it intercepts the agent's outbound API calls, owns the clock, plays the people and records every change. A coding agent reaches it over MCP, so "simulate it and fix what it finds" is a loop that needs no person in it.

## Positioning

**Claim:** Build your proactive agent yourself.

**The objection it answers:** "A proactive agent is a product somebody sells me. If I build one, I get a chatbot with a cron job, and I will not know what it is missing until it embarrasses me in front of a colleague."

**The answer:** what separates the two is know-how, and Minutehand hands it over. It runs your agent through the situations a proactive agent has to survive, shows where yours breaks, and names the design that fixes each break.

What is sold is the know-how, in three forms:

| Form | What it is | Where it comes from |
|---|---|---|
| **Scenarios** | The situations: a person goes quiet, a person is away, a date moves, an approval is declined, a weekly task recurs, a deadline closes in | Ayven's 21 field-observation cases |
| **Checks** | What going wrong looks like in each | The nine failure categories captured from Ayven's own runs, and the incidents behind each guard |
| **Patterns** | The design that stops it, with a working implementation to read | How Ayven does it |

A finding carries its pattern (`Finding.pattern`), so the coding agent that reads "followed up 33 hours late" is handed "give every wait an expiry and wake on it" in the same answer.

Monitoring is how the know-how is delivered. It is not the product.

## Problem

Measured on alknoma-cloud, 2026-10-04:

| What | Evidence |
|---|---|
| A run reports no pass or fail | `FieldRunSummary.terminal_reason` is the only verdict. Run `f431fc97f427` ended `turns_exhausted` while the agent had researched the wrong company, filed two tickets under that name, and sent a reminder 33 hours late. |
| The fakes run on the real clock | `lints/wall_clock.py` finds 72 machine-clock reads across the 9 emulators (Slack 37, Asana 11, Teams 10, Jira 6, YouTrack 3, Graph 2, Drive 1, GitHub 1, Notion 1). The mission clock reached 1 September; every ticket and message was stamped 24 August. |
| The harness is welded to one agent | `runner.py` imports `select_action`, `scheduling_now` and `IssueRepositoryImpl`, reads `nextCheck`/`nextStaleCheck`/`activeExecutionId` from Firestore, and writes `simulatedNow` directly. |
| People's replies skip the world | Replies are posted to the `/bridge` endpoint of `request_response_service`. No inbound Slack event is ever produced. |
| Production code carries the test stack | 29 switch sites: 10 base-URL swaps, 12 credential bypasses, 7 parallel fake adapters (`EmulatorDriveService` alone is 539 lines). |
| Ten containers, state in memory | Five fakes (Teams, Graph, Drive, Notion, GitHub) hold the world in process memory; four (Slack, Jira, Asana, YouTrack) reload and rewrite whole JSON files on mutation. The tenth container is Firebase's own emulator. |
| It cannot gate a change | Field observation is excluded from CI (`-m "not field_observation"`), is manual, and takes 30–90 minutes a run. |

## Solution

One process with five parts:

1. **Proxy.** The agent's container sets `HTTPS_PROXY`. Calls to hosts a provider claims are answered by that provider's fake. Calls to model APIs pass through. Anything else is refused and recorded.
2. **Providers.** One module per SaaS, loaded on its first request. Each answers the real API's routes, writes to the store, and turns each call into a `WorldEvent`.
3. **Clock.** Minutehand's clock is the only clock in a run. `next_jump()` moves it to the next moment something is due, every provider stamps from it, and the agent is told the time by it.
4. **People.** A scenario's `Person` replies after a simulated delay, through the provider as a real inbound event.
5. **Checks.** Functions over `RunView` that return `CheckReport`. Deterministic first; anything that needs judgement answers `review`.

The agent under test declares how it comes back to work (`AgentUnderTest.wakes`). At most it answers `WakeRequest` with `AgentReport`; an agent that books its own wake-ups answers nothing.

## Examples

### A scenario

```yaml
name: partner_pipeline_build
goal: Ayven has three signed integration partnership agreements.
owner: owner
starts_at: 2026-08-24T10:50:03Z
deadline_after: P14D
protected_names: [Ayven]
people:
  - {key: owner,  name: Test User,      email: owner@example.com}
  - {key: sofia,  name: Sofia Romano,   email: sofia@example.com}
  - {key: dania,  name: Dania Kovac,    email: dania@example.com, reply: {kind: silent}}
ticket_fates:
  - {assignee: sofia, becomes: done, after: P3D}
```

`Scenario.model_validate` rejects an unknown field, a duplicate `Person.key`, and any `assignee`, `owner` or `delegate` that names nobody.

### A check that ran on the real capture

```
$ python tests/test_against_captured_run.py .../runs/f431fc97f427__partner_pipeline_build
near_miss_name: 4 finding(s)
  [fail] wrote "Aiven" where the scenario says "Ayven"  evidence=[1]
  [fail] wrote "Aiven" where the scenario says "Ayven"  evidence=[2]
  [fail] wrote "Aiven" where the scenario says "Ayven"  evidence=[7]
  [fail] wrote "Aiven" where the scenario says "Ayven"  evidence=[8]
repeated_message: 3 finding(s)
  [review] two messages to the same person 18 seconds apart with no reply between  evidence=[3, 4]
  [review] two messages to the same person 217 seconds apart with no reply between  evidence=[4, 5]
  [review] two messages to the same person 2 seconds apart with no reply between  evidence=[7, 9]
expectations: 1 finding(s)
  [fail] ticket created for dania: wanted at least 1, found 0  evidence=[]
near_miss_name on the same run with the name corrected: 0 findings
```

`near_miss_name` finds the two tickets and two messages and goes clean when the name is corrected. `repeated_message` is imprecise on this capture: one of its three flags is the real duplicate. It measured wall time because the capture has no simulated time on messages; it answers `review` for that reason.

### The loop a coding agent runs

```
run_scenario(name="partner_pipeline_build")        -> run_id, 4 fail, 2 review
show_evidence(run_id, finding=1)                   -> the ticket as filed, the wake it was filed in, the trace id
  ... edits the agent's code ...
rerun_from(run_id, wake=2)                         -> restores the checkpoint before wake 2, replays recorded replies
```

## Spike: one process, intercepted calls, existing fakes

Run on 2026-10-04 with `spike/run_spike.py` (macOS, Python 3.12, mitmproxy 12.2.3) and again as one container (`spike/Dockerfile`).

| Claim | Result |
|---|---|
| mitmproxy can be embedded and answer for real hostnames | Yes. `DumpMaster` plus one addon that calls `mitmproxy.addons.asgiapp.serve()`. |
| The existing Flask fakes mount unchanged | Yes for Slack and Asana, with the two adjustments below. |
| A provider loads on first use | Slack imported on call 1 (0.01 s), Asana on call 4 (0.01 s), GitHub never. |
| A client needs environment variables only | Yes. `slack_sdk.WebClient(token=…)` with its default `https://slack.com/api/` and plain `httpx.get("https://app.asana.com/api/1.0/workspaces")` were both answered by the fakes with only `HTTPS_PROXY` and `SSL_CERT_FILE` set. |
| Trace context survives | The `traceparent` header on the Asana call was recorded on its `exchange` row. |
| An unclaimed host is refused | `https://example.org/` got 502 and an `exchange` row with no provider. |

| Measure | As a process | As one container |
|---|---|---|
| Ready to accept calls | 0.26–0.54 s over five starts | 1.6 s including `docker run` |
| Memory, no provider loaded | 87 MB | 57 MiB |
| Memory, Slack and Asana loaded | 91–97 MB | 58 MiB |
| Image size | n/a | 367 MB on `python:3.12-slim` |
| One call | p50 0.9 ms, p95 1.0 ms | not measured |
| Eight clients at once | 1,401 calls a second | not measured |

What the spike turned up:

- **Two options are required so the real host is never contacted:** `connection_strategy="lazy"` and `upstream_cert=False`. They were set; the absence of upstream traffic was not checked by watching the network.
- **The fakes seed themselves inside `if __name__ == "__main__"`.** An import does not run that, so Slack answered `invalid_auth` until the loader copied `data/defaults/*.json` into place. A `Provider.seed()` call replaces this.
- **The Asana fake serves at the root; the real API is under `/api/1.0`.** Our adapter's base URL hid the difference. An intercepted client sends the real path, so a manifest carries a path prefix.
- **The fakes still stamp with the machine clock.** The Slack `ts` in the spike is the real date while the recorded `sim_time` is 30 August. This is the 72-read defect, unchanged until providers take the clock.
- **mitmproxy's app host buffers each response whole and does not implement WebSockets** (its own docstring). Streaming responses and Slack Socket Mode need a different path.

## Architecture

### Components

```
src/minutehand/
  domain/            pure: no I/O, no clock reads, no provider imports
    scenario.py      Scenario, Person, Answers, Helpfulness, WorkingHours, Absence, SeededTicket, TicketFate, Expectation
    world.py         WorldEvent, Exchange, EntityRef, TicketSnapshot, MessageSnapshot
    agent.py         WakeRequest, AgentReport, AgentUnderTest, WakeSource, HumanAction, Inbox, StateHooks
    provider.py      Manifest, Tier
    experiment.py    Fork, Override, PromptPatch, ModelSwap, PersonChange, TicketEdit
    checks.py        Finding, CheckReport, Pattern, Obligation, Stability, RunView, Check
    clock.py         Due, Jump, next_jump()
  application/       orchestrator (the wake loop), check runner, replier
  adapters/
    proxy/           interception, host routing, pass-through policy
    providers/<name> manifest.py, app.py, normalise.py, seed.py, inbound.py
    store/           SQLite
    telemetry/       OpenTelemetry export
    mcp/             the tools a coding agent calls
    web/             control API and the viewer
  checks/            one file per check
lints/               discovered by directory, no registration
```

`domain/` and `application/` never import `adapters/`. A provider never imports another provider.

### Data models

Every model extends `Model` (`frozen=True, extra="forbid"`). Kinds are `StrEnum` members and unions are discriminated on a `kind` literal, so nothing is decided by matching on a string.

The contract with the agent:

```python
class WakeRequest(Model):
    run_id: str
    now: AwareDatetime
    reason: WakeReason  # START | DUE | PERSON_REPLIED | DIRECTION | TICK
    goal: str | None = None  # set on the START wake only


class AgentReport(Model):
    status: AgentStatus  # WORKING | IDLE | DONE
    next_wake: AwareDatetime | None = None
    commitments: list[Commitment] | None = None


WakeSource = Annotated[Reported | Booked | Polled | Command, Field(discriminator="kind")]


class AgentUnderTest(Model):
    name: str
    wakes: list[WakeSource]  # at least one; replies and pushed events always wake it
```

An agent may need none of this: one that books its wake-ups with an intercepted scheduler declares `Booked` and exposes nothing. See "How the clock knows what is next".

What a provider records, the same for every provider:

```python
class WorldEvent(Model):
    seq: int
    run_id: str
    wake: int  # 0 is setup
    sim_time: AwareDatetime
    wall_time: AwareDatetime
    actor: Actor  # AGENT | PERSON | SCENARIO
    operation: Operation  # CREATE | UPDATE | DELETE | READ | SEARCH
    entity: EntityRef
    after: Snapshot | None = None  # TicketSnapshot | MessageSnapshot | DocumentSnapshot
    exchange: Exchange | None = None  # the raw HTTP call, provider's own format, as text
```

What a check returns, after `alknoma-cloud/research-services/lints/_core.py`:

```python
class Finding(Model):
    check: str
    severity: Severity  # ERROR | WARNING | INFORMATION  (how loud)
    kind: FindingKind  # FAIL | REVIEW | INFORMATIONAL  (what the reader must do)
    message: str
    at: AwareDatetime | None = None  # simulated time
    wake: int | None = None
    evidence: list[int] = []  # WorldEvent.seq


class CheckReport(Model):
    findings: list[Finding] = []
    blocked: list[str] = []  # the check could not read its input: it did not run
    notes: list[str] = []
```

### The provider port

Two protocols, because five of the nine providers push nothing and a method that returns `[]` is a stub:

```python
class Provider(Protocol):
    manifest: Manifest  # name, hosts, entity kinds

    def app(self, store: Store, clock: Clock) -> ASGIApp: ...
    def normalise(self, exchange: Exchange) -> list[WorldEvent]: ...
    def seed(self, scenario: Scenario, store: Store) -> None: ...


class PushesEvents(Protocol):  # Slack, Teams, Graph, Notion
    def deliver(self, reply: PersonReply, target: AgentInbound) -> None: ...
```

Jira, Asana, YouTrack, GitHub and Drive implement `Provider` only. A person "replying" on those is a state change in the store (`TicketFate`), which the agent finds on its next read.

### Flow of one wake

```
orchestrator: next_jump(now, pending) -> Jump
  clock.set(jump.now)                         every provider now stamps this time
  for due in jump.firing:
    PERSON_REPLY  -> provider.deliver(...)    a real inbound event reaches the agent
    TICKET_FATE   -> store.update(...)        recorded as actor=SCENARIO
    AGENT_WAKE    -> POST wake_url WakeRequest(now, reason)
  until AgentReport.status != WORKING:        poll report_url
    proxy: request -> provider.app -> store -> Exchange -> normalise -> WorldEvent
  store.checkpoint(wake)
  pending += AgentReport.next_wake, replies owed for new asks
checks.run(RunView) -> CheckReport -> store, telemetry, exit code
```

## Implementation

### Storage: SQLite, one file per run

| Option | Verdict |
|---|---|
| **SQLite (WAL)** | Chosen. In the standard library, one file, on disk, one writer with any number of readers (the viewer reads while a run writes), and the world can be read "as of" any sequence number with plain SQL. |
| DuckDB | Not for the world: column store, weak at many small transactional writes. Useful read-only for questions across many runs; it attaches SQLite files directly. |
| LMDB / RocksDB | No query language; every listing endpoint (`conversations.history`, issue search) would be hand-written scans. |
| Postgres | A second process. Breaks "one container, one command". |
| JSON files (today) | Rewrites the whole file per mutation and holds it all in memory. |

```
<state>/catalog.db                     runs, scenarios
<state>/runs/<run_id>/world.db         entity_version, event, exchange, wake, finding, reply
<state>/blobs/<sha256>                 any body over 64 KB
```

- `entity_version` is the world: append-only, one row per change, read "as of" a sequence number (see "The changelog, rewind and forks"). Providers page through it with SQL; nothing is held in process memory between requests.
- `event` and `exchange` are append-only too.
- `reply` stores every person's reply the first time it is produced. `rerun_from` replays them, so a rerun does not call a model for the people.
- One file per root run isolates parallel runs and makes deletion a directory removal. A fork lives in its parent's file.

### One container

Achievable for every SaaS fake. One image, one process, two ports:

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
      SSL_CERT_FILE: /ca/ca.pem          # httpx, requests, slack_sdk
      REQUESTS_CA_BUNDLE: /ca/ca.pem
      HTTPLIB2_CA_CERTS: /ca/ca.pem      # googleapiclient
      NODE_EXTRA_CA_CERTS: /ca/ca.pem
    volumes: ["minutehand-ca:/ca:ro"]
```

Without Docker: `uvx minutehand up`.

Three limits:
- **The agent's own database is not a SaaS fake.** Firebase's emulator suite is Google's and stays a separate container.
- **The agent's container must trust the CA.** One environment variable per HTTP library, as above.
- **A client that ignores proxy settings** uses the `/p/<provider>/` base URL instead, which is a configuration change in the agent.

### Lazy loading

- A provider's `manifest.py` is data only: name, host patterns, the path prefix the real API uses, entity kinds. All manifests load at start; nothing else does.
- The proxy maps a request's host to a manifest and imports that provider's `app` on first use (`importlib.import_module`).
- Providers register under the entry-point group `minutehand.providers`, so a provider can ship as a separate package.
- Per-customer hosts (`*.atlassian.net`, a self-hosted YouTrack) are wildcard patterns in the manifest plus hosts a scenario declares.

### Thousands of services

Nine hand-written fakes took about 19,400 lines. That does not reach thousands, so a provider has a `Tier`, and only the top one is written by hand.

| `Tier` | What it is | Made from | Effort per service |
|---|---|---|---|
| `OBSERVED` | Calls pass through to the real service and are recorded | Nothing | None |
| `GENERATED` | Stateful create, read, update, delete, list and paginate over the store; requests and responses validated against the service's own description | An OpenAPI document, or the tool list of a remote MCP server | None; one generic engine serves them all |
| `TAUGHT` | Generated, then corrected: recorded real traffic is replayed against it and a coding agent fixes each difference | `OBSERVED` recordings plus the conformance run | Minutes of agent time, reviewed |
| `FINISHED` | Refusals, pushed events, sign-in, known quirks | Hand work | Weeks; reserved for what every agent touches (Slack, Teams, the big trackers) |

- **One engine, not thousands of providers.** `GENERATED` is a single provider whose manifests are produced from descriptions. The APIs.guru directory holds roughly 1,900–2,500 public descriptions.
- **MCP is the shorter road.** A remote MCP server lists its tools with schemas. The same engine can stand in for any of them, and an agent that reaches its services through MCP needs nothing else.
- **An unmapped resource is still checked.** A generated provider emits `RecordSnapshot(resource, text)`. Checks that need only the written text run on it; checks that need a ticket's assignee do not, and say so in `blocked`.
- **Mapping is data.** Which resource is a ticket and which field is its title is a short mapping file per service, drafted by a coding agent and reviewed.
- **Prior art:** FetchSandbox generates a stateful sandbox from an OpenAPI document and is hosted; Prism and Microcks serve examples without state. Nango's provider catalogue (1,000+ APIs) is under the Elastic License and cannot be copied into this repo.
- **Unproven:** how much of a real service's behaviour create-read-update-delete over its description actually covers. This needs measuring on five services before the tier is promised.

### Hosts the proxy does not own

| Host | Default |
|---|---|
| Claimed by a provider | Answered by the fake |
| A model API (`api.openai.com`, `api.anthropic.com`, …; configurable list) | Passed through untouched |
| Anything else | Refused with 502 and recorded; surfaces as an `unmatched_call` finding |

Capture mode flips the last row to "passed through and recorded", which is how a provider's fidelity is measured against the real API.

### What "right" means for a scenario

A scenario states what must be true of the world, in macro terms: a person was asked, a ticket was created, deleted, or reached a state. Each is a typed selector with a count and an optional time bound.

```yaml
expect:
  - {kind: person_asked,   person: owner}
  - {kind: ticket_created, assignee: sofia}
  - {kind: ticket_created, assignee: dania, by: P5D}
  - {kind: ticket_created, mentions: [Aiven], at_least: 0, at_most: 0}
  - {kind: ticket_deleted}                      # default: at most 0
```

On the reference run, `expectations` reports one failure: `ticket created for dania: wanted at least 1, found 0`. The legal-review ticket the goal depends on was never filed; both attempts were declined.

`mentions` is a literal word match. "Was Sofia asked about pricing" in the sense of meaning, not words, is a judged check and is queued with the others.

### A person acting in the agent's own product

Some of what a person does never touches a SaaS. In alknoma-cloud an approval is decided at `operation_gateway`'s `/api/v1/consent/decide` and a question is answered at `request_response_service`'s `/bridge`. No fake can stand in for those: they are the agent's own endpoints.

```python
class HumanAction(Model):
    name: str
    description: str  # when a person would do this; the persona reads it
    method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str  # may hold {argument} placeholders
    body: str | None  # JSON text with {argument} placeholders
    arguments: list[ActionArgument]


class Inbox(Model):  # where the monitor learns what is waiting on a person
    url: str
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
    expectations_met: int
    expectations_total: int
    waits_opened: int
    waits_open_at_end: int
    follow_ups_due: int  # waits that passed their expected date while still open
    follow_ups_made: int
    follow_ups_late: int
    time_lost: timedelta  # late follow-ups plus slow reactions: what the agent added
    slowest_follow_up: timedelta | None
    wakes: int
    idle_wakes: int
    failed_checks: int
```

The rule that makes it fair: time the world itself took is not the agent's. A person who needed three days, or a reviewer who never answered, costs the agent nothing. `time_lost` counts only the stretch between the moment the agent should have acted and the moment it did.

The reference run, scored by `tests/test_effectiveness_on_captured_run.py` from the run's own turn files:

```
expectations_met       3 of 4      the legal-review ticket was never filed
waits_opened           12          5 still open when the run stopped
follow_ups_due         3           made 3, late 1
time_lost              1 day, 8:43 one reminder, 33 hours after the wait expired
wakes                  20          3 changed nothing
failed_checks          5           4 wrong company name, 1 unmet expectation
```

| Layer | Answers | State |
|---|---|---|
| Scorecard (`Effectiveness`) | How well, in numbers that compare across runs, prompts and models | Model and `measure()` written; run on one capture |
| Expectations | Did the world end up right | Written; run on one capture |
| Checks | Which known failure, where, with evidence | Three written |
| Patterns | What design fixes it | Nine listed |
| Stability | How often, over several samples | Model only |

Not built:

- **The earliest the work could have finished,** given how the people and systems behaved. With it, `time_lost` becomes "finished four days later than was possible". It needs to know which waits depend on which.
- **Reaction time to an answer.** The capture does not record when a reply reached the agent, so `time_lost` here is late follow-ups only.
- **Burden on people:** messages per person, per outcome. The data is in the log; the measure is not written.
- The reference scores above read waits from Ayven's own mission records, because the obligations ledger that would derive them from the world is not built.

### Pillar two: the changelog, rewind and forks

The world is a log, not a state. Every change any provider makes is one row with a sequence number and the simulated time, and a fake's "current state" is a question asked of that log.

```
entity_version(provider, kind, external_id, seq, sim_time, wall_time, actor, body)   -- append-only
```

- **The world as of any moment is a query:** the latest version of each entity at or below a sequence number. Rewinding does not restore anything; it moves the point the fakes read from.
- **A fork is a child run that shares its parent's log up to a sequence number** and writes its own rows after it. No copy is made, so forking a two-week run costs nothing and any number can branch from one moment.
- **Providers do not know.** They read and write through the store, which applies "as of" for them.
- Per-wake file checkpoints become an optimisation for long logs, not the mechanism.

What a rewind needs beyond the world:

| Part | How it comes back |
|---|---|
| The clock and everything pending | Rows in the same log |
| People's replies already given | The `reply` table; replayed up to the fork, written fresh after it |
| The agent's own state | Locally: `StateHooks`, a snapshot and a restore command named in the agent file. Hosted: a snapshot of the whole virtual machine the agent runs in, which needs no hooks. |

Without a way to restore the agent's own state, `rerun_from` is refused and a rerun starts at the beginning.

A fork can change something, and none of it touches the agent's code:

```python
Override = PromptPatch | ModelSwap | PersonChange | TicketEdit | DeadlineShift


class Fork(Model):
    parent_run: str
    at_seq: int  # the last WorldEvent.seq shared with the parent
    overrides: list[Override]
    samples: int = 1
```

| Override | What changes | Where |
|---|---|---|
| `PersonChange`, `TicketEdit`, `DeadlineShift` | The people and the world from the fork onward | In Minutehand; recorded as `actor=SCENARIO` rows |
| `PromptPatch`, `ModelSwap` | The agent's prompt or model | On the wire. The agent's request to its model provider already passes through the proxy; the proxy edits the body and sends it on. |

`spike/prompt_patch.py` shows the second kind working: a client posted a request with model `gpt-5.6-luna` and a one-line system prompt; the upstream host received `gpt-5.6-terra` and the prompt with a sentence appended; the user message was untouched. Both legs were real TLS and the client was unchanged.

- The agent's model calls are still never recorded or replayed. A patch changes the request and the real model answers it.
- Patching means the proxy opens model traffic it otherwise only tunnels, so it sees prompts and the API key. Locally that stays on the developer's machine. Hosted, it is a trust decision for the customer.
- `CallMatch` picks which of an agent's several prompts a patch applies to. How reliably `system_contains` singles one out in a real agent is untested.

What alknoma-cloud has today is the inference half. `iua_single_turn` restores one captured model step of the update agent (its inputs and message history, 27 cases) and reruns that single step under the current prompt. It stops after the step and restores nothing in Slack or the trackers.

### Hosted

Hosted Minutehand keeps every run's log and can rewind or fork any of them on request. Each run executes in its own small virtual machine whose only route out is the proxy.

| Problem locally | Why the virtual machine removes it |
|---|---|
| The agent's own state needs snapshot and restore commands | The whole machine is snapshotted: the agent, its database, its files |
| A faked date must stay near the real one | The machine's clock is set to the simulated time; the proxy outside holds the real clock and issues certificates valid for the simulated date |
| An agent that reads the clock without the system library is out of reach | Every process on the machine sees the same clock |

None of this is built or tested. It is the reason the hosted service is more than the open-source tool run for you.

### How the clock knows what is next

The clock jumps to the earliest `Due`. Four things produce one, and a run may use several at once.

| Source | What the agent must do | Exact? | Cost of a quiet fortnight |
|---|---|---|---|
| **Replies and pushed events** | Nothing. The monitor plays the people and delivers through the provider. | Yes | None |
| **`Booked`**: the agent books wake-ups with a scheduler (Cloud Tasks, EventBridge Scheduler, QStash, a delayed queue message) | Nothing. The booking is an outbound call the proxy already intercepts; a scheduler provider (`Manifest.books_wakes`) records the time and calls the agent back when the clock reaches it. | Yes | None |
| **`Reported`**: the agent answers `next_wake` | An endpoint, or an adapter beside its tests | Yes | None |
| **`Polled`**: the agent is invoked every N minutes and decides for itself | Declare the rhythm | Yes, at that rhythm | One call per tick: 4,032 calls for 14 days at 5 minutes |

- `Polled` never skips a tick. Skipping is only safe when the agent says when it next matters, which is `Reported`.
- Ayven is `Reported` through an adapter: `next_wake = min(nextCheck, nextStaleCheck)`. Its scheduler is an in-process loop reading Firestore, which nothing can intercept.
- `Booked` is the source that makes a stranger's agent work with no adapter. `spike/booked_wake.py` shows it on AWS: an agent using the stock `boto3` SDK, with no endpoint override, created an EventBridge Scheduler schedule for 27 August targeting its SQS queue. Its queue poll returned nothing on the 24th and the 26th. When the clock was moved to the 27th the booking fired and the next poll returned the message. AWS itself is answered by `moto` (Apache-2.0), mounted in the same process.

Every scheduler is translated into one internal shape, so the clock knows nothing about any vendor:

| Scheduler | How the agent books | How the wake is delivered | State |
|---|---|---|---|
| AWS EventBridge Scheduler, SQS delay | JSON over HTTPS; `moto` already fakes the storage | Into the target queue, where the agent's own poll finds it; or an HTTP call for a target mapped to a URL in the agent file | Spike works for a one-time schedule to SQS |
| Google Cloud Tasks | gRPC by default, plus a token fetch from Google's sign-in host; no `moto` equivalent | An HTTP call to the task's URL | Not attempted. gRPC responses need trailers, which the app host used elsewhere does not produce. |
| A fixed schedule set at deploy time (Cloud Scheduler, a Kubernetes CronJob, Vercel cron) | Not booked at run time at all | `Polled`, with the schedule written in the agent file | Designed |
| A workflow engine's timers (Temporal, Inngest) | Inside the engine | The engine's own time-skipping test server would have to be driven | Not designed |

### What the agent is waiting on

The monitor does not need to be told. It plays every person and owns the clock, so it already knows what was asked, when the answer landed, and what the agent did in between.

```python
class Obligation(Model):
    key: str
    kind: ObligationKind  # ANSWER_FROM_PERSON | WORK_WITH_PERSON | DATE
    person: str | None  # Person.key
    entity: EntityRef | None
    opened_at: AwareDatetime
    opened_by: int  # WorldEvent.seq of the ask or hand-off
    due_at: AwareDatetime | None  # when the world will settle it; None is never
    settled_at: AwareDatetime | None
    agent_touches: list[int]  # agent events on the same person or entity while open
    first_touch_after_settled: int | None
```

| Opens when | Settles when | Checks it feeds |
|---|---|---|
| The agent messages a person and that person's `ReplyBehaviour` would answer | The reply is delivered | No follow-up after silence; follow-up while the person was away; asked twice; slow to act on the answer |
| The agent assigns a ticket to a person | `TicketFate` lands | Never looked again; kept chasing after it was done |
| The scenario fixes a date | The clock passes it | Acted before it; nothing done by the deadline |

`AgentReport.commitments` stays optional and becomes a cross-check: the agent believes it is waiting on something the world shows as answered, or the reverse.

### Time, for the agent

From least to most invasive; the fakes are on Minutehand's clock in every case.

1. `WakeRequest.now`. The agent uses it as its "now" for the wake.
2. `GET :8081/clock`. For agents that read the time more than once per wake.
3. The system clock, faked from outside with `libfaketime`. No code change in the agent.

Option 3 was tried on 2026-10-04 (`spike/faketime/`, an unmodified Python program in a Linux container, clock set by writing a file):

| Question | Result |
|---|---|
| Does the program's clock follow the file while it runs? | Yes, three jumps across 14 days, each read back within a minute of the value written. The sub-minute difference was not investigated. |
| Does a secure connection to a real model API still work under a faked date? | 14 and 60 days ahead: yes, for `api.openai.com` and `api.anthropic.com`. 200 days ahead: no, "certificate has expired". 60 days back: no, "certificate is not yet valid". |
| Does a program asleep on its own timer wake when the clock jumps? | No. `asyncio.sleep(8)` took 8 real seconds across a two-hour jump. |

What follows:

- **A run must start at the real date and stay inside the real certificates' lifetime,** about two months ahead, unless model-API traffic is terminated at the proxy and re-sent from the real clock. Terminating it means the proxy decrypts prompts it otherwise only tunnels.
- **A scenario with a fixed past `starts_at` cannot use option 3.** `starts_at` defaults to the moment the run begins.
- **An agent that polls on a short real-time loop works under option 3:** after a jump its next poll reads the new time. The cost is one real poll interval per jump.
- **An agent that sleeps until a far-off moment does not wake.** It needs `Booked` or `Reported`.
- **The monitor still cannot see when an in-process scheduler next wants to run.** Jumping straight to the next reply would skip a follow-up the agent meant to send in between, and the run would blame the agent for lateness the jump caused. Such an agent is `Polled` at a declared rhythm, or `Reported`.
- Untested: a JVM under a faked clock (the Firestore emulator), Node, and a faked clock across several containers at once.

### Telemetry

OpenTelemetry SDK only, exported over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. The SQLite store is the record; telemetry is an export of it.

- **Spans**

  | Span | Parent | Carries |
  |---|---|---|
  | `minutehand.run` | none | `minutehand.run_id`, `minutehand.scenario`, `minutehand.seed` |
  | `minutehand.wake` | run | `minutehand.wake`, `minutehand.wake.reason`, `minutehand.sim_time` |
  | `<provider> <operation> <kind>`, e.g. `asana create ticket` | the caller's span | HTTP semantic-convention attributes, `minutehand.entity.id`, `minutehand.sim_time` |

- **Joining the agent's trace.** When the intercepted request carries a W3C `traceparent`, the provider span is created as its child, so what happened in the world sits in the same trace as the model call that caused it. Without one, the span parents to `minutehand.wake`.
- **Simulated time.** Span timestamps are wall time, because backends reject or misplace future timestamps. Simulated time travels as `minutehand.sim_time` (ISO 8601 string) and `minutehand.sim_time_unix_nano` (int), since OTLP has no datetime attribute type.
- **Findings.** One log record per `Finding`, linked to the spans of its evidence, with `minutehand.check`, `minutehand.finding.kind` and OTel severity from `Finding.severity`.
- **A fake's 4xx is not an error.** A provider refusing bad input is the fake working; the span status stays unset.
- **Bodies stay local** unless `MINUTEHAND_EXPORT_BODIES=1`. They are large and carry customer-shaped text.
- **Metrics.** `minutehand.findings{check,kind}`, `minutehand.wakes{changed}`, `minutehand.commitment.overdue` (histogram, simulated seconds), `minutehand.run.sim_seconds`, `minutehand.run.wall_seconds`.

### What a coding agent calls

| MCP tool | Returns |
|---|---|
| `list_scenarios` | names and goals |
| `run_scenario(name, agent)` | `run_id`, counts by `FindingKind` |
| `list_findings(run_id)` | `list[Finding]` |
| `show_evidence(run_id, finding)` | the `WorldEvent`s, their `Exchange`s, the wake, the trace id |
| `rerun_from(run_id, wake)` | a new `run_id` started from that checkpoint |

`minutehand run scenario.yaml --agent …` is the same thing for CI and exits 1 when any finding is `FindingKind.FAIL`.

### Distribution: a tool beside the codebase, never a dependency of it

The target is zero lines changed in the project under test. Minutehand is installed and run the way a linter is, outside the project's own dependencies.

| Channel | For | Touches the project |
|---|---|---|
| PyPI, run as `uvx minutehand …` | Anyone with Python 3.12 available | Nothing. `uvx` runs it from its own environment; it is not added to the project's requirements. |
| Docker image, built from the same release | Any stack, and CI | Nothing |
| The git repo | Contributors and provider authors; also `uvx --from git+https://…` before the first release | Nothing |

PyPI and the repo are not alternatives: the repo is the source, PyPI and the image are how a release reaches a user. The names `minutehand` and `minute-hand` are unclaimed on PyPI and npm as of 2026-10-04 and stay claimable by anyone until a first upload.

One command wraps the project's own start command and injects everything through the environment:

```
uvx minutehand run scenario.yaml -- docker compose up
uvx minutehand run scenario.yaml -- python -m my_agent
```

| What the run needs | How it gets there with no code change | Where it stops being free |
|---|---|---|
| Outbound calls reach the fakes | `HTTPS_PROXY` and the CA variables set on the wrapped command; for Compose, an override file written outside the repo and passed with `-f` | A client that pins certificates. Node's built-in `fetch` needs `NODE_USE_ENV_PROXY=1`, also an environment variable; to verify. |
| Pushed events reach the agent | The agent's event URL and the name of its signing-secret variable are in the scenario's agent file; the secret is generated per run and injected | Nothing |
| The agent wakes at the right moments | Replies, pushed events and `Booked` wake-ups need nothing. `Polled` needs a URL in the agent file. | `Reported` needs an endpoint or an adapter. That is code, though it can live outside the project. |
| The agent agrees on what time it is | `libfaketime` preloaded through the same wrapper; works for a Python process (see "Time, for the agent") | Runs must start at the real date and stay within about two months. An agent that sleeps on its own long timer does not wake on a jump. |
| Scenarios and the agent file | Plain files, in the project or anywhere else | Nothing |

- There is no client package and nothing to import. A pytest plugin may come later as a convenience; it is not the path.
- mitmproxy requires Python 3.12 and pins 27 dependencies, which is one more reason the tool never enters a project's environment.
- Providers register under the entry-point group `minutehand.providers`. The nine ship inside `minutehand`; anything else is `minutehand-provider-<name>`, installed into the tool's environment with `uvx --with`.

### Python, not Rust

| | Measured on the Python spike |
|---|---|
| Per call, one client | p50 0.9 ms, p95 1.0 ms |
| Eight clients at once | 1,401 calls a second, p50 5.4 ms |
| Ready | 0.26–0.54 s |
| Memory | 57 MiB in a container; 94 MB after 1,100 calls as a process |

- A run's time is the agent's model calls, seconds each. A fake that answers in a millisecond is not on the critical path, and Rust would not shorten a run.
- Providers are where the work is, and they are written by Python-speaking agent builders and by coding agents. The nine existing fakes are Python and ran unchanged.
- What Rust would buy: one static binary with no runtime, and an image far smaller than 367 MB. Both matter for distribution, not for speed.
- The seam that keeps the door open: a provider speaks HTTP in and out (ASGI) and a store interface. The proxy and store behind that seam can be replaced without touching a provider.
- Revisit when a measurement says so: memory or latency with a large world, or adoption blocked by needing Python.

### Packages

| Package | Use | Note |
|---|---|---|
| `mitmproxy` 12.x (MIT) | Proxy, TLS interception, HTTP/2, pass-through and capture | Embedded through one addon; each provider is an ASGI or WSGI app served with `asgiapp.serve()`. Proven in the spike above. Its app host does not do WebSockets or streaming. |
| `pydantic` 2 | Every model | |
| `starlette` | Provider apps, control API | The nine existing fakes are Flask (WSGI) and can be mounted unchanged while each is ported. |
| `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | Telemetry | |
| `mcp` | MCP server | |
| `datamodel-code-generator` | Pydantic models for provider payloads from each provider's OpenAPI document | Asana publishes one (`Asana/openapi`); YouTrack serves one at `/api/openapi.json`. Slack's official spec repo is abandoned. The rest are unconfirmed. |
| `openapi-core` | Validate every fake's responses against the provider's published document | The conformance test that needs no real account. |
| `libfaketime` | Time for agents that cannot be told the time | Optional, unproven, see above. |
| `duckdb` | Questions across many run files | Optional. |
| `uv`, `ruff`, `pyright`, `pytest` | Tooling | |

Codebases worth reading before writing a provider: `vercel-labs/emulate` (Apache-2.0; Slack, Google, GitHub surfaces), LocalStack (lazy service loading), `moto` (one server, many services).

## Patterns

```python
class Pattern(Model):
    key: str
    title: str
    failure: str  # what the agent does wrong, in one sentence
    design: str  # what a proactive agent does instead
    reference: str | None
```

The first set, each taken from a mechanism that exists in alknoma-cloud:

| `Pattern.key` | Failure | Design | In Ayven |
|---|---|---|---|
| `expiry_on_every_wait` | Waits on something forever | Every wait carries an expected-by date and the agent wakes on it | `Blocker.expectedResolutionBy`, `nextStaleCheck` |
| `check_world_before_model` | Spends a model call to learn nothing changed | On waking, look at the world with plain code first; involve the model only when judgement is needed | `filter_actually_stale`, `stale_preflight` |
| `absence_aware` | Chases someone who is away | Know who is away and until when; extend the wait or go to their delegate | `_absence_prefilter` |
| `budgeted_follow_up` | Follows up too often, or too late | Space reminders across the time left before the deadline | `chase.py` |
| `bounded_asking` | Asks for input indefinitely | After a fixed number of attempts, stop asking and deliver the best available version | `_PIVOT_ESCALATION_THRESHOLD` |
| `one_open_ask_per_person` | Sends the same question twice | Track what is already open with each person before asking | `duplicate_judge.py` |
| `no_double_tick` | Does the weekly task twice | A recurring task has one instance per period | `_guard_cadence_tick_duplicate` |
| `honest_closure` | Reports done when it is not | Closing is decided from the state of the world, not from the agent's last message | `closure_evaluation.py` |
| `confirm_names` | Acts on a name it guessed | A name that matters is carried exactly as given, and an assumption is asked about before it is acted on | None. Run `f431fc97f427` shows Ayven lacks it. |

- Patterns are documentation in the repo, one page each, and data the tool returns. They are not code the user must import.
- The Ayven column is what makes them more than advice. Publishing the implementations they point to is the separate, heavier product.
- A scenario library graded from easy to hard, with a pass mark, is the form in which this becomes a standard others measure against. It is not designed yet.

## Queued behind a working emulator suite

### People written by a model

```python
class Person(Model):
    key: str
    name: str
    email: str
    title: str | None
    facts: list[str]  # all a model reply may draw on
    stale_facts: list[str]  # what they believe that is no longer true
    reply: ReplyBehaviour  # Answers | Scripted | Silent
    working_hours: WorkingHours | None
    absences: list[Absence]


class Answers(Model):
    delay: DelayRange
    helpfulness: Helpfulness  # FULL | PARTIAL | ASKS_BACK | DECLINES | MISTAKEN
    voice: str | None  # terse, formal, chatty
    model: str | None
    temperature: float = 0.6
```

- Each knob changes what the agent has to cope with: `PARTIAL` and `ASKS_BACK` force a second round, `DECLINES` forces a reroute, `MISTAKEN` plus `stale_facts` tests whether the agent verifies, `working_hours` and `absences` test timing.
- The replier's first decision is whether a message needs an answer at all. That decision is what opens an `Obligation`.
- A reply is stored the first time it is written. `rerun_from` replays it, so a rerun costs no model call for the people and plays out the same.
- Replies arrive through the provider as real inbound events.

### Checks that need a model

"Does this ticket make sense to the person it was assigned to" cannot be computed. A judged check implements the same `Check` protocol and returns the same `Finding`, with three differences:

- `FindingKind.REVIEW` unless the scenario sets a pass rate over several samples.
- The model, its prompt version and its rationale are recorded with the finding.
- It runs after the deterministic checks and only on entities they passed, so a ticket already failed for a wrong name is not judged for clarity.

Every finding, judged or not, becomes one OpenTelemetry log record linked to the span of the entity it is about, so "tickets judged unclear, by scenario, across ten thousand runs" is a query in whatever store receives the telemetry.

## Build tracks

One thing first, then eleven in parallel, then three that need the others.

| Order | Track | Needs |
|---|---|---|
| First, alone | **Contracts**: the models in `domain/`, the `Provider` and store interfaces, the control API | Nothing. Everything below codes against these, so parallel work before they settle collides. |
| Parallel | **Proxy runtime**: host routing, lazy load, CA, pass-through policy, base-URL mode | Contracts |
| Parallel | **Store**: schema, events, checkpoints, blobs | Contracts |
| Parallel | **Clock and orchestrator**: `next_jump`, wake sources, the run loop, `rerun_from` | Contracts |
| Parallel ×4 | **Providers**: Slack, YouTrack, Asana, Drive. Each: mount the existing fake, move its state to the store, take the clock, normalise, seed, port its refusal tests | Contracts. Independent of each other. |
| Parallel | **Scheduler provider** (`Booked`) | Contracts |
| Parallel | **Checks and the obligations ledger** | Contracts. Can be built against the 21 captured runs today. |
| Parallel | **Telemetry export** | Contracts |
| Parallel | **Surfaces**: CLI, MCP, viewer | Contracts; the store for real data |
| Then | **alknoma-cloud adoption**: one container in compose and CI, the adapter, the deletions | Proxy runtime, the four providers |
| Then | **People written by a model** | Orchestrator, Slack provider |
| Then | **Generated providers** (the `GENERATED` engine), then **judged checks** | Store, checks |

The four providers are the widest fan-out and the only fully independent work. The other parallel tracks share the store and the run loop, so they settle those interfaces between them early. The remaining five fakes (Jira, Teams, Graph, Notion, GitHub) are five more parallel pieces once the first four show the pattern.

## Licence

Decided 2026-10-04: the licence must stop anyone else selling Minutehand as a hosted service, because Alknoma will.

| Licence | Stops a competing hosted service | Still "open source" by the OSI definition | Used by |
|---|---|---|---|
| **Functional Source License (FSL-1.1-ALv2)** | Yes: any use except a competing product | No, "fair source". Each release becomes Apache-2.0 two years after it ships. | Sentry, Codecov, Liquibase, Convex, PowerSync, GitButler |
| Elastic License 2.0 | Yes: may not be offered as a managed service | No. Never converts. | Elastic, Arize Phoenix, Nango |
| Business Source License | Yes, with terms each vendor writes | No. Converts after a delay the vendor picks (four years at HashiCorp). | MariaDB, HashiCorp |
| AGPL-3.0 | No. Hosting is allowed; changes must be published. | Yes | Grafana, k6 |
| Apache-2.0 | No | Yes | LocalStack before 2026, vercel-labs/emulate |

Proposed: FSL-1.1-ALv2 for the tool from its first commit. Starting under it avoids the relicensing that drew forks at HashiCorp, Redis and LocalStack. The cost: it cannot be called open source, only source-available or fair source, and some companies' policies admit OSI licences only. Confirm with a lawyer before the first public commit.

## Testing

- A check ships with the run it was written for and a test that the check goes clean when the defect is removed from that run (`tests/test_against_captured_run.py`).
- A provider ships with refusal tests ("refuses what the real API refuses"), ported from the nine suites in alknoma-cloud, plus response validation against the provider's OpenAPI document where one exists.
- `python -m lints` runs every lint in `lints/`. See `docs/lints.md`.
- Conformance against the real APIs needs real accounts and is a scheduled job, not a lint.

## Known issues / limitations

- **The agent under test is a model, and its variance is reported, not hidden.** One run fails the build on any failed check. `--samples N` runs the scenario N times and reports `Stability(samples, passed)`: "passes 3 of 5" is the finding.
- **`repeated_message` is imprecise** (one true flag of three on the reference run).
- **The obligations ledger is designed, not built.** Whether "this message needs an answer" can be decided reliably by the replier is unproven.
- **Performance is measured only at spike scale:** two providers, five calls. Nothing is known about memory or speed with thousands of entities or concurrent runs.
- **Out of scope:** browser OAuth flows, certificate-pinned clients, Slack Socket Mode, reading back from real providers in production, the hosted service.

## References

- `docs/lints.md`: the lints proposed for this repo.
- `docs/adopting-in-alknoma-cloud.md`: how alknoma-cloud consumes this and what must keep working.
- alknoma-cloud `docs/features/field-observation/design.md`, `.claude/rules/lints.md`.
- Reference run page: https://claude.ai/artifact/AEpfatMwhGw2rcs7D428S7
