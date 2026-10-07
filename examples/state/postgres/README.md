# Rewind an agent whose state is in PostgreSQL, with no hooks

The follow-up agent of [`../sqlite`](../sqlite), with everything it knows in a PostgreSQL database: the job, what
it is waiting on, and a note per wake with a serial id. Each wake it also drafts a note and rolls it back.

The agent file declares no `state:` hooks. It declares the database instead, and Minutehand fronts it:

```yaml
databases:
  - kind: postgres
    name: app
    listen: 127.0.0.1:6543                                   # where Minutehand listens as PostgreSQL
    upstream: postgres://postgres:secret@127.0.0.1:5432/follow_up
    env: DATABASE_URL                                        # set for the agent to the upstream, pointed at 6543
    base: {kind: template}
```

Tested end to end against a real PostgreSQL: `tests/state/test_postgres_recipe.py` (`-m docker`; it starts a
`postgres:16` container, or uses the server `MINUTEHAND_TEST_POSTGRES` names), and in CI with a service container.

| File | What it is |
|---|---|
| `agent.py` | The agent. Needs `slack_sdk` and `psycopg`. Connects to `DATABASE_URL` for every request. |
| `agent.yaml` | Where Minutehand reaches it, and the database it fronts. |

The scenario and the fork are the SQLite recipe's: `../sqlite/scenario_silent.yaml` and
`../sqlite/fork_rosa_answers.yaml`.

## Run it

Start a PostgreSQL with a `follow_up` database (`docker run -d -e POSTGRES_PASSWORD=secret -p 5432:5432
postgres:16`, then `createdb -h 127.0.0.1 -U postgres follow_up`), and from this folder:

```bash
minutehand run ../sqlite/scenario_silent.yaml --agent agent.yaml -- python agent.py
minutehand fork <run_id> --at <seq after wake 1> --changes ../sqlite/fork_rosa_answers.yaml -- python agent.py
```

Before the agent starts, Minutehand takes the base: `CREATE DATABASE follow_up_mh_<run> TEMPLATE follow_up`.
Every transaction the agent commits through the relay is written into the run's log with its statements and
parameters, before the agent hears it committed. A fork stops the agent, drops `follow_up`, creates it again from
the base, replays every transaction recorded up to the checkpoint, comparing each statement's answer, the rows it
returned and the sequences after it with the record, starts the agent, and compares its report:

```
minutehand fork: restore stop: stopping the agent's command, python agent.py
minutehand fork: restore database: putting back the databases Minutehand fronts
minutehand fork: restore database: database app made again from base follow_up_mh_…, 2 transaction(s) (5 statement(s)) replayed and matched, in 0.06 s
minutehand fork: restore start: starting the agent's command, python agent.py
minutehand fork: restore verify: the report equals the one at the checkpoint
```

## What it costs, and what it does not cover

The log grows by what the agent wrote: here 8 records, about 4 KB, against a database of 8 MB. There is no copy
per checkpoint. The base is one copy per run; `TEMPLATE` copies files, so it takes as long as the database is
large. For a database of many gigabytes, name a copy-on-write branch as the base instead (`base: {kind:
command, take: [...], put_back: [...]}`: a Neon branch, a ZFS or btrfs snapshot, a cloud volume snapshot), and a
fork's cost is the branch plus the replay of what the agent wrote before the checkpoint.

Not covered, each said in `docs/design.md` ("The agent's database, recorded at the wire"): TLS between the agent
and the database (the relay speaks in the clear and answers `sslmode=require` with "the server does not support
SSL"); writes that do not pass the relay (another process, a trigger calling out, `pg_cron`); values the database
makes up itself (`now()`, `random()`, `gen_random_uuid()`), which a replay makes up again: a write that returns
them is refused at the fork, one that does not is silently different; a function called in a `SELECT` that
writes; `COPY ... FROM STDIN` and two-phase commit, after which a fork is refused; and any database but
PostgreSQL. State outside the database still needs `state:` hooks, which run beside the replay.
