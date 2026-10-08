"""How the agent reaches Minutehand while a run plays it: JSON over HTTP to the run's receiver.

    MINUTEHAND_ON          set (to anything but empty) by Minutehand for the agent it plays: the store and the wake
                           marker talk to the run. Unset in production, where both are inert
    MINUTEHAND_AGENT_URL   where they talk to: the receiver's `/minutehand/agent`, on the host and port Minutehand
                           hands the agent beside its proxy (the host is in the agent's NO_PROXY)

A call is retried when it never reached Minutehand (the connection refused or reset) and when Minutehand answers 502,
503 or 504, a few times over a few seconds; then it raises `MinutehandUnreachable`. It never falls back to the
agent's own database: a run whose agent quietly wrote to production would be worse than a run that stops. A
refusal (any other 4xx or 5xx) raises `MinutehandRefused` with what Minutehand said.

The standard library only: `urllib.request` with the process's own proxy settings, so `NO_PROXY` decides as it does
for every other call the agent makes, and the CA bundle Minutehand hands out for an https URL.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

ON = "MINUTEHAND_ON"
URL = "MINUTEHAND_AGENT_URL"

ATTEMPTS = 6
"""How many times a call that never reached Minutehand is tried: about three seconds in all."""
FIRST_WAIT = 0.1
TIMEOUT = 30.0
RETRIED_STATUSES = frozenset({502, 503, 504})


class MinutehandUnreachable(RuntimeError):
    """MINUTEHAND_ON is set and the run could not be reached; nothing was written anywhere."""


class MinutehandRefused(RuntimeError):
    """The run answered, and refused the call: what it said is the message."""


def on() -> bool:
    """Whether this process is played by Minutehand. Read at every call, so a test can switch it."""
    return bool(os.environ.get(ON))


def url() -> str:
    found = os.environ.get(URL)
    if not found:
        raise MinutehandUnreachable(
            f"{ON} is set and {URL} is not, so there is no run to talk to; Minutehand sets both for the agent it "
            f"plays. Unset {ON} to use the agent's own database"
        )
    return found.rstrip("/")


def call(path: str, body: object) -> object:
    """POST `body` as JSON to the run at `path` and answer the JSON it sends back (None for an empty answer)."""
    target = f"{url()}/{path}"
    data = json.dumps(body, ensure_ascii=False).encode()
    wait = FIRST_WAIT
    last = ""
    for attempt in range(ATTEMPTS):
        request = urllib.request.Request(target, data=data, method="POST", headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
                raw = answer.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            said = e.read().decode(errors="replace")
            if e.code not in RETRIED_STATUSES:
                raise MinutehandRefused(f"Minutehand refused {path}: {e.code} {said}") from None
            last = f"{e.code} {said}"
        except urllib.error.URLError as e:
            if not isinstance(e.reason, ConnectionError):
                raise MinutehandUnreachable(_unreachable(target, str(e.reason), attempt + 1)) from e
            last = str(e.reason)
        except ConnectionError as e:
            last = str(e)
        if attempt + 1 < ATTEMPTS:
            time.sleep(wait)
            wait *= 2
    raise MinutehandUnreachable(_unreachable(target, last, ATTEMPTS))


def _unreachable(target: str, why: str, attempts: int) -> str:
    return (
        f"{ON} is set and Minutehand did not answer at {target} ({attempts} attempt(s); the last: {why}). Nothing was "
        "written: the store never falls back to the agent's own database while it is set. Play the agent with "
        f"`minutehand run`, or unset {ON}"
    )
