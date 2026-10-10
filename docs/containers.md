# An agent in a container

Minutehand runs on the host and the agent runs in one or more containers. Minutehand does not start the agent;
it hands out the environment the agent needs, and `minutehand run` without `--` wakes the agent that is
already up. This page covers `minutehand run`. A test suite against a stack with `minutehand serve` in it is
in `docs/serve.md`, "One container in a stack".

## What the container needs

| What | Why |
|---|---|
| `HTTPS_PROXY`, `HTTP_PROXY` and their lower-case spellings, pointing at the proxy by a name the container can reach | Every call to a faked or declared host goes through Minutehand's proxy |
| `NODE_USE_ENV_PROXY=1` | Node's built-in `fetch` (and SDKs on it, such as `@slack/web-api` v8) reads the proxy variables only with it, on Node 24 and later |
| `NO_PROXY` and `no_proxy` as Minutehand hands them out | What the agent reaches directly. Anything wider sends calls around the proxy (below) |
| The proxy's CA bundle, mounted, and its path in `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and the rest | The agent's clients must trust the certificates the proxy makes for each host |
| The agent's URLs in the agent file reachable from the host | Minutehand wakes the agent and pushes events to the URLs in `wakes` and `inbound`. Publish the port, and have the agent listen on `0.0.0.0` inside the container, not `127.0.0.1` |
| Its signing secret as `secret: {kind: from_env, env: <variable>}` | A secret generated for each run reaches only a command Minutehand starts; `minutehand env` refuses an agent file that asks for one |

## The commands

Give the proxy and the telemetry receiver fixed ports, listen on every interface, and name the host the way
the container sees it:

```bash
PROXY="--proxy-host 0.0.0.0 --proxy-port 38080 --telemetry-port 38081 --agent-proxy-host host.docker.internal"

minutehand env --agent agent.yaml $PROXY --format compose --service agent > minutehand.override.yaml
docker compose -f compose.yaml -f minutehand.override.yaml up -d

AGENT_SLACK_SIGNING_SECRET=… minutehand run scenario.yaml --agent agent.yaml $PROXY
```

`--format compose --service <name>` (repeat `--service` for each service the agent runs in) prints a Compose
override. Each named service gets the variables in `environment:`, the CA bundle mounted read-only at
`/etc/minutehand/ca-bundle.pem` (`--ca-path` to change it), and, for `host.docker.internal`, an `extra_hosts`
entry so the name resolves on Linux too. The service names join `NO_PROXY`, so the services reach each other
directly. Give `--no-proxy <name>` for anything else in the stack the agent reaches directly, such as a
database emulator.

The CA is made under the state folder (`.minutehand/ca/`, or `--state`) the first time `env` runs. The bundle
stays the same across runs under that state folder, so the stack needs to be started only once.

Without `--format compose`, `minutehand env` prints `export` lines with the bundle's path on the host. For
`docker run`, mount the bundle at that same path (`-v "$BUNDLE:$BUNDLE:ro"`) and pass the variables with `-e`.

Ports already in use on the host make `run` fail with "the proxy could not listen on 0.0.0.0:<port>"; a stack
that publishes 18080 or 28080 is common, so pick ports nothing else holds.

## The NO_PROXY trap

Docker Desktop writes this into the Docker client config, `~/.docker/config.json` (or `$DOCKER_CONFIG/config.json`):

```json
"proxies": {"default": {"noProxy": "*"}}
```

The Docker CLI copies `noProxy` into `NO_PROXY` and `no_proxy` of every container it starts. With `*`, every
client in the container sends every call straight to the real service. Nothing reaches the proxy, so nothing is
refused and nothing is recorded: the run reads as if the agent never called anything ("providers the agent
called: none"), or the real service answers and the agent fails on its answer (Slack's `invalid_auth` for a
token the fake made up).

What wins over the config, measured with Docker 29.1.2 and Compose 2.40.3 on Docker Desktop:

| How the variable is passed | Value in the container |
|---|---|
| Not passed | `*` |
| `docker run --env-file` | `*`: the config overrides the file |
| `docker run -e NO_PROXY=… -e no_proxy=…` | The value passed |
| `docker run -e NO_PROXY=…` alone | `NO_PROXY` as passed, `no_proxy` still `*` |
| Compose `environment:` | The value passed |
| Compose `env_file:` | The value passed |

So:

- With Compose, the override `minutehand env --format compose` writes sets both spellings in `environment:`, and
  the config does not touch them.
- With `docker run`, pass both `NO_PROXY` and `no_proxy` with `-e`, never only in an `--env-file`.
- Or set them in the container's entrypoint, after Docker has set the environment.
- Or remove `noProxy` from the Docker client config.

`minutehand env` and `minutehand doctor` read the Docker client config and warn when a `noProxy` is `*`, or
names a host a provider claims or the agent file declares (`slack.com`, `.slack.com`, `acme.atlassian.net`).
The warning names the file, the value and these fixes. It is a warning, not a refusal: Minutehand cannot see
how the container will be started, and Compose's `environment:` is not affected.

## A client that ignores HTTPS_PROXY: transparent capture

Some clients never read the proxy variables: Node's built-in `fetch` before Node 24 or without
`NODE_USE_ENV_PROXY=1` (Minutehand hands that variable to every agent, so Node 24 and later is covered),
`httplib2` without PySocks, a bare `urllib3.PoolManager`, many Go and JVM clients. Such a client goes straight to the
real service. In a Linux container it can be captured anyway: the container's own packet filter sends its connections
to Minutehand.

```bash
PROXY="--proxy-host 0.0.0.0 --proxy-port 38080 --telemetry-port 38081 --transparent-port 38443 --agent-proxy-host host.docker.internal"

minutehand env --agent agent.yaml $PROXY --format redirect > redirect.sh          # the iptables rules
minutehand env --agent agent.yaml $PROXY --format compose --service agent > minutehand.override.yaml
minutehand run scenario.yaml --agent agent.yaml $PROXY
```

`--transparent-port` opens a second listener beside the proxy. `--format redirect` prints a shell script that, run as
root inside the agent's container, sends every TCP connection to port 80 or 443 of an address outside loopback and
the private ranges (10/8, 172.16/12, 192.168/16, 169.254/16, 100.64/10) to that listener with an `iptables` DNAT
rule. With `--transparent-port`, the Compose override also mounts the script at `/etc/minutehand/redirect.sh` (written
under the state folder) and gives each service the `NET_ADMIN` capability. The container's own entrypoint runs it
before the agent, so it needs `iptables` installed:

```yaml
services:
  agent:
    command: ["sh", "-c", "sh /etc/minutehand/redirect.sh && exec python agent.py"]
```

The listener reads where each connection was going from what the client sends first: the server name in a TLS
ClientHello (taken as port 443) or the `Host` header of a plain HTTP request (its port, else 80). From there it is the
inside of a `CONNECT` to that host: a claimed host is answered by its provider, a model host tunnelled, anything else
refused or captured, all recorded the same way. mitmproxy's own transparent mode cannot be used here: it asks the
kernel where the connection was going (`SO_ORIGINAL_DST`), which knows only when the rewrite happened in the proxy's
own network namespace, and the rule runs in the agent's.

The proxy variables stay set, so a client that honours them still goes through the proxy as before; only the others
are redirected. The CA bundle is still needed: the client checks the certificate Minutehand mints for the host.

What it does not cover:

- Traffic to the private ranges, where the rest of a stack lives (other containers, the host's own services). A SaaS
  host that resolves to a private address in your network is not redirected.
- Ports other than 80 and 443, IPv6 (add the same rules with `ip6tables` on an IPv6 network), and UDP (HTTP/3: clients
  fall back to TCP when UDP 443 goes unanswered).
- A TLS client that sends no server name (one that connects to an IP address): the connection is closed.
- An agent on the host rather than in a container. There, the run tells you instead (below).

An agent in the `Contained` sandbox reaches the network through the outer container's link to the sandbox
(`vp-runsc` in the spike, "Evidence" in `docs/design.md`); the same DNAT rules in that container's `nat PREROUTING`
chain, on that interface, would send its connections to the listener. Not tried.

Tested with a real container (`tests/providers/slack/test_slack_redirected_container.py`, marked `docker`, CI job
`transparent`): `curl --noproxy '*'` with `NO_PROXY=*` set, in `alpine` with `iptables`, is answered by the Slack fake
once the script has run, and times out before it.

### When a run went around the proxy

Without transparent capture, a client that ignores the proxy reaches the real service and Minutehand sees nothing. The
`around_proxy` check says so when it can:

- **States it for review** (and fails the run when the agent file or the scenario names `around_proxy` in
  `fail_on_integrity`) when the agent's own OpenTelemetry names an HTTP call to a host a provider claims (`url.full`,
  `http.url`, `server.address` or `net.peer.name` on a span with an HTTP method) that the proxy never recorded. A span
  is matched to the proxy's record by the `traceparent` the call carried; spans left over are counted against the
  host's records that carried none.
- **Asks you to review** when the agent was woken and the proxy saw no call to any provider the scenario and agent file
  name. That is what a client that ignores the proxy looks like, and also what an agent that did nothing looks like.

Both name the fixes: `NODE_USE_ENV_PROXY=1` on Node 24 or later, PySocks for `httplib2`, `trust_env=True` for
`aiohttp`, a base URL (`base_urls`), or transparent capture.

Neither catches an agent that exports no telemetry and still calls some provider through the proxy while another
client bypasses it. Two other ways to find out were looked at and not built: resolving the claimed hosts to a local
address for the agent alone (a hosts file through `HOSTALIASES`, a resolver through `LD_PRELOAD`) is ignored by static
Go binaries and needs root on macOS; and polling the agent's sockets misses a call that finishes between two polls.

## Checked by hand

The follow-up example (`tests/agents/follow_up`) was run this way on 2026-10-07: the agent in a `python:3.13-slim`
container with `slack_sdk`, listening on `0.0.0.0:8700` and published on the same port, its agent file's secret
changed to `from_env`, started with Compose and the override above. The run passed exactly as it does with the
agent on the host. Started again with `docker run --env-file` holding the same variables, the container had
`NO_PROXY=*`, the agent's first Slack call reached the real `slack.com` and was answered `invalid_auth`, and the
run failed with "providers the agent called: none". With `-e NO_PROXY=… -e no_proxy=…` added, it passed. None
of this is in the test suite; the override itself is tested as text (`tests/e2e/test_cli.py`).
