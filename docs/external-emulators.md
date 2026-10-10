# External emulators

A fake that Minutehand does not hold: any service's emulator, in any language, run as its own process or container.
Declare it, forward the service's real host to it, and the agent's calls go there, through the proxy, with no
change to the agent and none to Minutehand. Built and tested: `domain/emulator.py`, `adapters/emulator/`, the
`forward` path of `adapters/proxy/addon.py`, `application/emulators.py`, `tests/emulators/`,
`tests/serve/test_emulator_worlds.py`.

## The declaration

The worked example, `tests/agents/external_emulator/`, forwards the payments API to
[stripe-mock](https://github.com/stripe/stripe-mock), downloaded as it is:

```yaml
outbound:
  - host: api.stripe.com
    kind: forward
    emulator: stripe
    # strip: /api   prefix: /v2   host_header: upstream    (optional: path mapping, the Host the emulator sees)
emulators:
  - name: stripe
    upstream: {url: "http://127.0.0.1:{port}"}           # or https://... with `ca: emulator-ca.pem`, or unix:///path.sock
    command: [docker, run, --rm, --name, minutehand-ext-stripe, -p, "{port}:12111", "stripe/stripe-mock:latest"]
    ready: {kind: http, path: /v1/charges, status: 401}  # or {kind: tcp}, or {kind: log, line: "Listening"}
    ready_within: PT60S
    health: {every: PT1S, fails: 2}                      # with `path:`, an HTTP check; without, a TCP one
    faithful: [{status: 402}]                            # errors the real API gives; any 4xx already is one
    # not_implemented: [{status: 501}]                   # the default; or {at: "errors[*].extensions.code", equals: NOT_IMPLEMENTED}
    # answer_within: PT30S                               # a call with no byte of answer after this is answered 504
```

`{port}` is a free port Minutehand picks for each start. With no `command`, the emulator is already running and
Minutehand attaches to it. In `minutehand serve`, `CreateWorld.emulators` takes the same entries: one emulator per
server, started with the first world that declares it and shared by every world declaring it the same; a world
declaring a running name otherwise is refused 409.

```bash
cd tests/agents/external_emulator
minutehand run scenario.yaml --agent agent.yaml      # agent.py is a ten-line stand-in for an agent
```

## What you get

- **The call, forwarded and kept verbatim.** Request and answer cross unchanged and streamed. On the forwarded
  copy only, Minutehand adds `x-minutehand-world` (the run or standing world), `x-minutehand-wake` and
  `x-minutehand-time` (the simulated time, ISO 8601: the whole time contract; an emulator that stamps simulated
  times reads it), and replaces `traceparent` with the agent's trace and Minutehand's span of the call as the
  parent (a trace begun for it when the agent sent none). Nothing else differs:
  `test_a_forwarded_call_reaches_the_emulator_unchanged_but_for_the_documented_headers` compares every header and
  byte. The kept copy is the agent's own call, redacted as a pass-through's (`docs/capture.md`), with
  `Captured.mode: forward`, `answered_by: emulator`, `emulator`, `operation` (a GraphQL operation's name, else the
  method and path) and `forwarded_traceparent`.
- **What each answer was** (`Exchange.outcome`): `answered`; `refused` (a 4xx, or an error declared `faithful`);
  `not_implemented` (summarised per operation at the end of the run, and listed for review); `internal_error` (a
  5xx nobody declared faithful: the emulator's own failure, listed for review); `unavailable`.
- **Its failure, as the environment's.** One that never comes up refuses the run before the agent starts, with the
  end of its log. One that dies, fails its health check, or accepts a call and answers nothing within
  `answer_within`: the agent's call is answered 502 (504 for no answer) with
  `{"error": "external emulator stripe is unavailable: ...", "emulator": "stripe"}` and never reaches the real
  host; every later call is answered so at once; the call is kept `unavailable`; the run stops
  `ENVIRONMENT_FAILED`, its verdict says so and it exits 2. Minutehand never restarts an emulator.
- **Health in the record.** `ready`, `unhealthy`, `died` and `stopped` are versions of the entity
  `(<name>, record, health)` in the world's log, with the reason and the end of the log, and spans
  `minutehand.emulator <health>` in Minutehand's export.
- **Telemetry.** Minutehand's span of each forwarded call carries `minutehand.emulator`, `minutehand.operation`,
  `minutehand.call.outcome`, the status and the times, never a body, and its span id is the parent the emulator
  was handed. An emulator Minutehand starts is given the OTLP variables the agent is (and `OTEL_SERVICE_NAME`
  its name): its spans are kept with the run, placed in the wake they began in, and found from the call by
  `forwarded_traceparent`.

Killing the example's container mid-run (`EXAMPLE_PAUSE=3`, then `docker kill minutehand-ext-stripe`):

```
run 2e08e532dfcb: payments_example
  Environment failed: the run stopped because an external emulator it used was unavailable, so the agent is not judged on this run.
  stopped at 2026-08-24 10:00 UTC (simulated) because an external emulator the run used was unavailable
  emulator stripe broke off: [Errno 54] Connection reset by peer

external emulators
  stripe: 4 calls, 2 answered; 2 unanswered: the emulator was unavailable, first POST api.stripe.com/v1/customers (wake 2)

fail (1)
  emulator: external emulator stripe was unavailable (emulator stripe broke off: [Errno 54] Connection reset by peer); the first call it failed: POST api.stripe.com/v1/customers at wake 2, answered 502; 2 call(s) in all. The run's environment failed, not the agent. Its log ends:
...
Request: GET /v1/customers
```

`tests/emulators/test_downloaded.py` does this with Docker (`uv run pytest -m docker`); the default suite drives
the same paths with a stand-in process and local servers.

## What you do not get

- **Scoring of its state.** Minutehand cannot see inside the emulator: its calls are scored only as calls. A
  ticket filed there is not a ticket to `ticket_created`, a person cannot be scripted to act on it, and nothing is
  seeded into it from the scenario.
- **Rewind.** Its state is outside the run's record. A fork from a checkpoint after the agent's first call to it
  is refused, naming it; one from before is allowed.
- **A restart.** It stays down once it fails.

The other choice, for a fake that should be scored and rewound, is a provider in Python, in the process: a package
naming itself under the entry-point group `minutehand.providers` (`adapters/proxy/registry.py`), with the layout in
`CONTRIBUTING.md`. That route has not been tried from outside this repository.

## Where this comes from

LocalStack's `ProxiedDockerContainerExtension` starts a container with the gateway, routes the requests for its host
to it, health-checks it before routing, and removes it on shutdown: the same shape as `command` and `forward`, but
it is a Python package loaded into LocalStack, which this is not. Testcontainers' wait strategies (an HTTP path and
status, a listening port, a log line, each with a startup timeout) are `ready`, and its log output on a failed start
is the refusal's log tail.
