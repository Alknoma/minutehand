# The reference agent

`examples/reference_agent/` is a small product built the way real proactive agents are, used to prove that
Minutehand's core holds on something realistic, with no provider at all. It is also the worked example to copy
when wiring your own agent in.

**The job:** "Get a venue confirmed for the team's offsite on Friday, and tell Owen its booking reference." It
looks venues up, asks the model which one and what to write, emails the venue's contact, waits, follows up if no
answer comes, reads the answer with the model and tells the owner.

## What it is made of

| Part | File | What it shows Minutehand |
|---|---|---|
| API process | `api.py` | Takes the wake and answers at once; the work happens later. `GET /report` says WORKING while any job is queued or running (`REFERENCE_REPORT=naive` says IDLE as soon as the worker has picked a job up). `POST /inbound/email` takes replies, checking an HMAC signature. Sends the owner a thank-you with `requests`. |
| Worker process | `worker.py` | Polls the job queue in the database on its own timer and makes every other outbound call with one pooled `httpx` client, so its connection to the model API stays open across wakes. |
| One command | `run.py` | Starts both; `REFERENCE_WORKER=detached` leaves the worker running when stopped (a separate service a restart does not touch). |
| Database | `store.py` | SQLite file (default) or a Firestore emulator (`REFERENCE_FIRESTORE=host:port`), through its REST API on localhost: writes the proxy never sees. |
| Telemetry | `telemetry.py` | The official SDK with a `BatchSpanProcessor` at its default delay. `REFERENCE_TELEMETRY=http` (environment only), `grpc` (exporter built in code), or `none`. GenAI attributes on each model call; `traceparent` on every outbound call and carried from the API to the worker through the job. |
| State hooks | `hooks.py` | `snapshot`, `restore`, `busy` (exit 0 while a job is in flight), `fingerprint` (a digest of the database, order-independent, and of what each process holds in memory). `REFERENCE_RESTORE_BUG=next` restores the wrong snapshot on purpose. |
| Outside world | `outside.py` | A model API (`model.localhost`, chat completions, deterministic, streamed with `stream: true`, held `MODEL_DELAY` seconds) and a venue search (`search.localhost`), over HTTPS under their own CA. |

Behaviours (`REFERENCE_BEHAVIOUR`): `diligent`, `forgetful` (never follows up), `nagging` (every 12 hours),
`liar` (reports DONE right after asking), `slow` (`REFERENCE_SLOW_SECONDS` of work per job).
`REFERENCE_REAL_CLOCK=1` computes the follow-up from the machine's clock instead of the wake's `now`.

`REFERENCE_APPROVER=<email>` makes telling the owner wait on that person's approval in the agent's own web app
(`GET /approvals`, `POST /approvals/<id>/decision`, signed in with `REFERENCE_APPROVER_TOKEN`): it thanks the venue,
raises the approval, reminds the approver every `REFERENCE_APPROVAL_FOLLOW_UP_HOURS` (24), and sends the tell only once
approved; `heedless` sends it whichever way it was decided. `agent.yaml` declares the inbox (`docs/inboxes.md`);
`scenario_approved.yaml`, `scenario_rejected.yaml`, `scenario_approver_silent.yaml` and `scenario_approver_away.yaml`
play it.

## Running it

Every HTTP server in the example skips `http.server`'s reverse DNS lookup of this machine's name, which takes over 30 seconds on some Macs; copy that `server_bind` if you build on the standard library.

```bash
cd examples/reference_agent
python outside.py --dir .outside &                       # prints the model and search addresses
export REFERENCE_MODEL_URL=https://model.localhost:<port> REFERENCE_SEARCH_URL=https://search.localhost:<port>
export REFERENCE_EXTRA_CA=.outside/ca.pem                # the agent reaches the tunnelled model API directly
minutehand run scenario.yaml --agent agent.yaml --model-host model.localhost --upstream-ca .outside/ca.pem \
  -- python run.py
```

`agent.yaml` declares the email host `acknowledge` with `replies` (the venue's answer is delivered, signed, to
`/inbound/email`), the search `pass_through`, and all four state hooks. Scenarios: `scenario.yaml` (Rosa answers
her first email), `scenario_silent.yaml` (she never does), `scenario_lenient.yaml` (only "Rosa was asked" is
expected), `scenario_directed.yaml` (Owen adds a requirement that changes nothing the agent reports),
`scenario_long.yaml` (sixty days, three hundred wakes).

## What it measured

On `integration-main` before this work, through the real CLI: Rosa could not answer an email (the diligent agent
failed and the forgetful one was unscored); snapshots of the worker mid-job were labelled restorable; a restore of
another moment with the same report, and one that left the worker running, were verified; the gRPC exporter
delivered nothing; the liar passed a lenient scenario; the nagging agent scored as the diligent one. After: the
diligent agent passes in about 3 s of real time; the forgetful one fails `no_follow_up` with 2.2 days lost; the
nagging one fails `nagged` with 10 early follow-ups; the liar is `Not finished`; checkpoints wait for the worker;
both wrong restores are refused by the fingerprint; gRPC spans are received and placed. `tests/architecture/`
holds the proofs that run in the default suite.

## Copying it

- Report WORKING while any job of yours is queued or running: that is the contract for an agent with background
  work. Declare `busy` as well, so a checkpoint is confirmed rather than inferred from quiet.
- Fingerprint every place your state lives, including what a long-running process caches: a database digest alone
  passes a restore that left a process holding the future.
- Take "now" from the wake request only. A deadline computed from the machine clock lands weeks away from the
  simulated one (`REFERENCE_REAL_CLOCK=1` fails `no_follow_up`).
- Run `minutehand doctor --agent agent.yaml -- <your command>` once: it names every client that would go around
  the proxy.
