```
 __  __   ___   _   _   _   _   _____   _____   _   _      _      _   _   ____
|  \/  | |_ _| | \ | | | | | | |_   _| | ____| | | | |    / \    | \ | | |  _ \
| |\/| |  | |  |  \| | | | | |   | |   |  _|   | |_| |   / _ \   |  \| | | | | |
| |  | |  | |  | |\  | | |_| |   | |   | |___  |  _  |  / ___ \  | |\  | | |_| |
|_|  |_| |___| |_| \_|  \___/    |_|   |_____| |_| |_| /_/   \_\ |_| \_| |____/

     Turning days or months of agentic behaviour tuning work into minutes.
```

**Months of your proactive agent's work, in minutes.** A world of fake Slack, Teams, Jira, Asana, Notion, GitHub and
Google Drive, with people who answer late, go away, or never answer. Minutehand owns the clock.

**Findings, not opinions.** Every message, ticket, document and calendar event the agent touches is judged as it
happens against the agent's own instructions and the world you declared: too early, too late, again with nothing new,
a move the service refuses, a fact it made up. Each finding names what it was held to.

**One import, inert in production.** `minutehand.agent` does nothing unless `MINUTEHAND_ON` is set. The rest is
environment: `HTTPS_PROXY`, `NO_PROXY`, a CA bundle.

**Every run is a record.** Plain SQL over every call, message, memory write and model call. Replay a run from its
record; fork it from any checkpoint with the prompt, the model, a person or the world changed, and play it forward.

A proactive agent brings its own work, decides when it next wakes, and keeps going; nothing hands it a task and
nothing wakes it on a schedule. That is the agent Minutehand is for.

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

`examples/proactive_agent` is a small proactive agent: it gets answers from people and passes them on. Its work is
its own file (`work.json`: a cost centre from Sam, for Owen); it asks in Slack, follows up at most twice, and tells
the answer on. Its `README.md` says how it decides when to wake.

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
```

Two simulated weeks pass in seconds: Sam never answers; the agent follows up a working day apart, twice, tells Owen,
and stops. Runs are kept in `.minutehand/` in the folder you ran them from.

```bash
minutehand runs                  # every run, one line each
minutehand findings <run_id>     # a run's findings, and the checkpoints it can be forked from
minutehand trace <run_id>        # what the agent did, in order
minutehand replay <run_id> -- .venv/bin/python agent.py   # the run again, its model's answers from the record
minutehand replay <run_id> --at <seq> --frozen -- .venv/bin/python agent.py   # from a checkpoint, with the agent as it
                                 # is now; nothing sent to a model, exit 7 at the first model call the record lacks
minutehand model-calls <run_id>  # the agent's model calls, as OpenAI Batch API lines
minutehand view                  # the runs in a browser, at http://127.0.0.1:8081/
```

## Your own agent

A run needs two files.

| File | What it says |
|---|---|
| Agent file | How Minutehand reaches the agent: how it is woken, the events it receives, its own systems to keep inside the run. `minutehand schema agent` prints its JSON Schema. |
| Scenario | The world: its people, each with a profile and what they know, the state its services start in, and how long to watch the agent (`runs_for`). Team rules (`assess:`) are optional. |

```bash
minutehand scenarios new --person 'Name <email>'   # ready-made worlds with your people in them (docs/scenarios.md)
minutehand validate agent.yaml world.yaml           # every load-time check, naming each problem
minutehand doctor -- <your agent's command>         # HTTP clients in the agent that would go around the proxy
```

In the agent's code, one import, `minutehand.agent`, which does nothing unless `MINUTEHAND_ON` is set:

```python
from minutehand.agent import store, wake

store.configure(store.SqliteBackend("agent.db"))  # production: your database, through a three-method adapter

store.put("asks/sam", {"status": "asked", "expected_by": expected_by.isoformat()})  # what it remembers
for key, ask in store.query("asks/", where={"status": "asked"}):
    ...
wake.at(expected_by)  # when it next wants to wake
```

In production the store passes through to your own database and nothing is recorded. Under Minutehand the store is
the run's: every write recorded, every read answered from it, a memory of its own per run and fork, and a fork starts
from the memory as it stood at its checkpoint.

**Only state written through the store is part of a run.** What the agent keeps anywhere else (its own database,
files, a cache, a process that outlives a wake) is not simulated, not kept apart between runs, and not rewound by a
fork. Minutehand reports what it can see of that, and refuses a fork when the agent's report after it is not the one
recorded at the checkpoint.

Nothing else is required. An agent can also take a wake (a `POST` carrying the simulated `now`), answer a report,
take pushed events in each provider's own format, and list its own inboxes for the simulated people to decide.
`docs/agent-contract.md` lists every touch point; `examples/recipes` shows the wiring in five agent frameworks.

## Reading a run

A run's findings say what went wrong; its read model says everything the agent did. Every run and fork is plain SQL
over documented views, bodies decoded.

| View | Holds |
|---|---|
| `actions` | every act of the agent, in order |
| `messages` | to whom, the words, whether it asked, followed up or answered |
| `calls` | every HTTP call with its request and answer |
| `memory` | the store over time |
| `model_calls` | the agent's model calls, with tokens |
| `wakes`, `replies`, `findings` | what woke the agent, what people said, what the checks said |

```bash
minutehand query <run> "SELECT seq, at, text FROM messages WHERE is_follow_up = 1"
minutehand trace <run> --person sofia      # the agent's acts in order
minutehand explain <run> <seq>             # what led to one event, and what followed
minutehand query --schema                  # every view and column
```

Over MCP (`minutehand mcp`) every command is a tool of the same name. `docs/querying.md` documents every column, how
the views stay stable, and a dozen ready-made queries.

## What is faked

| Kind | Services |
|---|---|
| Chat | Slack, Microsoft Teams and Graph |
| Work tracking | Asana, Jira, YouTrack, GitHub |
| Documents | Notion, Google Drive with Docs and Slides |
| Scheduling and queues | AWS EventBridge Scheduler and SQS (through moto), Google Cloud Tasks |

Each fake is tested against the service's own client library; none has been checked against the real service's wire
details. A host no fake answers is declared in the agent file: acknowledge, pass through, replay, or forward to an
emulator of your own (`docs/capture.md`, `docs/external-emulators.md`).

## Status

`docs/design.md`, "What exists", is the inventory: each part, its state, its tests and its known limits.

- **Built and tested:** the proxy, the run loop, the store, the agent's memory, forks, assessment of every effect
  the agent has on the world, a model reviewer for what only meaning can tell (`--judge`), `run-all` over a folder of
  scenarios, dispatch rules (a wake delivered late, twice or never), outbound capture, MCP recording, OpenTelemetry
  in and out, the viewer, `doctor`, `validate`, `schema`, and a container image.
- **Experimental:** `Contained`, an agent in a gVisor sandbox whose clock Minutehand owns. It needs a patched gVisor
  kept outside this repository and has been run on arm64 only.
- **Checked by hand, not in the test suite:** an agent in containers (`docs/containers.md`).
- **Not built:** generated providers, a faked system clock (`libfaketime`), the hosted service.

## Docs

| Page | What it covers |
|---|---|
| `docs/design.md` | The design, what exists, and its known limits |
| `docs/agent-contract.md` | Every way an agent and Minutehand touch |
| `docs/scenarios.md` | The scenario library: ready-made worlds with your people in them |
| `docs/assessments.md` | The rules a team judges its agent by |
| `docs/querying.md` | Reading a run with SQL: the views, their columns, the stability guarantee, ready-made queries |
| `docs/capture.md` | Hosts no fake answers: acknowledge, pass through, replay, `--capture-unknown` |
| `docs/external-emulators.md` | Forwarding a host to a fake of your own |
| `docs/containers.md` | An agent in a container, and the Docker `NO_PROXY` trap |
| `docs/serve.md` | `minutehand serve`, for a test suite |
| `docs/inboxes.md` | Work that waits on a person in the agent's own product |
| `docs/approvals.md` | An agent that waits for a person's sign-off |
| `docs/reference-agent.md` | A larger example: two processes, a job queue, email, a model API |
| `examples/recipes/README.md` | The same agent in five frameworks |

Five of the hard parts of a proactive agent, each answered in short on [minutehand.ai](https://minutehand.ai):
[why it sends the same message twice](https://minutehand.ai/answers/why-does-a-proactive-agent-send-the-same-message-twice),
[how it survives a restart](https://minutehand.ai/answers/how-does-a-proactive-agent-survive-a-restart),
[why it acts before an approval](https://minutehand.ai/answers/why-does-a-proactive-agent-act-before-an-approval),
[how to reproduce a failure](https://minutehand.ai/answers/how-do-you-reproduce-a-proactive-agent-failure) and
[why it is harder to test than a chatbot](https://minutehand.ai/answers/why-is-a-proactive-agent-harder-to-test-than-a-chatbot).

## Develop

```bash
uv sync
uv run pytest -q -n auto
uv run pyright
uv run python -m lints
uv run ruff check . && uv run ruff format --check .
```

`CLAUDE.md` is the house rules, `CONTRIBUTING.md` how to contribute, and `docs/lints.md` argues each lint.
`uv tool install .` installs the checkout's own `minutehand`.

## Licence

Apache License 2.0; see `LICENSE` and `NOTICE`.
