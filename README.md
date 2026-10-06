# Minutehand

Build your proactive agent yourself. Minutehand runs it through two simulated weeks in a few minutes, against fake Slack, trackers and document stores with people who answer late or not at all, measures how well it carried the work, and names the design that fixes each thing it got wrong.

The project under test changes by zero lines: its outbound calls are intercepted, and the clock is Minutehand's.

## State

| Part | State |
|---|---|
| Models and contracts (`src/minutehand/domain`, `src/minutehand/ports`) | Written |
| The world as an append-only log with forks (`adapters/store/sqlite.py`) | Written, tested |
| Checks: `near_miss_name`, `repeated_message`, `expectations`, the `effectiveness` scorecard | Written, tested on a real captured run |
| Proxy, providers, orchestrator, people, telemetry, command line, MCP, viewer | Not built; see `docs/design.md` |

## Quick start

From a checkout, with [uv](https://docs.astral.sh/uv/):

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

`docker build -t minutehand .` builds the image without the example's library. `examples/follow_up/README.md`
walks through both runs.

## Scenarios to start from

`minutehand scenarios` lists a library of ready-made situations (a person goes quiet, answers late, is away with a
delegate; an approval is rejected or never decided; a deadline moves; a scheduled wake comes late, twice or never).
`minutehand scenarios new --all --goal ... --owner 'Name <email>' --ask 'Name <email>'` writes each out with your
values. See `docs/scenarios.md`.

## Develop

```bash
uv sync
uv run pytest -q
uv run pyright
uv run python -m lints
```

`docs/design.md` is the design. `CLAUDE.md` is the house rules. `docs/lints.md` argues each lint.

Licensed under the Functional Source License 1.1 (Apache-2.0 future licence); see `LICENSE.md`.
