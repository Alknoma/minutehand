# A small proactive agent

`agent.py` gets answers from people and passes them on. Its work is its own: `work.json` lists each ask (who answers,
the question, who is told), and the code names no one. It asks in Slack, follows up at most twice, a working day
apart, and tells the answer on, or that none came. Nothing hands it this work and nothing outside it wakes it: its
own scheduler, one thread waiting on the next deadline, decides when it next acts, and its state is its own SQLite
file (`proactive_agent.db`). It imports nothing of Minutehand's and has no endpoint for it. It is plain Python on
stock `slack_sdk`, about 150 lines, and the whole of it is the agent's.

Under Minutehand it runs unchanged, in a sandbox whose clock Minutehand owns (`docs/design.md`, "A sandbox whose
clock Minutehand owns"): the sandbox's clock reads simulated time, so a day's wait passes in a jump; the deadline its
scheduler thread waits on is read from the sandbox as the agent's next wake; and the only way out of the sandbox is
Minutehand's proxy, where the fake Slack answers. A fork from any checkpoint plays the agent's recorded inputs back
into a fresh sandbox and the agent stands where it stood, database and timers included.

| World | What happens |
|---|---|
| `worlds/answers.yaml` | Sam answers a few hours after he is asked. The agent tells Owen his answer at once; nothing is left open and its scheduler waits on nothing. |
| `worlds/quiet.yaml` | Sam never answers. The agent follows up a working day after its question, again a day after that, then tells Owen, and stops. |
| `worlds/sam_answers_the_follow_up.yaml` | A fork's changes, not a world: from a checkpoint of the quiet run, Sam answers the first follow-up three hours on. The agent, played back to the checkpoint, tells Owen and follows up no more. |

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
export MINUTEHAND_MODEL=... MINUTEHAND_MODEL_API_KEY=...   # the model that plays Sam and Owen; or the offline
                                                          # stand-in: python tests/support/fake_model.py
minutehand run worlds/quiet.yaml --agent agent.yaml --proxy-host 0.0.0.0 --agent-proxy-host 192.168.10.3 \
  --transparent-port 8082 --dns-port 8053 -- ./run_in_sandbox.sh
minutehand checkpoints <run>                              # seq 27, after wake 2: asked, followed up once
minutehand fork <run> --at 27 --changes worlds/sam_answers_the_follow_up.yaml --proxy-host 0.0.0.0 \
  --agent-proxy-host 192.168.10.3 --transparent-port 8082 --dns-port 8053 -- ./run_in_sandbox.sh
```

`run_in_sandbox.sh` seals the sandbox's link first (`sandbox/seal.sh`: the proxy is its only way out), then starts
the agent with `runsc do` on that link (192.168.10.2, its gateway 192.168.10.3 where the proxy listens), born at the
run's start (`MINUTEHAND_STARTS_AT_NS`); the agent inside sources the file Minutehand wrote its environment to
(`MINUTEHAND_AGENT_ENVIRONMENT`), so nothing of it is on a command line. That environment is what `agent.yaml`
declares (`docs/agent-environment.md`): the machine's, then `agent.env`, the agent's own file with its Slack token
and its port, which Minutehand reads itself and whose names, never values, the run records. `agent.yaml` names the two control calls Minutehand makes on the sandbox
and where Slack's events reach the agent inside it. What each run showed is under "Evidence" in `docs/design.md`
(2026-10-22): four wakes a day apart in the quiet world, Owen told in the same wake Sam answered in, and the fork
verified at its checkpoint.

`tests/examples/test_proactive_agent.py` holds the agent's own logic (when a follow-up falls due, what it says and
when it stops) against its own SQLite memory, with no Slack and no sandbox.
