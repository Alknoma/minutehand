# OpenAI Agents SDK

`agent.py` is an Agents SDK `Agent[Memory]` with three `function_tool`s and a typed run context, behind three
endpoints Minutehand calls. `../README.md` describes the agent and the contract all five recipes share.

## The lines that connect it

```python
@dataclass
class Memory:  # the run context, recalled from the store on every wake
    work: str | None = None
    waits: dict[str, Wait] = field(default_factory=dict)  # person -> Wait(expected_by, asks)


@function_tool
def remember_wait(memory: RunContextWrapper[Memory], email: str, expected_by: str) -> str:
    waits = memory.context.waits
    waits[email] = Wait(datetime.fromisoformat(expected_by), waits[email].asks + 1 if email in waits else 1)
    return f"waiting on {email} until {expected_by}"


@remembering
def wake(request):  # POST /wake
    now = datetime.fromisoformat(request["now"])
    if request["reason"] == "start":
        memory.work = WORK
        run(now, "Nobody has been asked yet.")  # asyncio.run(Runner.run(agent, situation, context=memory))
        return
    for email, wait in list(memory.waits.items()):  # reason "due": whatever has passed its date
        if wait.expected_by <= now:
            run(
                now,
                f"No answer yet from {email}, expected by {wait.expected_by.isoformat()}. Follow-ups sent: {wait.asks - 1}.",
            )


def report():  # GET /report, after every wake
    recall()
    if memory.work is None:
        return {"status": "idle", "next_wake": None}
    if not memory.waits:
        return {"status": "done", "next_wake": None}
    return {"status": "idle", "next_wake": min(w.expected_by for w in memory.waits.values()).isoformat()}
```

- **The wake handler** makes one `Runner.run` per wake, with the situation as its input and the same `Memory` as
  its `context`. A Slack event from a person the agent waits on is one more run (`message`).
- **`next_wake`** is read from the run context the tools write: the SDK hands `RunContextWrapper[Memory]` to any
  tool whose first parameter asks for it, and never shows it to the model.
- **The Slack tool** is a plain `function_tool` on `slack_sdk.WebClient`. It calls `slack.com`; Minutehand's
  `HTTPS_PROXY` and CA in the agent's environment take the call to the fake Slack.

## What it remembers

Its work and the waits are kept in `minutehand.agent.store`, the one import that ties the agent to Minutehand:
`recall()` reads them into `Memory` before every wake, Slack event and report, and `keep()` writes them back, in one
batch, after every wake and event (the `@remembering` handlers). In production the store passes to the backend
`store.configure` names (`MemoryBackend()` here; `SqliteBackend(path)` keeps it across restarts). Under Minutehand it
is the run's own memory: every run starts from the scenario's, and a fork from any checkpoint starts from what the
agent remembered there, with nothing to snapshot or restore (`../../../docs/agent-contract.md`). What the framework
keeps in its own objects past a wake is not part of the run, so nothing here is kept there.

```python
store.configure(store.MemoryBackend())
waits_kept = store.collection("waits")  # person -> {"expected_by": ISO 8601, "asks": n}


def recall() -> None:
    global memory
    work = store.get("work")
    waits = {
        email: Wait(datetime.fromisoformat(str(kept["expected_by"])), int(str(kept["asks"])))
        for email, kept in waits_kept.list()
    }
    memory = Memory(work=work if isinstance(work, str) else None, waits=waits)
```

## The model

`OpenAIChatCompletionsModel(openai_client=AsyncOpenAI(base_url=os.environ.get("MODEL_BASE_URL", ...)))`: the
SDK's own way to name a client, pointed at `../fake_model.py`, over chat completions (the fake does not speak the
Responses API the SDK uses by default). The call goes straight to `127.0.0.1`, which is in the `NO_PROXY`
Minutehand hands out. With a real model, pass `model="gpt-4.1-mini"` alone and set `OPENAI_API_KEY`; the calls go
to `api.openai.com`, which Minutehand tunnels, or records with `--record-model-calls`.

Tracing is switched off (`set_tracing_disabled(True)`): the SDK's traces go to OpenAI's tracing API, which an
offline run cannot reach, and Minutehand receives OpenTelemetry, which they are not. This recipe sends Minutehand
no telemetry.

## Run both scenarios

From this folder:

```bash
python ../fake_model.py &      # once: the fake model on 127.0.0.1:8790

uv run --group recipes minutehand run scenario_late.yaml --agent agent.yaml -- python agent.py
# exits 0: Rosa answers after the follow-up; the agent tells Owen "... says: The lakeside hall, booked for the 14th."

uv run --group recipes minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
# exits 0: one follow-up two days in, then Owen is told Rosa never answered; no follows_up_when_due finding
```

Outside this checkout: `pip install openai-agents slack_sdk minutehand` in the agent's Python, and Minutehand installed on its
own (`uv tool install .` from a checkout); then `minutehand run …` without `uv run`.
