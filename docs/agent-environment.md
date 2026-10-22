# The agent's environment

The agent under test gets its own environment (a model provider's API key, a database password, a feature flag)
the way the agent file says, and from nowhere else. Before this, the only way an agent started by `minutehand run
-- <command>` got a secret was for whoever started `minutehand` to hold it in that process's environment, which the
command inherited whole: an accident of process inheritance, written down nowhere, and pushing adopters into passing
keys around with `-e`, `export` or a shell. Now it is a declared part of the agent, kept with the run and honoured
by every place Minutehand starts it.

```yaml
# agent.yaml
name: proactive_agent
sandbox: {deadlines: [...], advance: [...]}
environment:
  sources:
    - {kind: inherit}                   # the environment Minutehand was started in
    - {kind: files, paths: [agent.env]} # then this file, read by Minutehand, relative to the agent file
```

```bash
# agent.env (never committed; the agent's own)
OPENAI_API_KEY=sk-...
AGENT_SLACK_TOKEN=xoxb-...
AGENT_PORT=8740
```

## The three sources

| `kind` | What the agent gets | Notes |
|---|---|---|
| `inherit` | The environment Minutehand was started in, as today | **The default**, used when `environment:` is absent: no adopter has to pass anything |
| `files` | The variables of one or more dotenv files, in order | Paths are relative to the agent file. Minutehand reads the files itself when it starts the agent's process, the way Compose reads `env_file`, so nobody handles a value. A file that does not exist, or cannot be read, refuses the run at the start, naming the file |
| `only` | An allowlist of names taken from the inherited environment | For an agent that should see nothing else of the machine's. A name not set is left out |

The sources combine in the declared order, a later source overriding an earlier one. Minutehand's own variables
(`session.agent_environment`: the proxy and its CA, `NO_PROXY`, the telemetry receiver, the signing secrets it
generates, `MINUTEHAND_ON` and `MINUTEHAND_AGENT_URL`, `MINUTEHAND_STARTS_AT_NS`, the sandbox's ports, the agent's
own databases and base URLs, `MINUTEHAND_AGENT_ENVIRONMENT`) always apply last, and no source can override them.

A dotenv file is read the way Compose reads one: one `NAME=value` a line; an `export ` prefix allowed; blank lines and
lines starting with `#` skipped; an unquoted value trimmed and ending at ` #`; a single-quoted value kept as it is; a
double-quoted value with `\n`, `\t`, `\"` and `\\` read as escapes; a later line of the same name winning. A line
that is not `NAME=value`, a quote never closed, and `${...}` in a value (interpolation is not performed: write the
value out) refuse the run, naming the file and the line.

## What never reaches the agent

- **Minutehand's own credentials**, under every source: the key of the model that writes what people say
  (`MINUTEHAND_MODEL_API_KEY`) and the OTLP headers the agent's telemetry is forwarded with
  (`OTEL_EXPORTER_OTLP_HEADERS` and each signal's own). They are the runner's, not the environment's. An `only`
  source that asks for one by name is refused; a file or the inherited environment that holds one has it withheld,
  and the record says so.
- **A value, anywhere Minutehand writes.** No value is on a command line (`ps` shows argv), in a log line, in the
  run's SQLite record, in telemetry or in the viewer. What the run keeps (`environment.json`, the read model's
  `environment` view, `minutehand query`) is the declaration: each source in order, the file it is when it is one,
  and the NAMES it supplied; then the names of Minutehand's own variables and of the credentials it withheld.
- **A value the agent sends.** Every value a `files` or `only` source supplied joins the proxy's redaction
  (`adapters/proxy/redact.py`, `Secrets`): wherever it appears in a call's path, headers or body, a captured call, a
  recorded model call, a gRPC message or a WebSocket frame, the record holds `[redacted]` in its place, like a
  credential under a credential's name. A value shorter than eight characters (a port, `true`, a log level) is not
  redacted by value: it is not a secret anyone keeps, and taking it out wherever it occurs would take ordinary
  numbers and words out of the record. The inherited environment is the whole machine's and does not join: a user
  name or a locale is not a secret. What the agent's own telemetry carries is the agent's.

## How the values travel

Minutehand resolves the declaration when it starts the agent's command (`application/environment.py`), starts the
command with exactly the resolved environment (nothing else of Minutehand's process reaches it), and writes the same
environment to a file of its own, mode 0600, in the system's temporary directory, named to the command in
`MINUTEHAND_AGENT_ENVIRONMENT`: one `export NAME=value` a line, each value quoted for a POSIX shell, removed when the
command is stopped. The file is how the environment crosses into the sandbox explicitly ("The sandbox", below).

## Forks

A fork starts its agent with the same declaration, the one kept in the run's `agent.json`, with every file path
made absolute from the agent file's folder when the file was read. Its sources are re-read at the fork's start:
nothing comes from the parent's record, and a source that has gone missing refuses the fork, naming it. A value
that changed between the run and the fork is the fork's; one the agent sends in a call is redacted in both records,
so the fork's playback matches the parent's record where the agent does what it did before.

## The sandbox

The process inside the sandbox gets the environment explicitly, from the file, not through whatever `runsc`
happens to inherit, and never on a command line. The example's launcher (`examples/proactive_agent/run_in_sandbox.sh`)
starts `runsc` with nothing but `PATH` and the file's name, and the command inside sources it:

```sh
exec env -i PATH="$PATH" MINUTEHAND_AGENT_ENVIRONMENT="$MINUTEHAND_AGENT_ENVIRONMENT" \
  runsc ... do ... -- /bin/sh -c '. "$MINUTEHAND_AGENT_ENVIRONMENT" && exec python3 agent.py'
```

`runsc do` runs the command over the host's root filesystem, so the file is at the same path inside. The launcher
itself runs with the agent's environment, so a declaration of `files` or `only` alone leaves it no `PATH` to find
`runsc` with: a sandbox launcher declares `inherit` before its files, or lists `PATH` under `only`.
`sandbox/README.md` has the rest.

## Standing mode and the other commands

| Command | What applies |
|---|---|
| `minutehand run -- <command>`, `fork`, `replay`, `run-all`, `replay-all` | The declaration, resolved at each start |
| `minutehand run` with no command (an agent already running, as in a container) | Nothing: Minutehand hands the agent no environment; the team started it |
| `minutehand env --format shell` | `inherit`: nothing to add. `files` or `only`: refused by name, since `env` prints no value and the agent is not started by Minutehand; source the file yourself beside the printed variables, or start the agent with `minutehand run -- <command>` |
| `minutehand env --format compose` | `files` becomes each service's `env_file:` (the paths, no values), under Minutehand's variables in `environment:`, which Compose applies over them, as a run does. `only` is refused by name: a container inherits nothing of this machine's through a printed file |
| `minutehand serve` | No agent file is read and no agent started: no declaration applies. A suite that starts the agent itself gives it the variables `GET /v1/environment` prints (`docs/serve.md`) |
| `minutehand doctor --agent agent.yaml -- <command>` | Reports each declared source as readable or not, with the names it supplies and no value, and runs its probe with the resolved environment |
| `minutehand validate agent.yaml` | Reads the declaration as a file; a source is read only when the agent is started |

## Worked example: a model key in the agent's own `.env`

The agent reads `OPENAI_API_KEY` and a Slack token. Its team keeps them in `agent.env` beside the agent file, out of
version control, and runs Minutehand from a shell that holds Minutehand's own `MINUTEHAND_MODEL_API_KEY` for the
people's words:

```yaml
# agent.yaml
environment:
  sources:
    - {kind: inherit}
    - {kind: files, paths: [agent.env]}
```

```
$ minutehand run worlds/quiet.yaml --agent agent.yaml -- ./run_in_sandbox.sh
$ minutehand query <run> "SELECT position, kind, source, names FROM environment"
position  kind        source                      names
1         inherit     NULL                        ["HOME","LANG","PATH", ...]
2         files       /…/proactive_agent/agent.env ["OPENAI_API_KEY","AGENT_SLACK_TOKEN","AGENT_PORT"]
3         minutehand  NULL                        ["HTTPS_PROXY","NO_PROXY","SSL_CERT_FILE", ...]
4          withheld   NULL                        ["MINUTEHAND_MODEL_API_KEY"]
```

The agent's process held the key; the record holds its name; the call the agent made with it to its model API holds
`[redacted]` where the key was, in the span's request body as in its `Authorization` header (which the record never
kept). `minutehand doctor --agent agent.yaml -- ./run_in_sandbox.sh` says, before any run, whether `agent.env` can
be read and which names it supplies.
