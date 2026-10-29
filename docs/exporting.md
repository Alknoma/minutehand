# Exporting a run

`minutehand export` writes one run as one JSON record: what a reader outside Minutehand needs to tell the run's story
to someone who will never open Minutehand. It holds the run's facts in the run's own ids and, for a finished run,
the verdict and findings `minutehand findings` reads, as they were stored: nothing is judged again and no model is
called. A site or a report that retells the run reads it and adds its own words.

```bash
minutehand export <run> --out record.json     # or to stdout without --out
minutehand export <run> --state DIR --out record.json
```

`<run>` is a run's or a fork's id, or the start of one, as for `minutehand query`. The record is read from the run's
world as it stood at its end and from its scenario; nothing is written to the run. On stderr the command says how much
it wrote and what it left out, and why. Over MCP (`minutehand mcp`) it is the tool `export`.

## The record

The record's version is its `schema`: `minutehand-replay-record/2`. A change that removes or renames a field, or
changes what one means, is a new version; a reader validates the version it knows and refuses any other. `/2` only
adds fields to `/1` (`run.owner`, `run.goal`, `run.deadline` and `assessment`) and every `/1` field means what it did,
yet a reader of `/1` refuses a `/2` record all the same. Field names
are the reader's (`replyTo`, `editedUntil`); a field with nothing to say is left out rather than written as `null`.
Every moment is UTC, ISO 8601.

`schemas/record.schema.json` (`minutehand schema record`) is the record's JSON Schema (2020-12), generated from the
model the export writes and committed with a drift test (`tests/test_schemas.py`): it pins `schema` to
`minutehand-replay-record/2` and refuses a field it does not name. A reader validates against it, and generates its
types from it, rather than copying the table below.

| Field | What it holds |
|---|---|
| `schema` | `minutehand-replay-record/2` |
| `run` | `id`; `scenario`, its name; `timezone`, the zone a reader shows every moment in: the scenario owner's (`owner`), else its first person's, else `UTC`; `start`, the scenario's start; `end`, the end its record gives, or for a run with no record yet (still being written, or died before it wrote one) its last event's moment, as `minutehand view` reads it; `ranOn`, the day the run was performed, by the machine's clock at its first event; `chat`, the chat service its conversations are in as its users call it (`Slack`); `markup`, how its messages are written (`slack`); `owner`, the scenario owner's person key, when it names one; `goal`, the scenario's goal verbatim, when it has one; `deadline`, `start` plus the scenario's `deadline_after`, when it gives one |
| `agent` | `key`, its name lower-cased (`agent` when a person has that key); `name`, the agent file's `name`, else the name its Slack bot shows; `chatId`, its bot's member id, which a mention of it (`<@U…>`) names |
| `people` | Every person of the scenario: `key`, `name`, `title` when it has one, and `chatId`, their member id in the first workspace |
| `conversations` | Every Slack conversation a message in the record is in: a channel (`kind: channel`, `id`, `name`), a DM with the agent (`kind: direct`, `id`, `with`: the person), or a group DM (`kind: group`, `id`, `with`: everyone in it but the agent) |
| `messages` | Every message still in the workspace at the run's end: `id`, its `ts`; `at`, its first moment, and for one the scenario seeded, the moment Slack dates it, before the run; `by`, a person's key or the agent's; `conversation`; `replyTo`, the thread it answers; `text`, its last words; `wake`, the wake that sent it, 0 for one already there; `edited`, whether its words changed after it was sent; `editedUntil`, when they last did |
| `wakes` | Each wake of the agent as a window: `n`, `from` its moment, `to` the next one's, the last to the run's end |
| `items` | What was already written down when the run began, as the scenario seeded it: `id`, its provider's; `kind`, `Ticket`, `Document` or `Spreadsheet`; `ref`, a Jira issue's key (`PLATFORM-1`); `tool`, its service as its users call it; `place`, a ticket's project, or the folders a document sits in, outermost first (`Delivery / Notes`); `title`; `text`, a ticket's body or a document's text as its provider flattens it; `rows`, a sheet's cells; a ticket's `state` (`open` or `done`) and `assignee` |
| `approvals` | Each item the agent put in a person's inbox (`docs/inboxes.md`): `id`, the product's; `at`, when Minutehand first saw it waiting; `for`, the person; `status`: `pending`, `approved` or `rejected` (a decision whose name starts so), `decided` (any other decision, named in `decision`), or `withdrawn` (gone from the inbox undecided); `summary`, what it asks as the product words it |
| `actions` | What the agent created, changed or deleted itself in the team's tools outside the chat: a ticket, a document, a comment. `id`, the event's seq; `at`; `wake`; `did`, said as a reader would (`Created a document`); `title`, of the thing it was done to; `tool`; `place`; `ref`, a Jira key |
| `assessment` | Only for a finished run, whose stored result `minutehand findings` reads; left out for a run still being written or one that died before its record. `verdict`, the verdict's kind (`passed`, `unfinished`, `failed`, `environment_failed`, `tool_failed`, `not_judged`, `simulation_incomplete`, `replay_parted`); `stop`, how the run ended; `failedChecks`; `openWaits`, waits the world had not settled at the end; `findings`, each of the checks' findings in the order they were stored: `check`, `severity` (`error`, `warning`, `information`), `kind` (`fail`, `review`, `informational`), `text`, its message; `at` and `wake` when it names them; `cites`, each event it gives as evidence that the record holds, as `{"message": id}`, `{"action": id}` or `{"approval": id}`, in order, each once |

## What it leaves out, and why

- **A conversation in any chat but Slack.** A run whose people talk with the agent in another service is refused:
  half a run's conversations would read as all of them.
- **What an inbox item asks for in full.** Minutehand keeps an item's summary, its decisions and how it was decided;
  the drafted ticket or document behind it is the agent's own product's, and its fields are its own.
- **A message deleted by the run's end, an ephemeral one, or one by an account outside the scenario** (another app,
  Slackbot). The command counts each on stderr.
- **A comment the scenario seeded on a ticket.** It belongs to the ticket, which the record names; its words are in
  the run's read model (`minutehand query`, the `messages` view).
- **The verdict's sentence, and what the checks found beyond it.** `assessment` carries the verdict's kind and
  counts and the agent's findings, never the verdict's words (they name hosts and URLs) nor the simulated world's
  health, which `verdict` already sums up (`simulation_incomplete`); `minutehand findings` holds both.
- **Evidence the record holds no place for.** A finding's evidence is events; one that is no message, action or
  approval in the record (a read, the agent's memory, a deleted message) is dropped from `cites`, and the command
  says how many.

## Where each fact comes from

Most of the record is the store's own events: messages' versions, wakes, the scenario's tickets and documents as
seeded, inbox items, the agent's changes. What only a provider can read from its own JSON, it says itself: Slack what
a conversation is called and who is in it, who wrote a message and when Slack dates it (`slack/present.py`); Jira
the key each issue shows (`jira/present.py`). A provider's title (`Manifest.title`) is the service's name in `tool` and
`chat`.
