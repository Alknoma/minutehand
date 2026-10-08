# Recipes: an agent per framework, wired into Minutehand

The same small proactive agent, written five times in the idiom of five frameworks, so that wiring yours in is
copying twenty lines rather than working the contract out:

| Folder | Framework | Language | Model API it calls |
|---|---|---|---|
| `langgraph/` | LangGraph, with `langchain-openai` | Python | chat completions |
| `openai_agents/` | OpenAI Agents SDK | Python | chat completions |
| `claude_agent_sdk/` | Claude Agent SDK (`claude-agent-sdk`) | Python | messages |
| `pydantic_ai/` | Pydantic AI | Python | chat completions |
| `vercel_ai_sdk/` | Vercel AI SDK (`ai`, `@ai-sdk/openai`) | TypeScript on Node | chat completions |

Each agent takes its goal on the first wake ("confirm the venue for the team offsite with Rosa"), asks Rosa in
Slack through a tool built on the stock Slack client (`slack_sdk`, `@slack/web-api`), and remembers the wait with
the date it expects her answer by. After every wake it reports that date as `next_wake`. If the date passes with no
answer it follows up once and remembers a new date; if that one passes too, it tells Owen, who gave it the goal, and
closes the wait. When Rosa answers it thanks her, tells Owen what she said, and reports that it is done.

| File in each folder | What it is |
|---|---|
| `agent.py` / `agent.ts` | The agent: the framework's agent and tools, and three endpoints for Minutehand |
| `agent.yaml` | Where Minutehand reaches it: the wake and report endpoints, and where Slack pushes messages |
| `scenario_late.yaml` | Rosa answers two and a half days after she is asked, after the agent's follow-up at two days |
| `scenario_silent.yaml` | Rosa never answers |
| `README.md` | The lines that connect this framework to Minutehand, and the commands |

## The contract, once

Every recipe answers the same three calls (`docs/agent-contract.md`, the Reported wake source):

```
POST /wake          {"now": ..., "reason": "start" | "due" | ..., "goal": ...}   it is now `now`: do what is due
GET  /report        {"status": "idle" | "done", "next_wake": ISO 8601 or null}   asked after every wake
POST /slack/events  Slack's Events API, signed: a person wrote to the agent
```

and keeps the same shape of state: the goal, and for each person who owes an answer, the moment it is expected by.
`next_wake` is the earliest of those moments; `done` is a goal taken and no wait left.

## What they remember, and where

Each keeps that state in `minutehand.agent.store` (the Node recipe in `minutehand-store.ts`, its twin over the same
wire), never in the framework's own objects past a wake: the goal under `goal`, each wait under its person's email in
the collection `waits`. Each reads it back before every wake, Slack event and report, and writes it back in one batch
after every wake and event.

| Recipe | In production | Under Minutehand |
|---|---|---|
| `langgraph/` | `store.MemoryBackend()`; the graph runs with no checkpointer, from the store | the run's memory |
| `openai_agents/` | `store.MemoryBackend()`, recalled into the run context | the run's memory |
| `claude_agent_sdk/` | `store.MemoryBackend()`, recalled into the `Memory` the tools close over | the run's memory |
| `pydantic_ai/` | `store.MemoryBackend()`, recalled into the deps | the run's memory |
| `vercel_ai_sdk/` | a `Map` in the process | the run's memory, over `POST {MINUTEHAND_AGENT_URL}/store` |

`MemoryBackend()` keeps production as it was, in the process; `store.SqliteBackend(path)` keeps it across restarts.
Under Minutehand every run starts from the scenario's memory, and a fork from any checkpoint starts from what the
agent remembered there, with nothing to snapshot or restore: `tests/recipes` forks each Python recipe after its
first wake and checks its report there is the one it gave its parent. The agent never reads the machine's clock: its time is the wake's `now`, or a Slack event's timestamp.

Slack calls go to `slack.com` as in production. Minutehand starts the agent with `HTTPS_PROXY` and its CA in the
environment, and the Python Slack client follows them to the fake Slack. The Node client calls Node's own `fetch`,
which ignores `HTTPS_PROXY`, so that recipe is handed Slack's base URL instead (`base_urls` in its `agent.yaml`).

## The model, offline

`fake_model.py` stands in for both model APIs: `POST /v1/chat/completions` and `POST /v1/messages` (plain or
streamed). It is a few rules, not a model: it reads the situation each agent writes ("It is now …", "No answer yet
from …, expected by …. Follow-ups sent: 0.") and answers with the tool calls a careful agent would make, so every run
gives the same answer and needs no network and no key. It uses only the standard library.

Each agent is pointed at it through its framework's own base-URL setting (`MODEL_BASE_URL`, or
`ANTHROPIC_BASE_URL` for the Claude Agent SDK). The fake listens on `127.0.0.1`, which is in the `NO_PROXY`
Minutehand hands the agent, so the model calls go straight to it and Minutehand neither sees nor records them.

With a real model you drop the base URL, and the calls go to `api.openai.com` or `api.anthropic.com`. Those are
model hosts Minutehand knows: it tunnels them through untouched by default, records each call with
`--record-model-calls`, and takes another host, such as your own model gateway, with `--model-host HOST`
(`docs/design.md`, "Hosts the proxy does not own"). A run against a real model is neither offline nor repeatable.

## Running one

From a recipe's folder, in a checkout with [uv](https://docs.astral.sh/uv/):

```bash
python ../fake_model.py &                # the fake model, on 127.0.0.1:8790

uv run --group recipes minutehand run scenario_late.yaml --agent agent.yaml -- python agent.py
uv run --group recipes minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
```

Each recipe's README gives its own command. Both scenarios exit 0. The silent one ends like this:

```
run 6857256e8014: recipe_venue_silent
  Passed: no check failed, and the agent reported it was done.
  stopped at 2026-08-28 09:00 UTC (simulated) because the agent reported it was done
...
scorecard
  expectations met: 1 of 1
  waits opened: 1, still open at the end: 1
  follow-ups made: 1
```

Rosa's wait is still open at the end, as Minutehand counts it: she never answered. The agent closed its side by
telling Owen. Whether one follow-up two days in was right is the scenario's rules' to say; the recipes' scenarios
ask for a follow-up once her answer is due.

Leave the agent's follow-up out (have `/report` answer `next_wake: null`) and the silent run fails the scenario's
rule `follows_up_when_due`, naming the fix: an expected-by date on every wait, and a wake on it.

`tests/recipes/test_recipes.py` runs every recipe through both scenarios (`uv run --group recipes pytest -m recipes`).

## Versions

The `recipes` group in `pyproject.toml` installs the Python frameworks into Minutehand's own environment, which is
a convenience for the tests; in your project they live in the agent's environment and Minutehand in its own. On
Python 3.12, beside Minutehand's pin of mitmproxy, it resolves to openai-agents 0.4 and openai below 2.45. The Node
recipe's versions are in `vercel_ai_sdk/package-lock.json`.
