# Follow up

A small proactive agent, `agent.py`, brings its own work: confirm the venue for the team offsite with Rosa, and tell
Owen. It asks Rosa in Slack, and **decides when it next wakes**: two days on, to follow up once if she has not
answered. Nothing wakes it on a schedule; after every wake it reports the moment it wants waking next, or none. When
she answers it thanks her and emails Owen. It is an ordinary program: a web server from the standard library and the
stock `slack_sdk`. Its one line of Minutehand is what it remembers (its status, its next wake, whether it followed
up, Rosa's answer), kept in `minutehand.agent.store`: a SQLite file (`AGENT_DB`, default `follow_up.db`) in
production, and the run's own memory under Minutehand, so a fork from any checkpoint starts from what the agent
remembered there, with nothing to snapshot or restore.

Each scenario is a world, not a task: Rosa and Owen, who they are, how Rosa answers, and how long to watch the agent
(`runs_for`). The run hands the agent no work. Minutehand starts it, plays a fortnight of simulated time in a few
seconds against a fake Slack, and assesses every message it sends, automatically, against its own instructions and
that world (`docs/assessments.md`), with nothing more to write.

| File | What it is |
|---|---|
| `agent.py` | The agent. `AGENT_BEHAVIOUR=forgetful` makes it ask once and never wake again. |
| `agent.yaml` | Where Minutehand reaches it: the wake and report endpoints, where Slack pushes messages, and the two hosts it calls that are not faked (`outbound`). |
| `scenario.yaml` | A world where Rosa answers 36 hours after she is asked. |
| `scenario_silent.yaml` | A world where Rosa never answers. |
| `scenario_team_policy.yaml` | The silent world with one rule of a team's own: a follow-up is owed two days on. An example of optional policy. |

The agent needs `slack_sdk` and `minutehand` (for `minutehand.agent`, which imports only the standard library)
in the Python that runs it. Rosa's words are a model's, written from her profile and facts, so Minutehand needs a
model for its people (`MINUTEHAND_MODEL`, `MINUTEHAND_MODEL_API_KEY`, and `MINUTEHAND_MODEL_BASE_URL` for a service
other than OpenAI's); offline, run the recipes' stand-in, which answers by fixed rules:

```bash
python ../recipes/fake_model.py &
export MINUTEHAND_MODEL_BASE_URL=http://127.0.0.1:8790/v1 MINUTEHAND_MODEL=people MINUTEHAND_MODEL_API_KEY=offline
```

## 1. Rosa answers

```bash
minutehand run scenario.yaml --agent agent.yaml -- python agent.py
```

Minutehand starts `python agent.py`, points its Slack calls at the fake Slack, and wakes it: "it is now Monday
09:00; go". The agent asks Rosa and names its next wake, two days on. A day and a half later, simulated, Rosa
answers; the agent thanks her, emails Owen, and names no next wake. The world runs on to the end of the window.

```
run d56acb3f5d75: offsite_venue (seed 64972089)
  Passed: no check failed in the window; the run watched the agent to the end of its window.
  stopped at 2026-09-07 09:00 UTC (simulated) because the clock reached the end of the scenario's window
  providers the agent called: slack

outbound calls
  api.mail.example: 1 call, acknowledged, never sent

no findings

scorecard
  waits opened: 1, still open at the end: 0
  follow-ups made: 0
  wakes: 2, of which changed nothing: 0
  messages to people: 3, edited in place: 0, deleted: 0
  failed checks: 0
```

The email to Owen went to `api.mail.example`, which no fake answers. `agent.yaml` declares it `acknowledge`: it
never left the machine, was answered 202, and was read by the paths under `message` as an email from the agent to
Owen, and assessed as one. Without the declaration the call would be refused with 502, and the agent would fail its
wake. See `docs/capture.md`.

## 1a. The agent looks the venue up

```bash
LOOKUP_URL='https://places.example/v1/search?q=' minutehand run scenario.yaml --agent agent.yaml -- python agent.py
```

With `LOOKUP_URL`, the agent searches for the venue Rosa named before it writes to Owen. `places.example` is
declared `pass_through`: the search goes to the real host, and the request and its answer are kept with the
run (`minutehand view` shows both under "Outbound calls"). `places.example` stands for whatever search your
agent uses; `tests/e2e/test_example_capture.py` runs this with a local server in its place.

## 2. Rosa never answers

```bash
minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
```

The agent asks Rosa, wakes when it said it would, follows her up once, and names no wake after that. The run
passes: nothing it sent contradicts the world, repeats itself, or comes before a time the world declares for Rosa's
answer (her profile says she does not answer; it declares no time). Run it forgetful
(`AGENT_BEHAVIOUR=forgetful`) and it passes too: a follow-up nobody sent is no fact of the world. Whether one is
owed is the agent's own instructions' to say, which the reviewer reads with `--judge` and a capable model; a team
that wants it as a hard rule writes it in `assess:` (`docs/assessments.md`), as `scenario_team_policy.yaml` does: run
forgetful against it and the run fails, naming the rule.

## Afterwards

Each run is kept under `.minutehand/` in the folder you ran it from (or `--state DIR`):

```bash
minutehand runs                 # every run, one line each
minutehand findings <run_id>    # a run's findings again, and the checkpoints it can be forked from
```

## An agent that is already running

Minutehand need not start the agent. Leave `-- python agent.py` out, put the proxy on a fixed port, and
give the agent the environment `minutehand env` prints before the run:

```bash
minutehand env --agent agent.yaml --proxy-port 18080 > minutehand.env    # export lines; source them
minutehand run scenario.yaml --agent agent.yaml --proxy-port 18080       # the agent is already up
```

An agent started on its own cannot be handed a secret made for each run, so its `agent.yaml` says
`secret: {kind: from_env, env: <variable>}` and Minutehand reads the agent's own signing secret from that
variable. For an agent in containers, `--proxy-host 0.0.0.0 --agent-proxy-host host.docker.internal
--format compose --service <name>` prints a Compose override that sets the same variables in each named
service and mounts the CA bundle into it.

## In the container

The image runs the same commands; mount this folder and start the agent inside it. The image built with
`--target example` adds `slack_sdk` for this agent.

```bash
docker build --target example -t minutehand-example .
docker run --rm -v "$PWD/examples:/examples:ro" -w /examples/follow_up \
  minutehand-example run scenario.yaml --agent agent.yaml -- python agent.py
```
