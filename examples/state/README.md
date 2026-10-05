# Rewinding the agent's own state

A fork rewinds the fakes' world by reading their log as of an earlier moment. The agent's own state is the
agent's to save and put back, through four commands in its agent file (`state:`), and Minutehand's to prove:

- **Settle.** A checkpoint is snapshotted only when the agent reports it is not working and has made no
  outbound call through the proxy for `quiet`. One that has not settled after `settle_limit` is recorded as
  not restorable, with the reason, and a fork from it is refused with that reason.
- **Restore** is `stop`, `restore`, `start`, then the agent's report endpoint must answer within
  `answer_limit`. A step that fails stops the fork, naming the step and showing its output.
- **Verify.** The agent's report after the restore must equal the report recorded at the checkpoint: the
  same status, the same next wake, the same commitments by key and status. If not, the fork is refused with
  the difference, field by field.

| Recipe | State | Tested |
|---|---|---|
| [`sqlite/`](sqlite/) | a SQLite file; snapshot and restore by SQLite's online backup | in the default suite |
| [`firestore_emulator/`](firestore_emulator/) | Google's Firestore emulator in a private container; export while running, restart with `--import` | against a real container, `-m firestore`, nightly |
| [`postgres.md`](postgres.md) | PostgreSQL: a template database, or a base backup and point-in-time recovery | **untested**, described only |

## What no restore brings back

- What a real third-party service keeps. The proxy refuses hosts no provider claims, but a call it tunnels (a
  model API) reaches the real service, and whatever that service stored stays as the parent run left it.
- What a model provider keeps on its side: a stored conversation or response, a cache, a batch, a file.
- The AWS provider's queues and schedules, which moto holds in memory; a fork with a booking pending is refused.
- Work the agent does in the background after the quiet period, and calls that never pass the proxy (to
  `localhost`, or on a tunnel that was already open): settling cannot see them.
- Anything the agent's report does not show: the verify step compares the report and nothing else.
