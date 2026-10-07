"""Stands in for a patched gVisor sandbox around the agent: a clock, and the agent's in-process timers; a timer the
clock passes fires by calling the agent's own /timer, as its own code would run.

    python sandbox_stub.py STATE deadlines | advance NANOSECONDS
"""

import json
import sys
import urllib.request
from pathlib import Path

state_file = Path(sys.argv[1])
state = json.loads(state_file.read_text())
if sys.argv[2] == "deadlines":
    ahead = [t - state["now_ns"] for t in state["timers"] if t > state["now_ns"]]
    print(json.dumps({"idle": True, "earliest_ns": min(ahead) if ahead else -1}))
else:
    state["now_ns"] += int(sys.argv[3])
    due = [t for t in state["timers"] if t <= state["now_ns"]]
    state["timers"] = [t for t in state["timers"] if t > state["now_ns"]]
    state_file.write_text(json.dumps(state))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in due:
        opener.open(urllib.request.Request(state["fire_url"], data=b"{}", method="POST"), timeout=30).close()
