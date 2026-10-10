# A reliable proactive agent

An agent that keeps working on its own, for weeks, without a person watching, fails in a few ways that come back
again and again. A real model running a purchase in Minutehand did all of these
in five runs:

- **it forgot to come back:** it asked, and nothing brought it back when the answer was due;
- **it chased too soon:** twenty minutes after asking someone who answers in a few hours;
- **it acted before the decision:** it ordered while the approval was still pending, on a person's offhand "you're
  good to proceed";
- **it made up a fact:** it sent the approver a per-unit quote nobody had given it;
- **it repeated itself:** the same message twice, ten updates to the requester in two days, the same request read
  twenty times for three changes.

None of these is fixed by a better prompt. Each is fixed by structure the model works inside. This agent does one
purchase (`work.py`): get the cost centre from finance, file the approval request, wait for it, order, and tell the
requester. Its model does the language and the judgement; plain code makes each failure above impossible:

| Part | Its one job | What it makes impossible |
|---|---|---|
| `planner.py`, the wake planner | Decides when the agent wakes next, from its state alone: every open wait's due moment, every held request's next look, moved into working hours. The model never sets a wake. | Forgetting to come back (every wait has a date, and the planner wakes on it); chasing before an answer is due; polling a quiet request often (the look backs off while nothing changes); waking again for what it could not do |
| `moves.py`, the action menu | Works out the moves open now from the actual state of each item; the model picks among them and nothing else | Ordering before approval, or after a rejection: there is no such move to pick |
| `ledger.py`, the facts ledger | Keeps where every fact came from: its configuration, a person's words (kept only if their words hold the value), a service it read. Every figure and code in an outgoing message must be in it | Sending a figure from nowhere: a message holding one is written again, and failing that replaced by plain words from the ledger |
| `sender.py`, the sender | One message per purpose, never the same words twice to one person | Duplicates, retried events acted on twice, a follow-up that repeats the question word for word |
| `state.py`, the state | Everything the agent knows, read at the start of every wake and written at the end, in `minutehand.agent.store` (a SQLite file in production) | Losing track between wakes; a restart forgetting what is owed |

Two more rules it keeps:

- **Events are recorded, never acted on in the handler.** A Slack event is written to the inbox and answered at once;
  the model runs on the wake that follows (a reply also wakes it). A slow model never makes Slack retry, and a
  retried event is never acted on twice.
- **A decision is read where it is used.** Before it orders, it reads the request again, so the order rests on the
  approval as it stands, not as it stood an hour ago.

It is proactive: it brings its own work and decides when it next wakes. Nothing hands it a task, and nothing wakes it
on a schedule.

## What it does when the world is difficult

| World | What happens |
|---|---|
| `worlds/approved.yaml` | Sam answers within hours; the agent files at once; Nadia approves two days later; it reads the request on its backoff, orders within the hour of her decision, tells Owen, and names no wake after |
| `worlds/asks_back.yaml` | Nadia asks back for a per-unit quote the agent does not hold. It does not invent one: it asks Owen, who has it, resubmits his words, and orders when she approves |
| `worlds/finance_quiet.yaml` | Sam never answers. It follows up a working day later, again a day after that, each time saying something new, then tells Owen it is stuck, and stops chasing; Sam's answer, if it ever comes, wakes it |

`tests/reliable_agent` runs each world twice: with a careful model, and with a reckless one that tries to order at
every turn and writes a made-up price into every message. What reaches the world is the same both times; the
agent's memory lists what its structure stopped (`blocked`).

## Run it

```bash
pip install minutehand slack_sdk httpx
export AGENT_MODEL_BASE_URL=https://api.openai.com/v1 AGENT_MODEL=gpt-4o-mini AGENT_MODEL_API_KEY=...   # its model
export MINUTEHAND_MODEL=... MINUTEHAND_MODEL_API_KEY=...   # the model that plays its people and the approvals service
minutehand run worlds/asks_back.yaml --agent agent.yaml -- python agent.py
```

`agent.yaml` holds only what any proactive agent serves: the wake and report endpoints, where Slack pushes messages,
and its own order system, kept inside the run. Every run is assessed against the agent's own instructions
(`work.INSTRUCTIONS`, read from its model calls) and the world, with nothing more to write.

**The trade-off it makes.** The approvals service does not say when it decides, so the agent reads the request on a
backoff (an hour, doubling to four working hours while nothing changes). It acts on a decision the same half day, at
the price of a few reads that find nothing new, which the assessment reports for review (`redundant_reads`). A
service that pushes its decisions removes the trade-off.
