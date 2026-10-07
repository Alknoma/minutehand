# minutehand-agent

What an agent imports to be played by [Minutehand](https://github.com/Alknoma/minutehand): what it remembers across
wakes, and when it next wants to be woken. The standard library only; install it in the agent's own environment.

```bash
pip install minutehand-agent
```

```python
from minutehand_agent import store, wake

store.configure(store.SqliteBackend("agent.db"))  # production: your database, through a three-method adapter

store.put("asks/sam", {"status": "asked", "expected_by": expected_by.isoformat()})
for key, ask in store.query("asks/", where={"status": "asked"}):
    ...
wake.at(expected_by)  # when it next wants to wake
```

Both are inert in production: unless `MINUTEHAND_ON` is set, `store` passes every call to the backend the agent
configured and `wake` does nothing. Under `minutehand run`, which sets `MINUTEHAND_ON` and `MINUTEHAND_AGENT_URL`,
both go to the run instead: every write is recorded in the run and every read answered from it, so each run and each
fork has a memory of its own and the agent's own database is never opened.

Minutehand itself (`pip install minutehand`) is installed apart, the way a linter is; the agent never imports it.
The contract is [docs/agent-contract.md](https://github.com/Alknoma/minutehand/blob/integration-main/docs/agent-contract.md).
