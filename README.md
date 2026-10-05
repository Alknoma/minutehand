# Minutehand

Build your proactive agent yourself. Minutehand runs it through two simulated weeks in a few minutes, against fake Slack, trackers and document stores with people who answer late or not at all, measures how well it carried the work, and names the design that fixes each thing it got wrong.

The project under test changes by zero lines: its outbound calls are intercepted, and the clock is Minutehand's.

## State

| Part | State |
|---|---|
| Models and contracts (`src/minutehand/domain`, `src/minutehand/ports`) | Written |
| The world as an append-only log with forks (`adapters/store/sqlite.py`) | Written, tested |
| Checks: `near_miss_name`, `repeated_message`, `expectations`, the `effectiveness` scorecard | Written, tested on a real captured run |
| Proxy, providers, orchestrator, people, telemetry, command line, MCP, viewer | Not built; see [`docs/design.md`](https://github.com/Alknoma/minutehand/blob/integration-main/docs/design.md) |

## Install

```bash
pip install minutehand                # the `minutehand` command and the pytest plugin
uvx minutehand serve                  # or without installing: a standing proxy and control API for a test suite
```

For an agent in any other language, the image serves the same: it is driven through proxy variables and an
HTTP control API ([`docs/serve.md`](https://github.com/Alknoma/minutehand/blob/integration-main/docs/serve.md)), never imported.

```bash
docker run --rm -p 8080:8080 -p 8081:8081 -p 4318:4318 ghcr.io/alknoma/minutehand
```

## Quick start

The example lives in the repository. From a checkout, with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install .                     # the `minutehand` command, in an environment of its own
cd examples/follow_up
pip install slack_sdk                 # the example agent's one library, in the agent's own Python

minutehand run scenario.yaml --agent agent.yaml -- python agent.py
# exits 0: Rosa answers in a day and a half and the agent finishes

AGENT_BEHAVIOUR=forgetful minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
# exits 1: Rosa never answers, the agent never follows up, and the no_follow_up finding names the fix
```

Or in a container, with the example mounted and its agent started inside:

```bash
docker build --target example -t minutehand-example .
docker run --rm -v "$PWD/examples:/examples:ro" -w /examples/follow_up \
  minutehand-example run scenario.yaml --agent agent.yaml -- python agent.py
```

`docker build -t minutehand .` builds the image without the example's library. [`examples/follow_up/README.md`](https://github.com/Alknoma/minutehand/blob/integration-main/examples/follow_up/README.md)
walks through both runs.

## Develop

```bash
uv sync
uv run pytest -q
uv run pyright
uv run python -m lints
```

[`docs/design.md`](https://github.com/Alknoma/minutehand/blob/integration-main/docs/design.md) is the design. [`CLAUDE.md`](https://github.com/Alknoma/minutehand/blob/integration-main/CLAUDE.md) is the house rules. [`docs/lints.md`](https://github.com/Alknoma/minutehand/blob/integration-main/docs/lints.md) argues each lint.

Licensed under the Functional Source License 1.1 (Apache-2.0 future licence); see [`LICENSE.md`](https://github.com/Alknoma/minutehand/blob/integration-main/LICENSE.md).
