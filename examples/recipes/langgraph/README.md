# LangGraph

`agent.py` is a LangGraph graph of the usual shape (a chat model with tools bound, a `ToolNode`, `tools_condition`
between them) behind three endpoints Minutehand calls, with its work and the waits kept between wakes in
`minutehand.agent.store`. `../README.md` describes the
agent and the contract all five recipes share.

## The lines that connect it

```python
class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    waits: Annotated[dict[str, Wait | None], merge_waits]  # person -> {expected_by, asks}; None removes one
    work: str


store.configure(store.MemoryBackend())  # production: this process; under Minutehand: the run's memory
waits_kept = store.collection("waits")


def run(work, waits, text):  # one run of the graph from the store, and the waits it ends with written back
    ended = graph.invoke({"messages": [HumanMessage(text)], "work": work, "waits": waits})
    with store.batch() as kept:
        kept.put("work", work)
        ...  # each wait in ended["waits"] put, each one gone deleted, collection="waits"


def wake(request):  # POST /wake
    now = datetime.fromisoformat(request["now"])
    if request["reason"] == "start":
        run(WORK, {}, situation(now, WORK, "Nobody has been asked yet."))
        return
    work, waits = recalled()  # its work and the waits, as the store holds them now
    for email, wait in waits.items():  # reason "due": whatever has passed its date
        if datetime.fromisoformat(wait["expected_by"]) <= now:
            overdue = (
                f"No answer yet from {email}, expected by {wait['expected_by']}. Follow-ups sent: {wait['asks'] - 1}."
            )
            run(work, recalled()[1], situation(now, work, overdue))


def report():  # GET /report, after every wake
    work, waits = recalled()
    if work is None:
        return {"status": "idle", "next_wake": None}
    dates = [datetime.fromisoformat(w["expected_by"]) for w in waits.values() if w is not None]
    if not dates:
        return {"status": "done", "next_wake": None}
    return {"status": "idle", "next_wake": min(dates).isoformat()}
```

- **The wake handler** runs the compiled graph once per wake, from its work and the waits the store holds, and
  writes the waits it ends with back in one batch. A Slack event from a person the agent waits on runs it the same
  way (`message`). There is no checkpointer: what the agent remembers between wakes is in the store, so under
  Minutehand it is the run's own memory and a fork from any checkpoint starts from what the agent remembered there,
  with nothing to snapshot or restore. A wake's conversation is not kept past it; the situation each run is given
  carries what the model needs.
- **`next_wake`** is read from the store: the earliest `expected_by` among the waits. The model writes the waits
  through tools that return `Command(update={"waits": ...})`; `remember_wait` reads the current state through
  `InjectedState` to count the asks. `merge_waits` is the reducer, so two tool calls in one step both land.
- **The Slack tool** is a plain `@tool` on `slack_sdk.WebClient`. It calls `slack.com`; Minutehand's `HTTPS_PROXY`
  and CA in the agent's environment take the call to the fake Slack. The store is the only line that names
  Minutehand, and it does nothing in production but pass to the backend configured.

## The model

`ChatOpenAI(base_url=os.environ.get("MODEL_BASE_URL", "http://127.0.0.1:8790/v1"))`: the framework's base-URL
setting, pointed at `../fake_model.py`. The call goes straight to `127.0.0.1` (it is in the `NO_PROXY` Minutehand
hands out), so it is not a model host Minutehand tunnels or records. With a real model, drop `base_url` and set
`OPENAI_API_KEY`; the calls then go to `api.openai.com`, which Minutehand tunnels, or records with
`--record-model-calls`.

## Run both scenarios

From this folder:

```bash
python ../fake_model.py &      # once: the fake model on 127.0.0.1:8790

uv run --group recipes minutehand run scenario_late.yaml --agent agent.yaml -- python agent.py
# exits 0: Rosa answers after the follow-up; the agent tells Owen "... says: The lakeside hall, booked for the 14th."

uv run --group recipes minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
# exits 0: one follow-up two days in, then Owen is told Rosa never answered; no follows_up_when_due finding
```

Outside this checkout: `pip install langgraph langchain-openai slack_sdk minutehand` in the agent's Python, and Minutehand
installed on its own (`uv tool install .` from a checkout); then `minutehand run …` without `uv run`.
