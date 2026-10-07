"""A ten-line stand-in for an agent: each wake it files a customer with the payments API and asks for its list,
through whatever proxy its environment names. It knows nothing of Minutehand or of the emulator answering it."""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

wake = json.load(sys.stdin)
now = datetime.fromisoformat(wake["now"])
time.sleep(float(os.environ.get("EXAMPLE_PAUSE", "0")))  # a slow agent: time to kill the emulator mid-run
for method, path, body in (("POST", "/v1/customers", b"email=rosa%40example.com"), ("GET", "/v1/customers", None)):
    asked = urllib.request.Request(
        f"https://api.stripe.com{path}", data=body, method=method, headers={"authorization": "Bearer sk_test_123"}
    )
    try:
        urllib.request.urlopen(asked, timeout=30).read()
    except urllib.error.HTTPError as refused:
        print(f"{method} {path}: {refused.code} {refused.read()[:200]!r}", file=sys.stderr)
done = wake["reason"] != "start" and now.hour >= 12
print(json.dumps({"status": "done" if done else "idle", "next_wake": (now + timedelta(hours=1)).isoformat()}))
