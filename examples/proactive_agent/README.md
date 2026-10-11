# A small proactive agent

`agent.py` gets answers from people and passes them on. Its work is its own: `work.json` lists each ask (who answers,
the question, who is told), and the code names no one. It asks in Slack, follows up at most twice, a working day
apart, and tells the answer on, or that none came. Nothing hands it this work, and nothing wakes it on a schedule: it
decides when it next wakes.

That decision is the only part that needs judgement, so it is the only part that uses a model, and it lives in its own
small package, `wake/`:

| File | What it does |
|---|---|
| `wake/decide.py` | A Pydantic AI agent, the wake decider. It is shown what the agent is waiting on (since when, and the earliest it may act on it again) and proposes one moment to wake, and why. It can weigh what plain code cannot, such as a person who usually answers in the afternoon. |
| `wake/guard.py` | The rules every wake keeps, whatever the decider proposed: nothing open, no wake; something open, always a wake; never before an answer can be due; never in the past; always in working hours (a weekend is skipped). If the decider proposes nothing, or fails, the earliest due moment is used. |

The rest of `agent.py` is plain code: ask, follow up, report. Its memory is `minutehand.agent.store`, a SQLite file in
production and the run's own memory under Minutehand. Everything it knows between wakes is there, even the time of its last wake, so a restarted agent (a
fork from a checkpoint) is the agent it was. A Slack event is only recorded; the agent acts on the wake it brings.

The worlds are the people `work.json` names; the example's work asks Sam, in finance, for a cost centre and tells Owen.

| World | What happens |
|---|---|
| `worlds/answers.yaml` | Sam answers a few hours after he is asked. The agent tells Owen his answer at once, and names no wake after. |
| `worlds/quiet.yaml` | Sam never answers. The agent follows up a working day after its question, again a day after that, then tells Owen, and stops. |

## Run it

```bash
pip install minutehand slack_sdk 'pydantic-ai-slim[openai]'
export AGENT_MODEL=openai:gpt-6-luna OPENAI_API_KEY=...          # the wake decider's model; AGENT_MODEL=test offline
export MINUTEHAND_MODEL=... MINUTEHAND_MODEL_API_KEY=...         # the model that plays Sam and Owen
minutehand run worlds/quiet.yaml --agent agent.yaml -- python agent.py
```

`tests/proactive_agent` runs both worlds, and tests the guard against a decider that proposes too early, one that
proposes well, and one that fails.
