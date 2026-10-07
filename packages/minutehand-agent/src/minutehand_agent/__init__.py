"""What an agent imports: the whole of its contact with Minutehand.

    from minutehand_agent import store, wake

    store.put("asks/sam", {"status": "asked", "expected_by": "2026-09-03T09:00:00+00:00"})
    wake.at(expected_by)

`store` is what the agent remembers; `wake` is when it next wants to be woken. Both are inert in production: unless
MINUTEHAND_ON is set, `store` passes every call to the agent's own database through the adapter the agent configured
(`store.configure`), and `wake` does nothing. Under Minutehand (`minutehand run` sets MINUTEHAND_ON and
MINUTEHAND_AGENT_URL) both go to the run instead: every write is recorded in the run's log and every read answered
from the run's state, so each run and each fork has a memory of its own and the agent's own database is never
opened.

It is the distribution `minutehand-agent`, installed in the agent's own environment (`pip install minutehand-agent`),
and imports nothing but the standard library, so an agent pays nothing for it in production and never installs
`minutehand` itself; `tests/agent/test_store.py` holds it to that.
"""
