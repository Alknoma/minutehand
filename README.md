# Minutehand

Minutehand runs your proactive agent through months of simulated work in minutes and tells you how well it carried
that work. A proactive agent brings its own work (its prompt, and the state and items it sets), decides when it next
wakes, and keeps going: nothing hands it a task, and nothing wakes it on a schedule. Minutehand gives it a world
instead: fake Slack, Teams, Asana, Jira, YouTrack, Notion, GitHub, Google Drive and others, and people, each described
by who they are and what they know, who answer late, are away, or never answer. It owns the clock, records every change
in an append-only log, and assesses every message, ticket, document, calendar event and service item the agent
touches, as it happens, against the agent's own instructions and the world your scenario declares: too early, too
late, again with nothing new, a move the service refuses, a fact the agent made up. Nothing is measured against an
opinion of how agents should work, and each finding names what it was held to; policy the world cannot imply (how
often your team chases) is yours to add in YAML. A finished run can be forked from a checkpoint with the prompt, the
model, a person or the world changed, and played forward again.

Minutehand starts the agent's own command, or reaches one already running, and points it at the fakes through its
environment: `HTTPS_PROXY`, `NO_PROXY` and a CA bundle. Minutehand owns the agent's time and its memory, so the one
change to the agent's code is one import, inert in production: what the agent remembers goes through
`minutehand.agent.store`, and that is what a fork rewinds.

## Install

Minutehand is on PyPI. With [uv](https://docs.astral.sh/uv/), which fetches Python 3.12 if you do not have it:

```bash
uv tool install minutehand        # the `minutehand` command, in an environment of its own
uvx minutehand --help             # or run it without installing
```

or with pip, in an environment apart from your agent's dependencies: `pip install minutehand`.

`main` is the released branch: each release on PyPI is a commit of `main`. Development lands on `integration-main`
first; `uv tool install git+https://github.com/Alknoma/minutehand@integration-main` installs what is not released
yet.

## Quick start

The example is a small proactive agent (`examples/proactive_agent`): it needs a cost centre from Sam, asks him in
Slack, follows up at most twice, and tells Owen. Its one decision about time, when to wake next, is its own small
package (`wake/`): a Pydantic AI sub-agent proposes the moment, and plain code holds it to the rules (never before an
answer can be due, never in the past, always in working hours, never no wake while something is open). Clone the
repository for its files:

```bash
git clone --depth 1 -b main https://github.com/Alknoma/minutehand
cd minutehand/examples/proactive_agent
python3 -m venv .venv && .venv/bin/pip install minutehand slack_sdk pydantic-ai-slim[openai]

# Offline: a stand-in plays Sam and Owen, and AGENT_MODEL=test leaves each wake to the guard.
# With accounts, set MINUTEHAND_MODEL* for the people, and AGENT_MODEL=openai:<model> with OPENAI_API_KEY.
.venv/bin/python ../recipes/fake_model.py &
export MINUTEHAND_MODEL_BASE_URL=http://127.0.0.1:8790/v1 MINUTEHAND_MODEL=people MINUTEHAND_MODEL_API_KEY=offline
export AGENT_MODEL=test

minutehand run worlds/quiet.yaml --agent agent.yaml -- .venv/bin/python agent.py
# two simulated weeks in seconds: Sam never answers; the agent follows up a working day apart, twice, tells Owen,
# and stops

minutehand runs                  # every run, one line each
minutehand findings <run_id>     # a run's findings again, and the checkpoints it can be forked from
minutehand trace <run_id>        # what the agent did, in order
minutehand view                  # the runs in a browser, at http://127.0.0.1:8081/
```

Each run takes a few seconds. Runs are kept in `.minutehand/` in the folder you ran them from. The example's
`README.md` says how the agent decides when to wake.

To try your own agent, write two files. The agent file says how Minutehand reaches it: how it is woken, the events
it receives, its own systems to keep inside the run (`minutehand schema agent` prints its JSON Schema). The scenario
is the world: its people, each with a profile and what they know, the state its services start in, and how long to
watch the agent (`runs_for`). That is all a run needs to be assessed (`docs/assessments.md`); rules of your team's own
(`assess:`) are optional, for policy the world cannot imply. `minutehand scenarios new --person 'Name <email>'`
writes ready-made worlds with your people in them (`docs/scenarios.md`). Check the files with `minutehand validate`,
and run `minutehand doctor -- <your agent's command>` to find any HTTP client in the agent that would go around the
proxy. `examples/recipes` shows the wiring in five agent frameworks.

## How the agent touches Minutehand

In its code, through one import, `minutehand.agent`, which does nothing unless `MINUTEHAND_ON` is set:

```python
from minutehand.agent import store, wake

store.configure(store.SqliteBackend("agent.db"))  # production: your database, through a three-method adapter

store.put("asks/sam", {"status": "asked", "expected_by": expected_by.isoformat()})  # what it remembers
for key, ask in store.query("asks/", where={"status": "asked"}):
    ...
wake.at(expected_by)  # when it next wants to wake
```

In production the store is a pass-through to your own database (SQLite and in-memory adapters ship; any other is
three methods) and nothing is recorded. Under Minutehand the store is the run's: every write is recorded in the run,
every read answered from it, your database is never opened, each run and fork has a memory of its own seeded from
the scenario's `memory:`, and a fork starts from the memory as it stood at its checkpoint with nothing restored.

**Only state written through the store is part of a run.** What the agent keeps anywhere else (its own database,
files, a cache, a process that outlives a wake) is not simulated, not kept apart between runs, and not rewound by a
fork, so its later calls can depend on state from another moment or another run. Minutehand reports what it can see
of that: the store's reads and writes in every wake, a fresh empty SQLite file per run for each database the agent
file names (`own_databases`), noted as outside forks, and a fork refused when the agent's report after it is not the
one recorded at the checkpoint.

Beside that, nothing is required. An agent that books its own wakes on its own scheduler, and hears from people
through the providers' own pushes, implements no endpoint. An agent can also take a wake (a `POST` carrying the
simulated `now`), answer a report (still working, idle, and the moment it decides to be woken next), take pushed events in each provider's own format, and list its own inboxes for the
simulated people to decide. Hosts no fake answers are declared in the agent file: acknowledge, pass through,
replay, or forward to an emulator of your own. `docs/agent-contract.md` lists every touch point, and
`schemas/agent-api.openapi.json` describes the endpoints.

## Reading a run

A run's findings say what went wrong; its read model says everything the agent did. Every run (and every fork) can
be read with plain SQL over documented views, bodies decoded: `actions` (every act of the agent, in order),
`messages` (to whom, the words, whether it asked, followed up or answered), `calls` (every HTTP call with its
request and answer), `memory` over time, `model_calls` with tokens, `wakes` and what woke them, `replies`, `findings`.

```bash
minutehand query <run> "SELECT seq, at, text FROM messages WHERE is_follow_up = 1"
minutehand trace <run> --person sofia      # the agent's acts in order
minutehand explain <run> <seq>             # what led to one event, and what followed
minutehand query --schema                  # every view and column
```

The same are MCP tools (`query_run`, `schema`, `trace`, `explain`). `docs/querying.md` documents every column, how
the views stay stable, and a dozen ready-made queries.

## Status

Built and tested (`docs/design.md`, "What exists", counts the tests for each part):

- The proxy, the run loop, the store (SQLite, one file per run and its forks), the agent's memory
  (`minutehand.agent.store`) held by the run, forks from a checkpoint that start from that memory and verify the
  agent's report, and `minutehand serve` for test suites that open many worlds at once.
- Providers: Slack, Microsoft Teams and Graph, Asana, Jira, YouTrack, Notion, GitHub, Google Drive with Docs and Slides,
  AWS EventBridge Scheduler and SQS (through moto), and Google Cloud Tasks over its REST transport.
- Assessment, automatic: every effect the agent has on the world, by kind of item, as it happens, against the
  agent's own instructions (read from its recorded model calls) and the world the scenario declares; a model reviewer
  for what only meaning can tell (`--judge`). Simulation health apart: a world that failed to play as declared is never
  counted against the agent. Optional team rules in YAML (`assess:`) for policy the world cannot imply.
- Protected names, and, for older scenarios, what must be true at the end (`expect:`). The run's integrity (calls that
  went around the proxy, an agent answering against its own API description, a call nothing answered) is stated for
  review and fails a run only when the agent file or the scenario names it in `fail_on_integrity`. The scorecard
  counts facts only.
- Checks of the agent's own in Python, kept beside its agent file (`checks:`), reading the same facts
  (`minutehand.checks.facts`).
- `minutehand run-all <folder>` plays every scenario of a folder in parallel, each with its own port and folder for
  the agent, and exits 1 when a verdict differs from the scenario's `expect_outcome`.
- Dispatch rules: a scenario can deliver the agent's own wakes late, twice or not at all, and a fork can change
  them (`DispatchChange`).
- Outbound capture, with `--capture-unknown` for a first run: `reads` passes only GET, HEAD and OPTIONS, and
  `model` lets a model stand in for a service nobody declared once the agent writes to it.
- The agent's machine: `machine:` commands in a scenario change files at a moment, and the folders an agent file
  `watches:` are recorded as they change. The `file_removed` expectation reads them.
- MCP: an agent's tool calls are recorded over HTTP through the proxy, and over standard input and output with
  `minutehand mcp-relay`. The `tool_called` expectation reads them. `minutehand mcp` serves the tools a coding
  agent uses to run scenarios and read findings.
- The agent's own OpenTelemetry received and joined to the world events it caused; telemetry out over OTLP.
- The run viewer (`minutehand view`), `doctor`, `validate` and `schema`, and a container image (`Dockerfile`)
  that serves by default.

Experimental: `Contained`, an agent in a gVisor sandbox whose clock Minutehand owns, so timers in the agent's own
process become its wakes with no code change. It needs a patched gVisor that is kept outside this repository,
and has been run on arm64 only.

Checked by hand, not in the test suite: an agent in containers (`docs/containers.md`).

Not built: generated providers, a faked system clock (`libfaketime`), the hosted service. No fake has been checked against the real service's wire details; each is
tested against the service's own client library. `docs/design.md`, "Known issues", lists the limits of what is
built.

## Docs

| Page | What it covers |
|---|---|
| `docs/design.md` | The design, what exists, and its known limits |
| `docs/agent-contract.md` | Every way an agent and Minutehand touch |
| `docs/assessments.md` | The rules a team judges its agent by |
| `docs/querying.md` | Reading a run with SQL: the views, their columns, the stability guarantee, ready-made queries |
| `docs/capture.md` | Hosts no fake answers: acknowledge, pass through, replay, `--capture-unknown` |
| `docs/containers.md` | An agent in a container, and the Docker `NO_PROXY` trap |
| `docs/serve.md` | `minutehand serve`, for a test suite |
| `docs/external-emulators.md` | Forwarding a host to a fake of your own |
| `docs/inboxes.md` | Work that waits on a person in the agent's own product |
| `docs/approvals.md` | An agent that waits for a person's sign-off: every place an approval lives, the approver's script, the rules, forks and samples, and the gaps |
| `docs/reference-agent.md` | A larger example: two processes, a job queue, email, a model API |

## Scenarios to start from

`minutehand scenarios` lists a library of ready-made worlds (a person goes quiet, answers late, is away with a
delegate; an approval is rejected or never decided; a date moves earlier; a wake the agent planned comes late, twice
or never). `minutehand scenarios new --all --person 'Name <email>' --other 'Name <email>'` writes each out with your
people; each hands the agent no work, and every run of one is assessed against the agent's own instructions and the
world it declares. See `docs/scenarios.md`.

## Develop

From a checkout:

```bash
uv sync
uv run pytest -q -n auto
uv run pyright
uv run python -m lints
uv run ruff check . && uv run ruff format --check .
```

`CLAUDE.md` is the house rules, `CONTRIBUTING.md` how to contribute, and `docs/lints.md` argues each lint.
`uv tool install .` installs the checkout's own `minutehand`.

Licensed under the Functional Source License 1.1 (Apache-2.0 future licence); see `LICENSE.md`.
