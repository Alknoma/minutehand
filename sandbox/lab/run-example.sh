#!/bin/sh
# Runs the example agent through Minutehand inside a sealed sandbox, in the lab container (`sandbox/README.md`):
#   sandbox/lab/run-example.sh <world.yaml> [agent.yaml] [agent command]
# The offline model stand-in writes the people's words; the proxy sits on the sandbox's gateway, with its transparent
# listener and the name answerer the seal sends everything to. Prints the run's verdict and scorecard, then its wakes
# and calls from the run's read model, and the seal's packet counters. State lands in $MINUTEHAND_STATE (default
# /tmp/mh-state). With `sandbox/lab/seal-probe/probe.yaml` and its launcher, runs the seal's probe instead.
set -u
WORLD=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
AGENT=${2:-agent.yaml}
COMMAND=${3:-./run_in_sandbox.sh}
STATE=${MINUTEHAND_STATE:-/tmp/mh-state}
cd "$(dirname "$0")/../.."
uv run python tests/support/fake_model.py --port 8790 > /tmp/model.log 2>&1 &
sleep 2
export MINUTEHAND_MODEL_BASE_URL=http://127.0.0.1:8790/v1 MINUTEHAND_MODEL=people MINUTEHAND_MODEL_API_KEY=offline
cd examples/proactive_agent
timeout 600 uv run minutehand run "$WORLD" --agent "$AGENT" --state "$STATE" \
  --proxy-host 0.0.0.0 --proxy-port 8080 --agent-proxy-host 192.168.10.3 --telemetry-port 8081 \
  --transparent-port 8082 --dns-port 8053 \
  -- $COMMAND
EXIT=$?
echo "minutehand exit: $EXIT"
RUN=$(ls -t "$STATE/runs" | head -1)
echo "--- agent log (its own lines):"; grep "\[agent\]\|\[probe\]" "$STATE/runs/$RUN/agent.log" 2>/dev/null | head -20
echo "--- wakes:"; uv run minutehand query "$RUN" "SELECT wake, at, reason, woken_by, world_changes FROM wakes" --state "$STATE" 2>&1 | head -12
echo "--- calls:"; uv run minutehand query "$RUN" "SELECT wake, at, method, host, path, status FROM calls" --state "$STATE" 2>&1 | head -24
echo "--- the seal's counters:"; iptables -L FORWARD -v -n | grep DROP; iptables -t nat -L -v -n | grep -A4 "Chain MINUTEHAND"
exit $EXIT
