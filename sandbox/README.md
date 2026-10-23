# The sandbox whose clock Minutehand owns

Every agent runs in a gVisor sandbox whose clock Minutehand moves (`docs/design.md`, "A sandbox whose clock
Minutehand owns"). Stock gVisor reads the host's clock; this directory holds what makes it Minutehand's:

| File | What it is |
|---|---|
| `gvisor-sim-clock.patch` | The patch to gVisor's `go` branch at `c324264` (release 20261012.0): an offset on the sandbox's realtime and monotonic clocks, applied on the syscall path and in the VDSO parameters; every blocked task's deadline recorded; `runsc debug --deadlines` and `runsc debug --advance-clock=<duration>`; `runsc --sim-clock-at=<ns>` (born at a given time); `runsc do --name --state-dir` |
| `build.sh` | Builds `runsc` with the patch and the sidecars it loads from `gvisor-bin/` beside it, in a `golang:1.27` container, for the machine's own architecture |
| `seal.sh` | Seals a sandbox's link on its gateway, before `runsc do`: every connection to the proxy's transparent listener, every lookup to Minutehand's name answerer, nothing forwarded. The proxy is then the sandbox's only way out, whatever its clients do with `HTTPS_PROXY` |
| `lab/Dockerfile`, `lab/run-example.sh` | A Linux container with the tools a run needs (`iproute2`, `iptables`, `uv`, `slack_sdk`) that runs the example agent through Minutehand inside the sealed sandbox, with the offline model stand-in writing the people's words |
| `lab/seal-probe/` | A program that, inside a sealed sandbox, ignores the proxy and tries every way out; run like the example, it prints what each try got |

## Build it

```bash
sandbox/build.sh            # -> sandbox/dist/runsc, sandbox/dist/gvisor-bin/*
```

`runsc` finds its sidecars in `gvisor-bin/` next to itself: keep the two together wherever they go.

## Run the example in it

The sandbox needs Linux and a privileged container (or a Linux host) for `runsc do`'s network namespace. From the
repository root, on any machine with Docker:

```bash
docker build -t minutehand-lab sandbox/lab
docker run --rm --privileged -v "$PWD:/work" -v "$PWD/sandbox/dist:/opt/gv:ro" minutehand-lab \
  sandbox/lab/run-example.sh examples/proactive_agent/worlds/quiet.yaml
```

The script starts the offline model stand-in (`tests/support/fake_model.py`), runs `minutehand run` with the proxy on
the sandbox's gateway (`--proxy-host 0.0.0.0 --agent-proxy-host 192.168.10.3 --transparent-port 8082 --dns-port
8053`), and prints the run's verdict, scorecard, wakes, calls and the seal's packet counters. The second and third
arguments are the agent file and the command; the defaults are the example's.

## The seal

`run_in_sandbox.sh` runs `sandbox/seal.sh <name>` before `runsc do`. On the gateway it puts, for the link `vp-<name>`
that `runsc do` is about to create: a `nat PREROUTING` chain that lets TCP to the proxy's own ports and the receiver's
through (`HTTPS_PROXY`, `MINUTEHAND_AGENT_URL`), redirects UDP 53 to the name answerer (`MINUTEHAND_DNS_PORT`) and
every other TCP connection to the transparent listener (`MINUTEHAND_TRANSPARENT_PORT`); a `FORWARD` drop, so nothing
from the link leaves the machine; and an `INPUT` drop for any other UDP. The name answerer resolves every name to the
proxy, so a client that ignores the proxy connects to it by any name and names the host in its TLS ClientHello,
which is what the transparent listener routes on. What goes out for real is what the proxy tunnels: the model hosts
(`--model-host`, the public providers by default). This is not a security boundary (the gateway is the machine
Minutehand runs on); it is what makes every call land on a fake with no client configured for it.

The probe, run like the example with its own agent file and launcher:

```bash
docker run --rm --privileged -v "$PWD:/work" -v "$PWD/sandbox/dist:/opt/gv:ro" minutehand-lab \
  sandbox/lab/run-example.sh examples/proactive_agent/worlds/quiet.yaml \
  ../../sandbox/lab/seal-probe/probe.yaml ../../sandbox/lab/seal-probe/run_in_sandbox.sh
```

With every proxy variable removed and `NO_PROXY=*`, it should print: both names resolving to `192.168.10.3`; a direct
call to `slack.com` answered by the Slack fake (501, the method refused by name); `example.com` and `1.1.1.1` refused
by the proxy (502, no provider claims the host); `api.anthropic.com` tunnelled out (its real 401); a TCP connection to
`8.8.8.8:9999` accepted by the proxy and closed; UDP to `8.8.8.8:4444` unanswered. The run itself ends as the agent
exiting, which is what the probe does when done.

## What the two control calls print

```
runsc --root <state> debug --deadlines <name>
{"idle":true,"now_ns":1787562000218621000,"tasks":2,"timed":2,"earliest_ns":86399981379000,"waits":[{"task":1,"ns":86399981379000},{"task":2,"ns":497000000}]}

runsc --root <state> debug --advance-clock=86400s <name>
```

`idle`: every task is blocked. `now_ns`: the sandbox's realtime clock. These two are all Minutehand reads. `waits`:
each blocked task's deadline, as nanoseconds from now; `earliest_ns` the smallest, -1 for none. Minutehand no longer
reads them: no deadline in the sandbox is a wake (`docs/design.md`, "How the clock knows what is next"). `advance-clock` moves both clocks forward by the
duration and tells every timer the clock was set, so a sleep, futex, poll or epoll timeout past the new time
returns at once. `--sim-clock-at=<ns since the epoch>` on `runsc` births the sandbox at that realtime, with the
first application instruction already reading it; Minutehand hands the run's start to the agent's command as
`MINUTEHAND_STARTS_AT_NS`.
