# Follow up

A small agent, `agent.py`, gets a goal: confirm the venue for the team offsite with Rosa. It asks Rosa in
Slack, waits two days, follows up once if she has not answered, and emails Owen, who gave it the goal, when
she does. It is an ordinary program: a web server from the standard library and the stock `slack_sdk`.
Its one line of Minutehand is what it remembers (its status, its next wake, whether it followed up, Rosa's
answer), kept in `minutehand.agent.store`: a SQLite file (`AGENT_DB`, default `follow_up.db`) in production, and
the run's own memory under Minutehand, so every run starts empty and a fork from any checkpoint starts from what
the agent remembered there, with nothing to snapshot or restore.

Minutehand starts it, plays a fortnight of simulated time in a few seconds against a fake Slack, and
judges the run by the rules the scenarios declare: what must be true at the end (`expect:`), and how the agent
must behave on the way (`assess:`, `docs/assessments.md`). Those rules are this example's; yours may differ.

| File | What it is |
|---|---|
| `agent.py` | The agent. `AGENT_BEHAVIOUR=forgetful` makes it ask once and never follow up. |
| `agent.yaml` | Where Minutehand reaches it: the wake and report endpoints, where Slack pushes messages, and the two hosts it calls that are not faked (`outbound`). |
| `scenario.yaml` | Rosa answers 36 hours after she is asked. Owen is told the outcome and owes no answer. |
| `scenario_silent.yaml` | Rosa never answers. Its rule `follows_up_when_due` says the agent must follow her up within the hour of when her answer was due. |

The agent needs `slack_sdk` and `minutehand` (for `minutehand.agent`, which imports only the standard library)
in the Python that runs it.

## 1. Rosa answers

```bash
minutehand run scenario.yaml --agent agent.yaml -- python agent.py
```

Minutehand starts `python agent.py`, points its Slack calls at the fake Slack, and wakes it with the goal.
The agent asks Rosa. A day and a half later, simulated, Rosa answers; the agent thanks her, emails Owen and
reports that it is done.

```
run 054b98ac4e29: offsite_venue
  Passed: no check failed, and the agent reported it was done.
  stopped at 2026-08-25 21:00 UTC (simulated) because the agent reported it was done
  providers the agent called: slack

outbound calls
  api.mail.example: 1 call, acknowledged, never sent

informational (3)
  expectations: rosa asked: met by the message to Rosa Lind (seq 17): “Hi Rosa, could you confirm the venue for the team offsite, please?”; the message to Rosa Lind (seq 21): “Thank you!”
  expectations: owen asked mentioning ['confirmed']: met by the message to Owen Hart (seq 22): “Offsite venue The offsite venue is confirmed: The lakeside hall, booked for the 14th.”
  expectations: owen told what rosa said ('lakeside hall'): met by the message to Owen Hart (seq 22): “Offsite venue The offsite venue is confirmed: The lakeside hall, booked for the 14th.”

scorecard
  expectations met: 3 of 3
  ...
```

Each expectation that held quotes what met it, so a pass that rests on the wrong message shows: "Rosa asked"
is met by the question and also by the thank-you. The third expectation, `relayed`, holds only because the
note to Owen carries "lakeside hall", a phrase that only Rosa's answer holds.

The command exits 0: nothing failed, and the agent said it was done. The scorecard counts one wait, Rosa's answer, and none open at the end:
the thank-you to Rosa and the email to Owen asked nothing, because nobody would answer them.

The email to Owen went to `api.mail.example`, which no fake answers. `agent.yaml` declares it `acknowledge`:
it never left the machine, was answered 202, and was read by the paths under `message` as an email from the
agent to Owen, which is how "owen told what rosa said" is met by it. Without the declaration the call would
be refused with 502, and the agent would fail its wake. See `docs/capture.md`.

## 1a. The agent looks the venue up

```bash
LOOKUP_URL='https://places.example/v1/search?q=' minutehand run scenario.yaml --agent agent.yaml -- python agent.py
```

With `LOOKUP_URL`, the agent searches for the venue Rosa named before it writes to Owen. `places.example` is
declared `pass_through`: the search goes to the real host, and the request and its answer are kept with the
run (`minutehand view` shows both under "Outbound calls"). `places.example` stands for whatever search your
agent uses; `tests/e2e/test_example_capture.py` runs this with a local server in its place.

## 2. Rosa never answers, and the agent forgets

```bash
AGENT_BEHAVIOUR=forgetful minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
```

The forgetful agent asks Rosa once and asks to be woken never again. Rosa says nothing. Nobody comes back
to her, and the job sits waiting until the scenario's two weeks run out.

The command exits 1, with one failed finding from the scenario's rule `follows_up_when_due`: Rosa's answer
was due and no follow-up came within the hour. The rule names its pattern, so the report also names the design
that prevents it: give every wait a date by which you expect an answer, and wake on that date.

That a follow-up is owed, and when, is this scenario's rule, not Minutehand's. Leave `assess:` out of
`scenario_silent.yaml` and the same run is `Not assessed` (exit 5): it reports what happened, a wait on Rosa still
open and no follow-up made, and judges none of it.

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
