#!/bin/sh
# Starts the agent inside the patched gVisor sandbox `agent.yaml` names, with the environment Minutehand hands the
# command (the proxy, its CA, the signing secret). The sandbox's network is its own: 192.168.10.2 on a link whose
# gateway, 192.168.10.3, is where Minutehand's proxy must listen (`minutehand run --proxy-host 192.168.10.3 ...`).
# `runsc` here is the build with the clock patch (docs/design.md, "Evidence"), not a release.
set -eu
cd "$(dirname "$0")"
exec runsc --network=sandbox --ignore-cgroups do --name proactive_agent --ip 192.168.10.2 -- \
  env HTTPS_PROXY="$HTTPS_PROXY" HTTP_PROXY="$HTTP_PROXY" NO_PROXY="$NO_PROXY" \
      SSL_CERT_FILE="$SSL_CERT_FILE" REQUESTS_CA_BUNDLE="$REQUESTS_CA_BUNDLE" \
      AGENT_SLACK_SIGNING_SECRET="$AGENT_SLACK_SIGNING_SECRET" AGENT_PORT=8740 \
  python3 agent.py
