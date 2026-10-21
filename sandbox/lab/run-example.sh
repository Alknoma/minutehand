#!/bin/sh
# Runs the example agent through Minutehand inside the sandbox, in the lab container (`sandbox/README.md`):
#   sandbox/lab/run-example.sh <world.yaml> [agent command]
# The offline model stand-in writes the people's words; the proxy sits on the sandbox's gateway. Prints the run's
# verdict and scorecard, then its wakes and calls from the run's read model. State lands in $MINUTEHAND_STATE
# (default /tmp/mh-state).
set -u
WORLD=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
COMMAND=${2:-./run_in_sandbox.sh}
STATE=${MINUTEHAND_STATE:-/tmp/mh-state}
cd "$(dirname "$0")/../.."
uv run python tests/support/fake_model.py --port 8790 > /tmp/model.log 2>&1 &
sleep 2
export MINUTEHAND_MODEL_BASE_URL=http://127.0.0.1:8790/v1 MINUTEHAND_MODEL=people MINUTEHAND_MODEL_API_KEY=offline
cd examples/proactive_agent
timeout 600 uv run minutehand run "$WORLD" --agent agent.yaml --state "$STATE" \
  --proxy-host 0.0.0.0 --proxy-port 8080 --agent-proxy-host 192.168.10.3 --telemetry-port 8081 \
  -- $COMMAND
EXIT=$?
echo "minutehand exit: $EXIT"
RUN=$(ls -t "$STATE/runs" | head -1)
echo "--- agent log (its own lines):"; grep "\[agent\]" "$STATE/runs/$RUN/agent.log" 2>/dev/null | head -20
echo "--- wakes:"; uv run minutehand query "$RUN" "SELECT wake, at, reason, woken_by, world_changes FROM wakes" --state "$STATE" 2>&1 | head -12
echo "--- calls:"; uv run minutehand query "$RUN" "SELECT wake, at, method, path, status FROM calls" --state "$STATE" 2>&1 | head -24
exit $EXIT
