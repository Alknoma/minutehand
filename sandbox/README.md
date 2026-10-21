# The sandbox whose clock Minutehand owns

Every agent runs in a gVisor sandbox whose clock Minutehand moves (`docs/design.md`, "A sandbox whose clock
Minutehand owns"). Stock gVisor reads the host's clock; this directory holds what makes it Minutehand's:

| File | What it is |
|---|---|
| `gvisor-sim-clock.patch` | The patch to gVisor's `go` branch at `c324264` (release 20261012.0): an offset on the sandbox's realtime and monotonic clocks, applied on the syscall path and in the VDSO parameters; every blocked task's deadline recorded; `runsc debug --deadlines` and `runsc debug --advance-clock=<duration>`; `runsc --sim-clock-at=<ns>` (born at a given time); `runsc do --name --state-dir` |
| `build.sh` | Builds `runsc` with the patch and the sidecars it loads from `gvisor-bin/` beside it, in a `golang:1.27` container, for the machine's own architecture |
| `lab/Dockerfile`, `lab/run-example.sh` | A Linux container with the tools a run needs (`iproute2`, `iptables`, `uv`, `slack_sdk`) that runs the example agent through Minutehand inside the sandbox, with the offline model stand-in writing the people's words |

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
the sandbox's gateway (`--proxy-host 0.0.0.0 --agent-proxy-host 192.168.10.3`), and prints the run's verdict,
scorecard, wakes and calls. A second argument is passed to `minutehand run` as the world file's command; the default
is the example's `run_in_sandbox.sh`.

## What the two control calls print

```
runsc --root <state> debug --deadlines <name>
{"idle":true,"now_ns":1787562000218621000,"tasks":2,"timed":2,"earliest_ns":86399981379000,"waits":[{"task":1,"ns":86399981379000},{"task":2,"ns":497000000}]}

runsc --root <state> debug --advance-clock=86400s <name>
```

`idle`: every task is blocked. `now_ns`: the sandbox's realtime clock. `waits`: each blocked task's deadline, as
nanoseconds from now; `earliest_ns` the smallest, -1 for none. `advance-clock` moves both clocks forward by the
duration and tells every timer the clock was set, so a sleep, futex, poll or epoll timeout past the new time
returns at once. `--sim-clock-at=<ns since the epoch>` on `runsc` births the sandbox at that realtime, with the
first application instruction already reading it; Minutehand hands the run's start to the agent's command as
`MINUTEHAND_STARTS_AT_NS`.
