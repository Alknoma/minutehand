# Pydantic AI

`agent.py` is a Pydantic AI `Agent` with typed dependencies (`deps_type=Memory`), a typed output
(`output_type=str`), and three tools, behind three endpoints Minutehand calls. `../README.md` describes the agent
and the contract all five recipes share.

## The lines that connect it

```python
@dataclass
class Memory:  # the deps, alive as long as the process
    goal: str | None = None
    waits: dict[str, Wait] = field(default_factory=dict)  # person -> Wait(expected_by, asks)


agent = Agent(model, deps_type=Memory, output_type=str, instructions=SYSTEM)


@agent.tool
def remember_wait(ctx: RunContext[Memory], email: str, expected_by: datetime) -> str:
    waits = ctx.deps.waits  # expected_by arrives validated as a datetime
    waits[email] = Wait(expected_by, waits[email].asks + 1 if email in waits else 1)
    return f"waiting on {email} until {expected_by.isoformat()}"


def wake(request):  # POST /wake
    now = datetime.fromisoformat(request["now"])
    if request["reason"] == "start":
        memory.goal = request["goal"]
        run(now, "Nobody has been asked yet.")  # agent.run_sync(situation, deps=memory)
        return
    for email, wait in list(memory.waits.items()):  # reason "due": whatever has passed its date
        if wait.expected_by <= now:
            run(
                now,
                f"No answer yet from {email}, expected by {wait.expected_by.isoformat()}. Follow-ups sent: {wait.asks - 1}.",
            )


def report():  # GET /report, after every wake
    if memory.goal is None:
        return {"status": "idle", "next_wake": None}
    if not memory.waits:
        return {"status": "done", "next_wake": None}
    return {"status": "idle", "next_wake": min(w.expected_by for w in memory.waits.values()).isoformat()}
```

- **The wake handler** makes one `agent.run_sync` per wake, with the situation as its prompt and the same `Memory`
  as its `deps`. A Slack event from a person the agent waits on is one more run (`message`).
- **`next_wake`** is read from the deps the tools write through `RunContext[Memory]`. Because the tool's
  `expected_by` is typed `datetime`, a date the model gets wrong is refused by validation and sent back to the
  model to retry, before it can reach the report.
- **The Slack tool** is an `@agent.tool_plain` on `slack_sdk.WebClient`. It calls `slack.com`; Minutehand's
  `HTTPS_PROXY` and CA in the agent's environment take the call to the fake Slack.

## The model

`OpenAIChatModel("gpt-4.1-mini", provider=OpenAIProvider(base_url=os.environ.get("MODEL_BASE_URL", ...)))`: the
provider's base URL, pointed at `../fake_model.py` over chat completions. The call goes straight to `127.0.0.1`,
which is in the `NO_PROXY` Minutehand hands out.

Pydantic AI has its own stand-ins, `TestModel` and `FunctionModel`, which answer inside the process. They are not
used here, on purpose: a run under Minutehand should be the agent you ship, and swapping the model object needs a
code path that production never takes, while a base URL leaves the agent's model, its HTTP client and the wire
format as they are. `FunctionModel` is the better tool for a unit test of one tool; this is a run of the whole agent.

With a real model, drop the provider (`Agent("openai:gpt-4.1-mini", ...)`) and set `OPENAI_API_KEY`; the calls go to
`api.openai.com`, which Minutehand tunnels, or records with `--record-model-calls`.

## Run both scenarios

From this folder:

```bash
python ../fake_model.py &      # once: the fake model on 127.0.0.1:8790

uv run --group recipes minutehand run scenario_late.yaml --agent agent.yaml -- python agent.py
# exits 0: Rosa answers after the follow-up; the agent tells Owen "... says: The lakeside hall, booked for the 14th."

uv run --group recipes minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
# exits 0: one follow-up two days in, then Owen is told Rosa never answered; no follows_up_when_due finding
```

Outside this checkout: `pip install "pydantic-ai-slim[openai]" slack_sdk` in the agent's Python, and Minutehand
installed on its own (`uv tool install .` from a checkout); then `minutehand run …` without `uv run`.
