"""An agent under test, run once per wake by the `Command` driver: on its first wake it files an order with the
orders service through the proxy its environment names, then reports idle. The standard library only."""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request

wake = json.loads(sys.stdin.read())
if wake["reason"] == "start":
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": os.environ["HTTPS_PROXY"]}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])),
    )
    request = urllib.request.Request(
        "https://api.orders.example/v1/orders",
        data=json.dumps({"sku": "laptop"}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request) as answered:
        answered.read()
print(json.dumps({"status": "idle", "next_wake": None}))
