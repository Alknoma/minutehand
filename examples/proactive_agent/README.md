# A small proactive agent

`agent.py` gets answers from people and passes them on. Its work is its own: `work.json` lists each ask (who answers,
the question, who is told), and the code names no one. It asks in Slack, follows up at most twice, a working day
apart, and tells the answer on, or that none came. Nothing hands it this work. When it asks, it books what it will
say later as Slack scheduled messages (`chat.scheduleMessage`): the two follow-ups, and the word to Owen that no answer
came. Slack keeps them and posts each at its moment; when an answer comes, the agent deletes the ones not yet posted
(`chat.deleteScheduledMessage`) and passes the answer on. No thread of it waits on a deadline: its plan is records,
Slack's and its own SQLite file's (`proactive_agent.db`), which can be read and changed. It imports nothing of
Minutehand's and has no endpoint for it. It is plain Python on stock `slack_sdk`, about 170 lines, and the whole of it
is the agent's.

Under Minutehand it runs unchanged, in a sandbox whose clock Minutehand owns (`docs/design.md`, "A sandbox whose
clock Minutehand owns"): the sandbox's clock reads simulated time, so a day's wait passes in a jump; each scheduled
message is a booking in the run's log, which the run delivers when its clock reaches it; and the only way out of the
sandbox is Minutehand's proxy, where the fake Slack answers. A fork from any checkpoint plays the agent's recorded
inputs back into a fresh sandbox, and the agent stands where it stood; the scheduled messages still pending there are
in the log the fork shares, and are posted at their moments.

| World | What happens |
|---|---|
| `worlds/answers.yaml` | Sam answers a few hours after he is asked. The agent tells Owen his answer at once and deletes the three messages it booked; nothing is left open. |
| `worlds/quiet.yaml` | Sam never answers. Slack posts the agent's follow-ups a working day after its question and again a day after that, then its word to Owen, and nothing more is booked. |
| `worlds/sam_answers_the_follow_up.yaml` | A fork's changes, not a world: from a checkpoint of the quiet run, Sam answers the first follow-up three hours on. The agent, played back to the checkpoint, tells Owen and deletes what it had booked, so no more follow-ups are posted. |

## Run it

It needs the patched gVisor (`sandbox/build.sh`) and Linux with a privileged container; `sandbox/lab` is one. From the
repository root:

```bash
sandbox/build.sh
docker build -t minutehand-lab sandbox/lab
docker run --rm --privileged -v "$PWD:/work" -v "$PWD/sandbox/dist:/opt/gv:ro" minutehand-lab \
  sandbox/lab/run-example.sh examples/proactive_agent/worlds/quiet.yaml
```

On a Linux host with `runsc` and its `gvisor-bin/` on the path, the run itself is:

```bash
pip install slack_sdk
export MINUTEHAND_MODEL=openai:gpt-5-mini OPENAI_API_KEY=...   # the model that plays Sam and Owen, named as pydantic-ai
    # names it, with its provider's own key; or the offline stand-in: python tests/support/fake_model.py, with
    # MINUTEHAND_MODEL=openai-chat:people OPENAI_BASE_URL=http://127.0.0.1:8790/v1 OPENAI_API_KEY=offline
minutehand run worlds/quiet.yaml --agent agent.yaml --proxy-host 0.0.0.0 --agent-proxy-host 192.168.10.3 \
  --transparent-port 8082 --dns-port 8053 -- ./run_in_sandbox.sh
minutehand checkpoints <run>                              # <seq>: the one after wake 2, asked and followed up once
minutehand fork <run> --at <seq> --changes worlds/sam_answers_the_follow_up.yaml --proxy-host 0.0.0.0 \
  --agent-proxy-host 192.168.10.3 --transparent-port 8082 --dns-port 8053 -- ./run_in_sandbox.sh
```

`run_in_sandbox.sh` seals the sandbox's link first (`sandbox/seal.sh`: the proxy is its only way out), then starts
the agent with `runsc do` on that link (192.168.10.2, its gateway 192.168.10.3 where the proxy listens), born at the
run's start (`MINUTEHAND_STARTS_AT_NS`), and passes it the environment Minutehand hands the command. `agent.yaml` names the two control calls Minutehand makes on the sandbox
and where Slack's events reach the agent inside it. What each run showed is under "Evidence" in `docs/design.md`
(2026-10-22, when its follow-ups were a thread's sleep read from the sandbox as its timer; that reading is gone):
four wakes a day apart in the quiet world, Owen told in the same wake Sam answered in, and the fork verified at its
checkpoint.

`tests/examples/test_proactive_agent.py` holds the agent's own logic (what it books and for when, what it says, what
it deletes when an answer comes) against its own SQLite memory, with no Slack and no sandbox.
