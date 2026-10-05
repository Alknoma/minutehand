# Rewinding an agent whose state is in PostgreSQL

**Untested.** Nothing in this repository runs it. It describes the equivalent of the SQLite and Firestore
recipes, for an agent whose state is a PostgreSQL database, so the choice of mechanism is written down; a
recipe that is run belongs beside them as a folder with a test.

The hooks are the same four: `snapshot`, `stop`, `restore`, `start`, each given `MINUTEHAND_SNAPSHOT_DIR`.

## Small databases: a template per checkpoint

PostgreSQL copies a whole database from another in one statement, `CREATE DATABASE ... TEMPLATE ...`, and the
copy is a file-level clone of the template, so it is quick for a database of a few hundred megabytes.

- `snapshot`: create a database named for the checkpoint from the agent's database, as a template, and write
  its name into `$MINUTEHAND_SNAPSHOT_DIR`. A template must have no other connection while it is copied, so the
  agent's connections are closed first (`pg_terminate_backend` on the agent's database) or the agent is told
  to release them; the copy then holds exactly what was committed.
- `stop`: stop the agent's processes, so none holds a connection or a cache.
- `restore`: drop the agent's database and create it again from the snapshot's template database.
- `start`: start the agent's processes.

Cost: one database per restorable checkpoint, held until the run is deleted. Every checkpoint briefly cuts
the agent's connections.

## Large databases: a base backup and point-in-time recovery

- Once, before the run: a base backup (`pg_basebackup`) with WAL archiving on.
- `snapshot`: no copy at all. Record a restore point (`SELECT pg_create_restore_point('<checkpoint>')`) and
  write its name into `$MINUTEHAND_SNAPSHOT_DIR`.
- `stop`: stop the agent's processes, then the server.
- `restore`: replace the data directory with the base backup, set `recovery_target_name` to the restore point
  and `recovery_target_action = 'promote'`, create `recovery.signal`, start the server and wait for recovery
  to end.
- `start`: start the agent's processes.

Cost: a restore replays every WAL record from the base backup to the restore point, so it grows with how much
the agent wrote before the checkpoint; WAL is kept for the whole run. After a restore the server is on a new
timeline, so a second fork from the same parent must recover along the original timeline
(`recovery_target_timeline`), not the one the previous fork created.

## What neither reaches

Sequences are part of the database and come back with it; a value the agent cached from one does not, which
is why `stop` and `start` are not optional. Anything outside the database — a queue, a cache server, files
beside it — needs its own snapshot in the same hooks.
