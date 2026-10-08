"""An agent under test, run once per wake by the `Command` driver: on its first wake it invites Dov to a planning
call on its own calendar through Calendar's `events.insert`, then reports idle. The standard library only."""

from __future__ import annotations

import json
import os
import sys
import urllib.request

wake = json.loads(sys.stdin.read())
if wake["reason"] == "start":
    body = {
        "summary": "Planning call",
        "description": "Can you make it?",
        "attendees": [{"email": "dov@example.com"}],
        "start": {"dateTime": "2026-08-26T10:00:00Z"},
        "end": {"dateTime": "2026-08-26T10:30:00Z"},
    }
    request = urllib.request.Request(
        os.environ["MH_BASE"] + "/google_workspace/calendar/v3/calendars/primary/events",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Host": "www.googleapis.com",
            "Authorization": "Bearer ya29.agent",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request) as answered:
        answered.read()
print(json.dumps({"status": "idle", "next_wake": None}))
