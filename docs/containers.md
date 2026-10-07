# An agent in a container

Minutehand runs on the host and the agent runs in one or more containers. Minutehand does not start the agent;
it hands out the environment the agent needs, and `minutehand run` without `--` wakes the agent that is
already up. This page covers `minutehand run`. A test suite against a stack with `minutehand serve` in it is
in `docs/serve.md`, "One container in a stack".

## What the container needs

| What | Why |
|---|---|
| `HTTPS_PROXY`, `HTTP_PROXY` and their lower-case spellings, pointing at the proxy by a name the container can reach | Every call to a faked or declared host goes through Minutehand's proxy |
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

## Checked by hand

The follow-up example (`examples/follow_up`) was run this way on 2026-10-07: the agent in a `python:3.13-slim`
container with `slack_sdk`, listening on `0.0.0.0:8700` and published on the same port, its agent file's secret
changed to `from_env`, started with Compose and the override above. The run passed exactly as it does with the
agent on the host. Started again with `docker run --env-file` holding the same variables, the container had
`NO_PROXY=*`, the agent's first Slack call reached the real `slack.com` and was answered `invalid_auth`, and the
run failed with "providers the agent called: none". With `-e NO_PROXY=… -e no_proxy=…` added, it passed. None
of this is in the test suite; the override itself is tested as text (`tests/e2e/test_cli.py`).
