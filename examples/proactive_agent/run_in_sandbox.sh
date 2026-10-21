#!/bin/sh
# Starts the agent inside the patched gVisor sandbox `agent.yaml` names (`sandbox/`), with the environment Minutehand
# hands the command (the proxy, its CA, the signing secret). The sandbox's network is its own: 192.168.10.2 on a link
# whose gateway, 192.168.10.3, is the address the proxy is reached at from inside
# (`minutehand run --proxy-host 0.0.0.0 --agent-proxy-host 192.168.10.3 ...`). The sandbox keeps its state under
# /tmp/minutehand-sandbox, where the agent file's `deadlines` and `advance` calls find it by name, and is born at the
# run's start (MINUTEHAND_STARTS_AT_NS), so its first instruction already reads simulated time.
set -eu
cd "$(dirname "$0")"
exec runsc --root /tmp/minutehand-sandbox --network=sandbox --ignore-cgroups \
  --sim-clock-at "${MINUTEHAND_STARTS_AT_NS:-0}" do \
  --name agent --state-dir /tmp/minutehand-sandbox --ip 192.168.10.2 -- \
  env HTTPS_PROXY="$HTTPS_PROXY" HTTP_PROXY="$HTTP_PROXY" NO_PROXY="$NO_PROXY" \
      SSL_CERT_FILE="$SSL_CERT_FILE" REQUESTS_CA_BUNDLE="$REQUESTS_CA_BUNDLE" \
      AGENT_SLACK_SIGNING_SECRET="$AGENT_SLACK_SIGNING_SECRET" AGENT_PORT=8740 \
  python3 agent.py
