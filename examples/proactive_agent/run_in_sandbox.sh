#!/bin/sh
# Starts the agent inside the patched gVisor sandbox `agent.yaml` names (`sandbox/`). The sandbox's network is its
# own: 192.168.10.2 on a link whose gateway, 192.168.10.3, is the address the proxy is reached at from inside
# (`minutehand run --proxy-host 0.0.0.0 --agent-proxy-host 192.168.10.3 ...`). The sandbox keeps its state under
# /tmp/minutehand-sandbox, where the agent file's `deadlines` and `advance` calls find it by name, and is born at the
# run's start (MINUTEHAND_STARTS_AT_NS), so its first instruction already reads simulated time. Its link is sealed
# (`sandbox/seal.sh`): every connection it opens lands on the proxy, every lookup on Minutehand's answerer, and nothing
# is forwarded, so the proxy is its only way out, whatever its clients do with HTTPS_PROXY.
#
# The agent's environment (docs/agent-environment.md) crosses into the sandbox explicitly: Minutehand started this
# script with it and wrote the same to the file MINUTEHAND_AGENT_ENVIRONMENT names; `runsc` is started with nothing
# but PATH and that name, and the command inside sources the file. No value is on any command line.
set -eu
cd "$(dirname "$0")"
# seal the sandbox's link first: the rules name the link and take effect the moment `runsc do` creates it
sh ../../sandbox/seal.sh agent
exec env -i PATH="$PATH" MINUTEHAND_AGENT_ENVIRONMENT="$MINUTEHAND_AGENT_ENVIRONMENT" \
  runsc --root /tmp/minutehand-sandbox --network=sandbox --ignore-cgroups \
  --sim-clock-at "${MINUTEHAND_STARTS_AT_NS:-0}" do \
  --name agent --state-dir /tmp/minutehand-sandbox --ip 192.168.10.2 -- \
  /bin/sh -c '. "$MINUTEHAND_AGENT_ENVIRONMENT" && exec python3 agent.py'
