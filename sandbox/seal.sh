#!/bin/sh
# Seals a sandbox's link so the proxy is its only way out. Run as root on the sandbox's gateway (the machine or
# container `runsc do --network=sandbox` runs on) BEFORE `runsc do`, with the environment Minutehand hands the agent's
# command; the rules name the link by the sandbox's name and take effect the moment `runsc do` creates it:
#
#   sandbox/seal.sh <sandbox name>        # the --name given to `runsc do`; its link is vp-<name>
#
# What the sandbox can then reach: the proxy and the telemetry receiver, at the address and ports it was told
# (HTTPS_PROXY, MINUTEHAND_AGENT_URL); every other TCP connection it opens, to any address, lands on the proxy's
# transparent listener (MINUTEHAND_TRANSPARENT_PORT), which answers a claimed host, tunnels a model API and refuses
# the rest; every DNS query lands on Minutehand's answerer (MINUTEHAND_DNS_PORT), which resolves every name to the
# proxy. Nothing is forwarded off this machine, and any other UDP is dropped. IPv4 only: `runsc do` gives the sandbox
# no IPv6 address.
set -eu
name=${1:?the sandbox name given to runsc do --name}
link="vp-$name"
chain="MINUTEHAND_$name"
proxy=${HTTPS_PROXY#*://}; proxy=${proxy%/}
gateway=${proxy%:*}
proxy_port=${proxy##*:}
transparent=${MINUTEHAND_TRANSPARENT_PORT:?the transparent listener of the proxy (minutehand run --transparent-port)}
dns=${MINUTEHAND_DNS_PORT:?the name answerer of Minutehand (minutehand run --dns-port)}
own="$proxy_port,$transparent"
if [ -n "${MINUTEHAND_AGENT_URL:-}" ]; then
  receiver=${MINUTEHAND_AGENT_URL#*://}; receiver=${receiver%%/*}; own="$own,${receiver##*:}"
fi

iptables -t nat -N "$chain" 2>/dev/null || iptables -t nat -F "$chain"
iptables -t nat -A "$chain" -p tcp -d "$gateway" -m multiport --dports "$own" -j RETURN
iptables -t nat -A "$chain" -p udp --dport 53 -j REDIRECT --to-ports "$dns"
iptables -t nat -A "$chain" -p tcp -j REDIRECT --to-ports "$transparent"
iptables -t nat -C PREROUTING -i "$link" -j "$chain" 2>/dev/null || iptables -t nat -I PREROUTING -i "$link" -j "$chain"
iptables -C FORWARD -i "$link" -j DROP 2>/dev/null || iptables -I FORWARD -i "$link" -j DROP
iptables -C INPUT -i "$link" -p udp ! --dport "$dns" -j DROP 2>/dev/null || iptables -I INPUT -i "$link" -p udp ! --dport "$dns" -j DROP
echo "minutehand: sandbox $name sealed: its TCP goes to $gateway:$transparent, its lookups to $gateway:$dns, nothing is forwarded" >&2
