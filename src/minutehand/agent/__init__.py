"""What an agent imports: the whole of its contact with Minutehand, and only for what it remembers.

    from minutehand.agent import store

    store.put("asks/sam", {"status": "asked", "expected_by": "2026-09-03T09:00:00+00:00"})

`store` is what the agent remembers. It is inert in production: unless MINUTEHAND_ON is set, every call passes to
the agent's own database through the adapter the agent configured (`store.configure`). Under Minutehand
(`minutehand run` sets MINUTEHAND_ON and MINUTEHAND_AGENT_URL) every write is recorded in the run's log and every
read answered from the run's state, so each run and each fork has a memory of its own and the agent's own database
is never opened. Nothing else is asked of the agent, and nothing else of Minutehand's runs in it: what it comes back
to by itself it books with a scheduler it calls, which the run keeps in its log. A date in `store` is the agent's
data, and wakes nothing.

This package imports nothing but the standard library and nothing else of Minutehand's, so an agent pays nothing for
it in production; `tests/agent/test_agent_package.py` holds it to that.
"""
