"""The seal's probe: inside a sealed sandbox, ignore the proxy and try every way out. Each line says what happened;
`sandbox/README.md` says what each should get. It exits when done, so the run that started it ends as the agent
exiting, which is what a run of this probe is for."""

import os
import socket
import ssl
import sys
import urllib.error
import urllib.request

for v in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
    os.environ.pop(v, None)
os.environ["NO_PROXY"] = "*"


def say(label, outcome):
    print(f"[probe] {label}: {outcome}", flush=True)


def fetch(url, verify=True):
    ctx = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"]) if verify else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url, timeout=8, context=ctx) as r:
            return f"HTTP {r.status} {r.read(80)!r}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.read(120)!r}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


print("[probe] started", flush=True)


def resolve(name):
    try:
        return socket.gethostbyname(name)
    except Exception as e:
        return f"{type(e).__name__}: {e}"


say("resolve slack.com", resolve("slack.com"))
say("resolve example.com", resolve("example.com"))
say("direct https://slack.com/api/api.test (no proxy)", fetch("https://slack.com/api/api.test"))
say("direct https://example.com (undeclared host)", fetch("https://example.com/"))
say("direct http://1.1.1.1/ (by address)", fetch("http://1.1.1.1/"))
say(
    "direct https://api.anthropic.com/v1/models (a model host: tunnelled out)",
    fetch("https://api.anthropic.com/v1/models"),
)
try:
    s = socket.create_connection(("8.8.8.8", 9999), timeout=4)
    say("tcp 8.8.8.8:9999", "connected (then the proxy reads the first bytes)")
    s.close()
except Exception as e:
    say("tcp 8.8.8.8:9999", f"{type(e).__name__}: {e}")
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.settimeout(3)
u.sendto(b"hello", ("8.8.8.8", 4444))
try:
    say("udp 8.8.8.8:4444", f"reply {u.recvfrom(64)!r}")
except Exception as e:
    say("udp 8.8.8.8:4444", f"no reply ({type(e).__name__})")
print("[probe] done", flush=True)
sys.stdout.flush()
