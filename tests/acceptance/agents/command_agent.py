"""A stand-in agent woken by command: reads the wake on stdin, makes its calls, prints its report.

AGENT_CALLS   the calls, "METHOD URL" separated by commas; a POST sends the body "x"
AGENT_SEEN    a file each call's status and body are appended to, one JSON line each
AGENT_REPORT  the report it prints (default {"status": "done", "next_wake": null})
"""

import json
import os
import sys
import urllib.error
import urllib.request

wake = json.load(sys.stdin)
for call in os.environ["AGENT_CALLS"].split(","):
    method, url = call.split()
    asked = urllib.request.Request(url, data=b"x" if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(asked, timeout=30) as answered:
            status, body = answered.status, answered.read()
    except urllib.error.HTTPError as refused:
        status, body = refused.code, refused.read()
    with open(os.environ["AGENT_SEEN"], "a") as seen:
        seen.write(json.dumps({"call": call, "status": status, "body": body.decode(errors="replace")}) + "\n")
print(os.environ.get("AGENT_REPORT", json.dumps({"status": "done", "next_wake": None})))
