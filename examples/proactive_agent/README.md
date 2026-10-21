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

## Run it

It needs the gVisor build with the clock patch, which is not in this repository yet (`docs/design.md`, "What
exists"): until it is, this example has not been run through Minutehand, and that is the first thing the sandbox
work will prove.

```bash
pip install slack_sdk
export MINUTEHAND_MODEL=... MINUTEHAND_MODEL_API_KEY=...   # the model that plays Sam and Owen; or the offline
                                                          # stand-in: python tests/support/fake_model.py
minutehand run worlds/quiet.yaml --agent agent.yaml --proxy-host 192.168.10.3 -- ./run_in_sandbox.sh
```

`run_in_sandbox.sh` starts the agent with `runsc do` on the sandbox's own link (192.168.10.2, its gateway
192.168.10.3 where the proxy listens) and passes it the environment Minutehand hands the command. `agent.yaml` names
the two control calls Minutehand makes on the sandbox and where Slack's events reach the agent inside it.

`tests/examples/test_proactive_agent.py` holds the agent's own logic (when a follow-up falls due, what it says and
when it stops) against its own SQLite memory, with no Slack and no sandbox.
