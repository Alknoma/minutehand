# Minutehand

Minutehand runs your proactive agent through simulated days of work in a few seconds and tells you how well it
carried the work. The agent talks to fake Slack, Teams, Asana, Jira, YouTrack, Notion, GitHub, Google Drive and others,
with people who answer late or not at all. Minutehand owns the clock, records every change in an append-only
log, and judges the result by the rules your team writes in YAML: when to follow up, how often, when to escalate
and to whom. Minutehand holds no opinion of its own about how an agent should behave; a run with no rules reports
what happened and says nothing was assessed. A failure can name the design that fixes it. A finished run can be forked
from a checkpoint with the prompt, the model, a person or the world changed, and played forward again.

Minutehand starts the agent's own command, or reaches one already running, and points it at the fakes through its
environment: `HTTPS_PROXY`, `NO_PROXY` and a CA bundle. Minutehand owns the agent's time and its memory, so the one
change to the agent's code is one import, inert in production: what the agent remembers goes through
`minutehand_agent.store`, and that is what a fork rewinds.

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

The example agent is in the repository, so clone it for the example files:

```bash
git clone --depth 1 -b main https://github.com/Alknoma/minutehand
cd minutehand/examples/follow_up
python3 -m venv .venv && .venv/bin/pip install slack_sdk minutehand-agent   # its Slack client and `minutehand_agent`

minutehand run scenario.yaml --agent agent.yaml -- .venv/bin/python agent.py
# exits 0: Rosa answers after a day and a half, and the agent tells Owen and finishes

AGENT_BEHAVIOUR=forgetful minutehand run scenario_silent.yaml --agent agent.yaml -- .venv/bin/python agent.py
# exits 1: Rosa never answers, the agent never follows up, and the scenario's rule follows_up_when_due names the fix

minutehand runs                  # every run, one line each
minutehand findings <run_id>     # a run's findings again, and the checkpoints it can be forked from
minutehand view                  # the runs in a browser, at http://127.0.0.1:8081/
```

Each run takes a few seconds. Runs are kept in `.minutehand/` in the folder you ran them from. The example's
`README.md` explains both runs line by line.

To try your own agent, write an agent file (`minutehand schema agent` prints its JSON Schema), a scenario, and the
rules your team judges the agent by (`assess:`, `docs/assessments.md`), check them with `minutehand validate`, and run `minutehand doctor -- <your agent's command>` to find any HTTP
client in the agent that would go around the proxy.

## How the agent touches Minutehand

In its code, through one import, `minutehand_agent`, which does nothing unless `MINUTEHAND_ON` is set. It is a
distribution of its own, the standard library only, installed in the agent's environment (`pip install
minutehand-agent`), never `minutehand` itself:

```python
from minutehand_agent import store, wake

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

Beside that, nothing is required. An agent that takes its goal by message and books its own wakes implements no
endpoint. An agent can also take a wake (a `POST` carrying the simulated `now`), answer a report (still working,
done, when to wake next), take pushed events in each provider's own format, and list its own inboxes for the
simulated people to decide. Hosts no fake answers are declared in the agent file: acknowledge, pass through,
replay, or forward to an emulator of your own. `docs/agent-contract.md` lists every touch point, and
`schemas/agent-api.openapi.json` describes the endpoints.

## Status

Built and tested (`docs/design.md`, "What exists", counts the tests for each part):

- The proxy, the run loop, the store (SQLite, one file per run and its forks), the agent's memory
  (`minutehand_agent.store`) held by the run, forks from a checkpoint that start from that memory and verify the
  agent's report, and `minutehand serve` for test suites that open many worlds at once.
- Providers: Slack, Microsoft Teams and Graph, Asana, Jira, YouTrack, Notion, GitHub, Google Drive with Docs and Slides,
  AWS EventBridge Scheduler and SQS (through moto), and Google Cloud Tasks over its REST transport.
- Assessments: the team's own rules, in YAML in the agent file and the scenario (`assess:`), counting the facts of
  a run (follow-ups, messages, writes, wakes, the wakes the agent planned, what it reported, the keys of its memory) between moments
  (`ask+P1D`, `answer`, `due`, `deadline`) against bounds. `docs/assessments.md` writes a real agent's policy whole,
  and the fourteen behaviours Minutehand once judged by itself as rules a team may copy.
- What the scenario says must be true at the end (`expect:`), protected names, and the run's integrity (calls that
  went around the proxy, an agent answering against its own API description). The scorecard counts facts only.
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

Not built: people written by a model, checks a model judges, generated providers, a faked system clock
(`libfaketime`), the hosted service. No fake has been checked against the real service's wire details; each is
tested against the service's own client library. `docs/design.md`, "Known issues", lists the limits of what is
built.

## Docs

| Page | What it covers |
|---|---|
| `docs/design.md` | The design, what exists, and its known limits |
| `docs/agent-contract.md` | Every way an agent and Minutehand touch |
| `docs/assessments.md` | The rules a team judges its agent by |
| `docs/capture.md` | Hosts no fake answers: acknowledge, pass through, replay, `--capture-unknown` |
| `docs/containers.md` | An agent in a container, and the Docker `NO_PROXY` trap |
| `docs/serve.md` | `minutehand serve`, for a test suite |
| `docs/external-emulators.md` | Forwarding a host to a fake of your own |
| `docs/inboxes.md` | Work that waits on a person in the agent's own product |
| `docs/reference-agent.md` | A larger example: two processes, a job queue, email, a model API |

## Scenarios to start from

`minutehand scenarios` lists a library of ready-made situations (a person goes quiet, answers late, is away with a
delegate; an approval is rejected or never decided; a deadline moves; a scheduled wake comes late, twice or never).
`minutehand scenarios new --all --goal ... --owner 'Name <email>' --ask 'Name <email>'` writes each out with your
values, and with the rules that judge it in its `assess:`, yours to edit. See `docs/scenarios.md`.

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
