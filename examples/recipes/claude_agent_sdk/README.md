# Claude Agent SDK

`agent.py` runs one Claude Agent SDK `query()` per wake, with three tools served from this process by
`create_sdk_mcp_server`, behind three endpoints Minutehand calls. `../README.md` describes the agent and the
contract all five recipes share.

The SDK is the real one, `claude-agent-sdk`, not the Anthropic SDK's tool runner: it drives the Claude Code command
line bundled in its wheel, which runs offline against `../fake_model.py` given `ANTHROPIC_BASE_URL`. Nothing else
needs installing.

## The lines that connect it

```python
@tool("remember_wait", "Remember that `email` owes an answer, expected by `expected_by` (ISO 8601). ...",
      {"email": str, "expected_by": str})
async def remember_wait(args):                               # writes the process's Memory
    email, waits = args["email"], memory.waits
    waits[email] = Wait(datetime.fromisoformat(args["expected_by"]), waits[email].asks + 1 if email in waits else 1)
    return said(f"waiting on {email} until {args['expected_by']}")

OPTIONS = ClaudeAgentOptions(
    system_prompt=SYSTEM, tools=[],                          # none of Claude Code's own tools
    mcp_servers={"follow_up": create_sdk_mcp_server("follow_up", tools=[send_slack_message, remember_wait, close_wait])},
    allowed_tools=["mcp__follow_up__send_slack_message", "mcp__follow_up__remember_wait", "mcp__follow_up__close_wait"],
    setting_sources=[], cwd=WORKING_DIR, max_turns=5,
    env={"ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:8790"), ...},
)

def wake(request):                                           # POST /wake
    now = datetime.fromisoformat(request["now"])
    if request["reason"] == "start":
        memory.goal = request["goal"]
        run(now, "Nobody has been asked yet.")               # asyncio.run(ask_claude(situation)): one query()
        return
    for email, wait in list(memory.waits.items()):           # reason "due": whatever has passed its date
        if wait.expected_by <= now:
            run(now, f"No answer yet from {email}, expected by {wait.expected_by.isoformat()}. Follow-ups sent: {wait.asks - 1}.")

def report():                                                # GET /report, after every wake
    if memory.goal is None:
        return {"status": "idle", "next_wake": None}
    if not memory.waits:
        return {"status": "done", "next_wake": None}
    return {"status": "idle", "next_wake": min(w.expected_by for w in memory.waits.values()).isoformat()}
```

- **The wake handler** makes one `query()` per wake, with the situation as its prompt. A Slack event from a person
  the agent waits on is one more (`message`). Each `query()` starts the bundled command line, which takes about half
  a second.
- **`next_wake`** is read from `Memory`, which the in-process tools write. The command line keeps its own session,
  but the agent's state is in this process, where the report can read it without asking the model.
- **The Slack tool** runs in this process, not in the command line, so its `slack_sdk.WebClient` call to
  `slack.com` goes through Minutehand's `HTTPS_PROXY` and CA like any other.
- **The command line's own files** (sessions, settings) go to a directory of the agent's own (`CLAUDE_CONFIG_DIR`,
  `cwd`), and `setting_sources=[]` keeps the user's and project's settings out, so a run depends on nothing in
  `~/.claude`.

## The model

`ANTHROPIC_BASE_URL`, passed to the command line through `ClaudeAgentOptions.env`, points it at
`../fake_model.py`, which answers `POST /v1/messages` as a stream of server-sent events, as the command line asks
for. The fake calls the tools by the names the command line offers them under (`mcp__follow_up__…`). The call goes
straight to `127.0.0.1`, which is in the `NO_PROXY` Minutehand hands out. With a real model, drop
`ANTHROPIC_BASE_URL` and set `ANTHROPIC_API_KEY`; the calls go to `api.anthropic.com`, which Minutehand tunnels, or
records with `--record-model-calls`.

## Run both scenarios

From this folder:

```bash
python ../fake_model.py &      # once: the fake model on 127.0.0.1:8790

uv run --group recipes minutehand run scenario_late.yaml --agent agent.yaml -- python agent.py
# exits 0: Rosa answers after the follow-up; the agent tells Owen "... says: The lakeside hall, booked for the 14th."

uv run --group recipes minutehand run scenario_silent.yaml --agent agent.yaml -- python agent.py
# exits 0: one follow-up two days in, then Owen is told Rosa never answered; no follows_up_when_due finding
```

Outside this checkout: `pip install claude-agent-sdk slack_sdk` in the agent's Python, and Minutehand installed on
its own (`uv tool install .` from a checkout); then `minutehand run …` without `uv run`.
