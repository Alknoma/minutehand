"""A sandbox whose clock the run owns, standing in for a patched gVisor: its state is a JSON file holding its clock
and the deadlines of the agent's in-process timers; a timer whose deadline the clock passes acts as the agent's own
code would, by messaging dania through the test providers.

    python fake_sandbox.py STATE deadlines
    python fake_sandbox.py STATE advance NANOSECONDS
"""

import json
import sys
import urllib.request
from pathlib import Path

state_file = Path(sys.argv[1])
state = json.loads(state_file.read_text())
if sys.argv[2] == "deadlines":
    ahead = [t - state["now_ns"] for t in state["timers"] if t > state["now_ns"]]
    print(json.dumps({"idle": True, "tasks": 2, "timed": len(ahead), "earliest_ns": min(ahead) if ahead else -1}))
else:
    state["now_ns"] += int(sys.argv[3])
    due = [t for t in state["timers"] if t <= state["now_ns"]]
    state["timers"] = [t for t in state["timers"] if t > state["now_ns"]]
    for t in due:
        if t in state.get("quiet_timers", []):
            continue  # a runtime's own timer: it fires and does nothing the run can see
        body = json.dumps({"to": "dania@example.com", "text": "Following up on the contract."}).encode()
        request = urllib.request.Request(
            state["base"] + "/testchat/messages", data=body, headers={"content-type": "application/json"}
        )
        urllib.request.urlopen(request).close()
        state["fired_at_ns"].append(state["now_ns"])
    state_file.write_text(json.dumps(state))
