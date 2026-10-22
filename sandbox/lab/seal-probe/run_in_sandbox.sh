#!/bin/sh
# Starts the seal's probe in a sealed sandbox, as the example's launcher starts the agent (`sandbox/seal.sh`, then
# `runsc do`, the environment sourced inside from the file MINUTEHAND_AGENT_ENVIRONMENT names); the probe then tries
# every way out it can think of and prints what each got.
set -eu
cd "$(dirname "$0")"
sh ../../seal.sh probe
exec env -i PATH="$PATH" MINUTEHAND_AGENT_ENVIRONMENT="$MINUTEHAND_AGENT_ENVIRONMENT" \
  runsc --root /tmp/minutehand-sandbox --network=sandbox --ignore-cgroups \
  --sim-clock-at "${MINUTEHAND_STARTS_AT_NS:-0}" do \
  --name probe --state-dir /tmp/minutehand-sandbox --ip 192.168.10.2 -- \
  /bin/sh -c '. "$MINUTEHAND_AGENT_ENVIRONMENT" && exec python3 probe.py'
