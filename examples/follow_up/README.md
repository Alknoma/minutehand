# Follow up

A small agent, `agent.py`, gets a goal: confirm the venue for the team offsite with Rosa. It asks Rosa in
Slack, waits two days, follows up once if she has not answered, and tells Owen, who gave it the goal, when
she does. It is an ordinary program: a web server from the standard library and the stock `slack_sdk`.
It has no idea Minutehand exists.

Minutehand starts it, plays a fortnight of simulated time in a few seconds against a fake Slack, and
says how well it carried the job.

| File | What it is |
|---|---|
| `agent.py` | The agent. `AGENT_BEHAVIOUR=forgetful` makes it ask once and never follow up. |
| `agent.yaml` | Where Minutehand reaches it: the wake and report endpoints, and where Slack pushes messages. |
| `scenario.yaml` | Rosa answers 36 hours after she is asked. |
| `scenario_silent.yaml` | Rosa never answers. |

The agent needs `slack_sdk` in the Python that runs it (`pip install slack_sdk`). Minutehand needs nothing
from the agent's environment, and the agent nothing from Minutehand's.

## 1. Rosa answers

```bash
minutehand run scenario.yaml --agent agent.yaml -- python agent.py
```

Minutehand starts `python agent.py`, points its Slack calls at the fake Slack, and wakes it with the goal.
The agent asks Rosa. A day and a half later, simulated, Rosa answers; the agent thanks her, tells Owen and
reports that it is done.

```
run 30d6c0aa7b98: offsite_venue
  stopped at 2026-08-25 21:00 UTC (simulated) because the agent reported it was done

no findings

scorecard
  expectations met: 2 of 2
  ...
```

The command exits 0: nothing failed.

## 2. Rosa never answers, and the agent forgets

```bash
AGENT_BEHAVIOUR=forgetful minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
```

The forgetful agent asks Rosa once and asks to be woken never again. Rosa says nothing. Nobody comes back
to her, and the job sits waiting until the scenario's two weeks run out.

```
run bf46bbfef5fe: offsite_venue_silent
  stopped at 2026-09-07 09:00 UTC (simulated) because nothing more was due and the agent asked for no wake

fail (1)
  no_follow_up: wait on rosa expired 11 days 6 hours before the run ended and the agent never came back to it
    pattern expiry_on_every_wait: An expiry on every wait. Every wait carries an expected-by date and the agent wakes on it.
...
```

The command exits 1. The finding names what went wrong (a question went unanswered and the agent never
came back to it) and the design that prevents it: give every wait a date by which you expect an answer,
and wake on that date.

## Afterwards

Each run is kept under `.minutehand/` in the folder you ran it from (or `--state DIR`):

```bash
minutehand runs                 # every run, one line each
minutehand findings <run_id>    # a run's findings again, and the checkpoints it can be forked from
```

## In the container

The image runs the same commands; mount this folder and start the agent inside it. The image built with
`--target example` adds `slack_sdk` for this agent.

```bash
docker build --target example -t minutehand-example .
docker run --rm -v "$PWD/examples:/examples:ro" -w /examples/follow_up \
  minutehand-example run scenario.yaml --agent agent.yaml -- python agent.py
```
