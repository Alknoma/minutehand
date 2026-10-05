# Rewind an agent whose state is in a Firestore emulator

An agent that keeps its state in Firestore, and in its own memory, run against Google's Firestore emulator in
a private container. Minutehand snapshots it with the emulator's own export and restores it by restarting
the emulator with that export imported.

Tested against a real emulator container: `tests/state/test_firestore_recipe.py`, marked `firestore`, left out
of the default run (`uv run pytest -m firestore`). The nightly workflow runs it with the image built from this
folder's Dockerfile.

| File | What it is |
|---|---|
| `agent.py` | The agent. Reads `jobs/offsite` and its commitments once as it starts, answers its report from memory, writes every change through. Standard library only. |
| `hooks.py` | `up`, `down`, `start-agent`, `stop-agent`, and the hooks `snapshot` and `restore`. Standard library and `docker compose`. |
| `compose.yaml` | One service, `firestore`: the emulator, its REST API and hub on loopback ports you choose, and a directory shared as `/exports`. |
| `Dockerfile` | `node:20-bookworm-slim`, Java 17 and `firebase-tools@13.35.1`, with the Firestore emulator downloaded at build time. |
| `firebase.json`, `start.sh` | The emulator binds `0.0.0.0`; `start.sh` passes `--import /exports/restore` when a snapshot is there. |
| `agent.yaml` | Where Minutehand reaches the agent, and all four state commands. |
| `scenario.yaml` | Rosa never answers: the agent follows up twice, two days apart, then gives up. |

## Google's emulator cannot import while it runs

The emulator exports while it runs: `POST http://<hub>:4400/_admin/export` with `{"path": "<a directory inside
the container>", "initiatedBy": "..."}` answers 200 and writes `firebase-export-metadata.json` and
`firestore_export/`. It does not import while it runs: firebase-tools passes an export to the emulator only as
it starts (`--import`), and no import endpoint answers anything but 400. So a restore here is a restart:

1. `stop`: `hooks.py stop-agent` stops the agent. It remembers what it read at startup; left running, it
   would answer from that memory whatever the database now holds.
2. `restore`: `hooks.py restore` copies the snapshot to `/exports/restore` and restarts the container;
   `start.sh` starts the emulator with `--import /exports/restore`; the hook waits until the REST API answers.
3. `start`: `hooks.py start-agent` starts the agent, which reads the restored state.
4. Minutehand waits for `GET /report` and compares the report with the one recorded at the checkpoint.

## How long a restore takes

The `restore` step, from the copy to the emulator answering again with the export imported:

| Where | Image | Seconds |
|---|---|---|
| macOS, Docker Desktop, about 47 other containers running, 2026-10-04: two test runs, two restores each | a local image with firebase-tools 13.35.1 | 6.8 and 16.7; 4.5 and 4.7 |
| the same machine, more heavily loaded, earlier the same day (stop to answering) | the same | about 58 |

Plan for a minute per fork. The whole restore sequence in the test (stop, restore, start, answer, verify)
took 8.3 s and 5.5 s; the test took 59.5 s and 31.1 s end to end, emulator start and removal included.

## Run it

From this folder, with Docker running:

```bash
export FIRESTORE_EXPORTS=$PWD/exports       # shared with the container as /exports
python hooks.py up                          # builds the image the first time (several minutes)
python hooks.py start-agent
minutehand run scenario.yaml --agent agent.yaml
minutehand fork <run_id> --at <seq after wake 1> --changes <a fork file>
python hooks.py stop-agent
python hooks.py down
```

`FIRESTORE_IMAGE` names an image you already have that holds `firebase` and Java, to skip the build.
`MINUTEHAND_FIRESTORE_PROJECT` names the Compose project, so several can run side by side;
`FIRESTORE_PORT` and `FIRESTORE_HUB_PORT` move the loopback ports from 8080 and 4400.

## What this proves, and what it does not

- The test changes the database after the checkpoint (a commitment deleted, a document added) and finds it
  back as it was once the fork's restore has run: the commitment is there, the later document is gone, the
  job's fields are the checkpoint's.
- A restore of the wrong snapshot (the one after wake 2, for a fork from wake 1) is refused by the verify
  step: `next_wake: 2026-08-26T09:00:00+00:00 at the checkpoint, 2026-08-28T09:00:00+00:00 after`.
- The verify step compares only the agent's report. A wrong snapshot whose report reads the same, for
  instance two checkpoints between which the agent changed only what it does not report, passes.
- The agent's calls to the emulator go to `127.0.0.1`, which Minutehand's proxy never sees, so the quiet
  period before a checkpoint sees none of them; the agent here writes before it answers its wake, so its
  report covers them.
