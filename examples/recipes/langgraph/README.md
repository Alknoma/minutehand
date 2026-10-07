# LangGraph

`agent.py` is a LangGraph graph of the usual shape (a chat model with tools bound, a `ToolNode`, `tools_condition`
between them, an `InMemorySaver` checkpointer) behind three endpoints Minutehand calls. `../README.md` describes the
agent and the contract all five recipes share.

## The lines that connect it

```python
class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    waits: Annotated[dict[str, Wait | None], merge_waits]  # person -> {expected_by, asks}; None removes one
    goal: str


def wake(request):  # POST /wake
    now = datetime.fromisoformat(request["now"])
    if request["reason"] == "start":
        first = situation(now, request["goal"], "Nobody has been asked yet.")
        graph.invoke({"messages": [HumanMessage(first)], "goal": request["goal"], "waits": {}}, THREAD)
        return
    for email, wait in state()["waits"].items():  # reason "due": whatever has passed its date
        if datetime.fromisoformat(wait["expected_by"]) <= now:
            overdue = (
                f"No answer yet from {email}, expected by {wait['expected_by']}. Follow-ups sent: {wait['asks'] - 1}."
            )
            graph.invoke({"messages": [HumanMessage(situation(now, state()["goal"], overdue))]}, THREAD)


def report():  # GET /report, after every wake
    if not graph.get_state(THREAD).values:
        return {"status": "idle", "next_wake": None}
    dates = [datetime.fromisoformat(w["expected_by"]) for w in state()["waits"].values() if w is not None]
    if not dates:
        return {"status": "done", "next_wake": None}
    return {"status": "idle", "next_wake": min(dates).isoformat()}
```

- **The wake handler** runs the compiled graph on one thread (`THREAD`), so the checkpointer carries the
  conversation and `waits` from one wake to the next. A Slack event from a person the agent waits on runs it the
  same way (`message`).
- **`next_wake`** is read from the graph's own state: `graph.get_state(THREAD).values["waits"]`. The model writes
  it through tools that return `Command(update={"waits": ...})`; `remember_wait` reads the current state through
  `InjectedState` to count the asks. `merge_waits` is the reducer, so two tool calls in one step both land.
- **The Slack tool** is a plain `@tool` on `slack_sdk.WebClient`. It calls `slack.com`; Minutehand's `HTTPS_PROXY`
  and CA in the agent's environment take the call to the fake Slack. Nothing in the agent names Minutehand.

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
# exits 0: one follow-up two days in, then Owen is told Rosa never answered; no no_follow_up finding
```

Outside this checkout: `pip install langgraph langchain-openai slack_sdk` in the agent's Python, and Minutehand
installed on its own (`uv tool install .` from a checkout); then `minutehand run …` without `uv run`.
