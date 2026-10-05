# Rewind an agent whose state is a SQLite file

The follow-up agent of `examples/follow_up`, with everything it knows in `agent.db` instead of in memory: the
job (status, next wake, whether it followed up, Rosa's answer) and what it is waiting on (a commitment, open
until Rosa answers). Its report is read from the file on every request.

Tested end to end in the default suite: `tests/state/test_sqlite_recipe.py`.

| File | What it is |
|---|---|
| `agent.py` | The agent. Needs `slack_sdk`. `AGENT_DB` names the file (default `agent.db`). |
| `hooks.py` | `snapshot` and `restore`: SQLite's online backup, into and out of `$MINUTEHAND_SNAPSHOT_DIR`. |
| `agent.yaml` | Where Minutehand reaches it, and its `state:` hooks. |
| `scenario_silent.yaml` | Rosa never answers. |
| `fork_rosa_answers.yaml` | A fork in which she answers after a day and a half. |

## 1. Run it

From this folder:

```bash
minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
```

Rosa never answers, so the run fails `expectations`. The end of the report lists every checkpoint and
whether the agent's state there can be put back:

```
checkpoints
  seq 14, after wake 0: restorable
  seq 18, after wake 1: restorable
  seq 22, after wake 2: restorable
  seq 23, after wake 3: restorable
  seq 24, after wake 3: restorable
```

A checkpoint is restorable when the agent had settled: it reported it was not working, and made no Slack call
for `quiet` (half a second here). Then `hooks.py snapshot` copied the file, and the agent's report at that
moment was recorded with the checkpoint.

## 2. Fork it

```bash
minutehand fork <run_id> --at 18 --changes fork_rosa_answers.yaml -- python agent.py
```

Each step is named as it is taken:

```
minutehand fork: restore stop: stopping the agent's command, python agent.py
minutehand fork: restore restore: python hooks.py restore
minutehand fork: restore restore: done in 0.0 s
minutehand fork: restore start: starting the agent's command, python agent.py
minutehand fork: restore answer: waiting for the agent's report endpoint
minutehand fork: restore answer: answered in 0.0 s
minutehand fork: restore verify: the report equals the one at the checkpoint
```

Minutehand stops the agent it started, restores the file, starts the agent again, and asks it for its report:
`idle`, next wake on the 26th, waiting on Rosa. That is what it reported at seq 18, so the fork goes on, and
Rosa's answer finishes the job. Every step's output is kept in the fork's `restore.json`.

Had the restore done nothing, the agent would answer as it was at the end of the parent run, with no next
wake, and the fork is refused before it plays:

```
the restore did not bring back the agent as it was at the checkpoint at seq 18: its report differs field by field:
  next_wake: 2026-08-26T09:00:00+00:00 at the checkpoint, none after
```

## What this proves, and what it does not

The verify step compares the agent's report: its status, its next wake and its commitments by key and
status. It cannot see anything the report does not carry. Here everything the agent knows is in the report
or decides it, so a different file shows; in an agent whose report is a summary of a larger state, a restore
that put back the wrong conversation history with the same open commitments would pass.
