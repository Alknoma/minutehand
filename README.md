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

## Develop

```bash
uv sync
uv run pytest -q
uv run pyright
uv run python -m lints
```

`docs/design.md` is the design. `CLAUDE.md` is the house rules. `docs/lints.md` argues each lint.

Licensed under the Functional Source License 1.1 (Apache-2.0 future licence); see `LICENSE.md`.
