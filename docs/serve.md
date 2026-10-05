# The standing mode: `minutehand serve`

`minutehand run` owns the loop, the clock and the people of one scenario. A test suite that starts a stack, seeds
a fake, calls its own services and then inspects the fake needs the opposite: a server that stays up, holds a
world per test, and does nothing until told. That is `minutehand serve`.

```
minutehand serve [--state DIR] [--host 127.0.0.1] [--proxy-port 8080] [--control-port 8081]
                 [--telemetry-port 4318] [--no-receive-telemetry] [--agent-host NAME] [--no-proxy HOST]... [--keep 100]
```

One process: the proxy, the OTLP receiver and the control API (HTTP and JSON, under `/v1`). Every installed
provider is available and built on its first call. There is no scenario, no run loop and no agent wake.

## When to use it, and when `run`

| | `minutehand run` | `minutehand serve` |
|---|---|---|
| Who drives | Minutehand: wakes the agent, plays the people, moves the clock | The test: opens a world, calls its services, speaks for people, moves the clock |
| Worlds | One per run, played to its end | Many at once, one per test, open as long as the test needs |
| Time | Jumps to the next thing due | Stands still until `POST /v1/worlds/{id}/clock` |
| People | Scripted or model-written replies, delivered when due | Only the test, unless the world is opened with `scripted_people: true` |
| Result | A verdict, findings and a scorecard at the end | Whatever the test asserts; the same checks on demand, and once more when the world is closed |
| Use for | Measuring an agent across simulated days | A service-level suite that used emulators |

## Which world a call belongs to

Tests run in parallel against one stack, so the proxy decides per call. In order:

1. **The host**, when a world claims it (`claims.hosts`: a self-hosted tracker's `acme.youtrack.cloud`).
2. **A world key its URL names**, when a world claims it (`claims.keys`). Each provider's manifest declares
   where a request names its world without a credential (`Manifest.world_keys`, a host with `{key}` for one
   label or a path prefix with `{key}` for one segment); the router reads those and knows no vendor. Microsoft:
   the tenant id or domain in `/{tenant}/oauth2/v2.0/…`, `/{tenant}/v2.0/.well-known/…` and
   `/{tenant}/discovery/…`, and `<label>.sharepoint.com` / `<label>-my.sharepoint.com` (pre-authenticated
   downloads and upload sessions). Jira: `<site>.atlassian.net` and `/ex/jira/{cloudId}/`. YouTrack:
   `<name>.youtrack.cloud` and `<name>.myjetbrains.com`. This is what lets two Microsoft tenants sign in at once
   in two worlds (`tests/serve/test_world_keys.py`); before, the second tenant's sign-in reached the first's
   world and was refused 400.
3. **A credential the call carries**, when a world claims it (`claims.tokens`). Read only where the OAuth
   standards put one (`adapters/proxy/credentials.py`): `Authorization: Bearer`, the user and password of
   `Authorization: Basic`, `access_token` in the query or a form body, Slack's legacy `token` form argument, and
   a token request's `refresh_token`, `code`, `client_id`, `client_secret` (RFC 6749 §2.3.1) and its `assertion`'s
   or `client_assertion`'s `iss` and `sub`, whether the request is a form or a JSON object (Atlassian's and
   Notion's token endpoints take JSON). No provider's own format is read.
4. **Minted credentials.** When a call of a world is answered with an OAuth token answer (RFC 6749 §5.1), its
   `access_token` and `refresh_token` are claimed by that world from then on. A Google service account claimed
   by its email signs in and its Drive calls follow it (`test_a_service_account_signs_in_and_its_minted_token_is_routed_to_the_same_world`).
5. **The default world**, when one is open (`claims.default: true`; at most one).
6. **None**: the call is refused with 502 and kept in the lobby, read with `GET /v1/unmatched`.

A token, key or host is claimed by one open world at a time; a second claim is refused (409).

Why not the other two options. *One current world with a reset between tests* breaks the moment two tests run
at once. *A header naming the world* needs the service under test to send it, which means changing its code.

**The limit.** Isolation is only as fine as the credentials the services use. A stack whose services hold one
fixed token for a provider (one Slack bot token in the environment) sends every test's calls with the same
token; two worlds cannot both claim it, so such tests share one world (`default: true`, or that token) and must
run one at a time against it. A service that keeps a credential per tenant (an installation token per Slack
workspace, a Personal Access Token per account, a tenant per Microsoft directory) isolates per test once each
test's world claims its own.

Per provider, given only what a client sends:

| Provider | Per-test worlds | What names the world |
|---|---|---|
| Slack | Yes, when each test's service holds its own bot token | The bot token; each world seeds its own workspace (`SlackSeed.workspaces`: team id, bot user, tokens), so two worlds' teams differ, and one world may hold an agent installed in two workspaces |
| Microsoft (Teams, Graph, SharePoint) | Yes | The tenant in the sign-in path, then the tokens it mints; each seed name gives its own tenant, domain, SharePoint host and bot id and secret. The Bot Framework's keys at `login.botframework.com` are the same in every world and answered with no world (`Manifest.shared_hosts`) |
| Asana | Yes | The bearer token |
| Jira | Yes | The site host (`<site>.atlassian.net`), the cloud id in `/ex/jira/{cloudId}/`, or the Basic password |
| YouTrack | Yes | The instance host or the permanent token |
| Notion | Yes | The integration token, or a public integration's token request |
| GitHub | Yes | The personal access token |
| Google Drive | Yes | The refresh token or the service account's assertion, then the token Google's endpoint mints |
| AWS | No | SigV4 is no OAuth credential: a world claims the host, or is the default, one at a time |

## The control API

Every request and answer is a model in `src/minutehand/adapters/control/wire.py`: frozen, and an unknown field
is refused with 422. A refusal is `{"error": "...", "kind": null}`: 404 for a world that is not open, 409 for what a world
cannot do (with `"kind": "unsupported"` when the provider cannot do it in any world; the client raises
`Unsupported`), 422 for a body that is not the model, 502 when the service an event was pushed to refused it.

| Route | Body → answer | What it does |
|---|---|---|
| `GET /v1/health` | → `ok` | |
| `GET /v1/ca.pem` | → PEM | The bundle a service trusts: public roots, then the proxy's CA |
| `GET /v1/environment[?ca_path=P][&no_proxy=H]...` | → `Environment` | The variables a service needs: proxy, `NO_PROXY` (with each `no_proxy` service of the stack and the server's own name; also as `no_grpc_proxy`), the CA variable of each HTTP library (`GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` too), OTLP |
| `GET /v1/worlds` | → `WorldList` | Every open world |
| `POST /v1/worlds` | `CreateWorld` → 201 `WorldView` | Open a world from a seed, with its claims, inbound targets, faults, outbound hosts, and whether scripted people speak |
| `GET /v1/worlds/{id}` | → `WorldView` | Its clock, its head, what it owes |
| `DELETE /v1/worlds/{id}` | → `Checked` | Close it: the checks as it stood, its record written, its claims released |
| `GET /v1/worlds/{id}/events?provider&kind&actor&operation&since[&since_reset=false]` | → `EventsPage` | The log, filtered; `since` is a seq |
| `GET /v1/worlds/{id}/entities?provider&kind` | → `EntitiesPage` | Each entity's latest version, in the provider's own JSON |
| `GET /v1/worlds/{id}/calls[?unmatched=true][?captured=true][?tunnelled=true][?since_reset=false]` | → `CallsPage` | Every call; `unmatched`: those refused because no provider claims and no declaration captures their host; `captured`: those to the world's outbound hosts; `tunnelled`: bursts on tunnels to a model host the world declared, relayed and never opened (`Exchange.tunnelled`: bytes each way, when, never what was said) |
| `GET /v1/worlds/{id}/spans[?since_reset=false]` | → `SpansPage` | Spans the services exported in traces this world's calls carried |
| `POST /v1/worlds/{id}/act` | `ActRequest` → `Acted` | A person acts: `say`, `reply`, `move_ticket`, `edit_ticket`, `happen` (any happening, now), `press` (a control on a message, now) |
| `GET /v1/worlds/{id}/clock` | → `WorldView` | |
| `POST /v1/worlds/{id}/clock` | `Advance` → `Advanced` | Move the clock `by` or `to`, firing what falls due |
| `POST /v1/worlds/{id}/faults` | `Fault` → `WorldView` | Answer the next matching calls with a status and body of the caller's |
| `POST /v1/worlds/{id}/provider-faults` | `DeclareFaults` → `WorldView` | A provider's own typed faults and switches, as a fragment of its seed model |
| `POST /v1/worlds/{id}/seed` | `FurtherSeed` → `Seeded` | More seeded into the open world: people, tickets, documents, spaces, sign-ins, channels, a provider seed fragment |
| `POST /v1/worlds/{id}/people` | `ChangePerson` → `Acted` | A person's account removed, deactivated or reactivated in one provider |
| `POST /v1/worlds/{id}/permissions` | `Permit` → `Acted` | A named permission granted or withheld for a person on a project |
| `POST /v1/worlds/{id}/inbound-credential` | `MintInbound` → `Minted` | The headers a provider's service would send with a request the test builds itself |
| `POST /v1/worlds/{id}/reset` | → `WorldView` | Back to the seed it was opened with, in place: the same id, claims, inbound targets and secrets; its record kept |
| `GET /v1/worlds/{id}/state?provider=P` | → `RawState` | Every version of every entity the provider holds, deleted ones too. For a person debugging; unstable |
| `GET /v1/worlds/{id}/checks` | → `Checked` | Every deterministic check and the scorecard over the world now |
| `GET /v1/providers` | → `ProvidersView` | What each installed provider can be asked to do while a world is open |
| `GET /v1/unmatched?since=N` | → `Unmatched` | Calls no open world claimed, among them bursts on tunnels to a model host no world declared (`--model-host`, or a default one); `head` is the position to read on from |

A provider the seed names (its tickets', documents' and inbound targets' providers) is seeded when the world
opens; any other is seeded on the first call to it, or the first read that names it (`?provider=`). A
`?provider=` that names one of the world's outbound declarations reads the messages its sends wrote.

### Outbound hosts

`CreateWorld.outbound` takes the entries an agent file's `outbound` does (`docs/capture.md`): an email API
acknowledged without sending, a search passed through, a lookup replayed from a closed world or run under the
server's state directory. A call reaches a world's declarations only once it is that world's by its claims, so
a service's email client that carries the world's token (or posts to a host the world claims) is answered by
its own world's declaration; another world may declare the same host differently, and a world that declares
nothing refuses it. A declared host a provider claims is refused with 409, naming both. `serve
--capture-unknown` passes every undeclared call through instead, kept in its world or the lobby.

### Model hosts

A model API is decided by its host before its call is opened, so it cannot be told apart by world from the
credentials it carries. `api.openai.com`, `api.anthropic.com` and `generativelanguage.googleapis.com`, and each
`serve --model-host HOST` (a self-hosted model, a gateway), are model hosts for every world: tunnelled, never
decrypted, or, under `serve --record-model-calls`, opened, sent on unchanged and kept as a span
(`docs/design.md`, Hosts the proxy does not own). A world may declare more:

```json
{"model_hosts": [{"host": "llm.internal", "record": true}]}
```

Each is tunnelled, or recorded when `record` says so, and belongs to that world until it closes: a second open
world that declares an overlapping host is refused with 409, as is a host a provider claims or the world
captures under `outbound`. A recorded call is kept in the world that declared its host, else in the world whose
calls carried its trace (`traceparent`), else in the lobby; `GET /v1/worlds/{id}/spans` reads it.

### A seed

`Seed` (`domain/scenario.py`) is a scenario file with nothing to achieve: the goal and expectations may be left
out, the owner defaults to the first person, and at least one person is required. A whole scenario file loads
too; its expectations are what `checks` holds the world to. Its `provider_seeds`, `channels`, `spaces`,
`sign_ins` and `happenings` are accepted as in a run; each happening falls due when the world's clock is advanced
past it, whether or not scripted people speak, and one aimed at a provider without its family's port refuses the
world at creation.

```json
{
  "seed": {
    "people": [{"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com",
                "reply": {"kind": "scripted", "delay": {"shortest": "PT1H", "longest": "PT1H"},
                          "replies": [{"to_ask": 1, "text": "Yes, Thursday works."}]}}],
    "tickets": [{"provider": "asana", "project": "Launch", "title": "Legal review", "assignee": "sofia"}]
  },
  "claims": {"tokens": ["xoxb-test-7f3a", "asana-pat-7f3a"]},
  "inbound": [{"provider": "slack", "url": "http://platform:8025/api/v1/slack", "secret": "<the service's signing secret>"}],
  "scripted_people": true
}
```

### People and time

The clock of a world stands still. Nothing fires on its own.

- **`scripted_people: false`** (the default): nobody answers but the test. `say` is a DM to the agent's bot,
  `reply` answers a message (in its thread in a channel, a new message in a DM); both are recorded as actor
  `PERSON` and pushed to the world's inbound target, signed with its secret, exactly as the run loop pushes them.
  `move_ticket` is the assignee completing, cancelling or reopening a ticket (actor `PERSON`); `edit_ticket`
  reassigns it or sets its state from outside (actor `SCENARIO`). A world with scripted people off ignores the
  seed's scripts, fates and directions however far the clock moves.
- **`scripted_people: true`**: each message the agent sends a scripted person is answered after the person's
  delay, each ticket handed to a person with a `TicketFate` meets it, and the owner's directions are said, each
  only when the clock passes its moment. A message is answered as it read when it was first seen; an edit is not
  put to the person again. A person whose replies a model writes is refused: a standing world has no model.
- **`happen`** lands one happening of any family now, as the clock lands a scheduled one: checked first as a
  scenario's would be (its person, its ticket, document, channel or post, the port its provider has, and the
  document changes its manifest allows), refused 409 with nothing written otherwise. **`press`** has a person use
  a control on a message (a button, a person picked, a form filled from `press.form`), pushed to the world's
  inbound target as an interactivity payload. `MinutehandClient.act`, and `OpenWorld.happen` / `.press` on the
  plugin's `minutehand_world`, send them.
- **Booked wakes** (a scheduler provider such as AWS) are recorded in the log and never fired: a booking
  becomes a wake only in the run loop.

### Faults

A fault armed through this route is answered by the server in front of the provider: the next `times` calls of
the world to `provider` whose method matches and whose path, as the provider's app sees it (Asana's `/api/1.0`
removed), starts with `path` get `status`, `body` and an optional `Retry-After`. The body is the caller's to write
in the service's error shape. A provider's own typed faults (Slack's `ratelimited` with `only_rich`, Drive's
`FaultKind`s, YouTrack's path faults, Asana's rate-limit stretches, Jira's rate limits, Notion's rate limits and
conflicts, Microsoft's error codes and held files) are declared in that provider's seed when the world is
created, or on an open world with `provider-faults`: `{"provider": "slack", "seed": {"faults": [...]}}`, a
fragment of the provider's seed model setting only its fault fields. The provider validates it (409 naming what
else it sets or what it names that the world does not hold) and records it as seeding does, its offsets counted
from the world's now. `OpenWorld.declare_faults(provider, fragment)` sends it.

### Changing a world while it is open

What the old emulators' admin routes did to a running fake is a typed request here, validated by the provider
that owns the thing, and recorded in the world as a change by actor `SCENARIO` at the world's clock, so the log
says what the test did and when.

- **A further seed** (`POST /seed`, `FurtherSeed`; `OpenWorld.seed()`, `.add_person()`): more people, tickets,
  documents, spaces, sign-ins, channels, or a fragment of a provider's own seed, in the same models a world is
  opened with. Each provider the world already holds is seeded twice in a scratch store, from the scenario before
  and after the addition, starting at the position in the log it was first seeded at, and the world is given what
  the second wrote that the first did not (`application/further_seed.py`). A provider fragment is merged into
  the provider's seed: a list grows, an object merges field by field, a value it sets must agree, and the whole
  is validated by the provider's model. `Seeded.written` says how many things each provider was given. Refused
  with 409 and nothing written when the grown scenario is not one (a key or title taken), when a value
  contradicts the seed, when the addition would renumber what is there, when it would rewrite something that has
  changed since it was seeded, or when it would take an id in use. Jira, YouTrack, Drive, Notion and Microsoft put
  the log's position into the ids of what they seed (an issue, a file, a page's blocks), so a person added to one
  of them while it holds seeded issues, files or pages is refused (open the world with them), as is a ticket added
  to Jira; tickets added to YouTrack and Asana, documents added to Drive, Notion and Microsoft, and people added to
  Slack and Asana land.
- **A person's account** (`POST /people`, `ChangePerson`; `OpenWorld.remove_person()`, `.deactivate_person()`,
  `.reactivate_person()`): what each provider can show is its `Manifest.people_changes`; anything else is 409
  with `kind: unsupported`, and the client raises `Unsupported`.
- **A permission** (`POST /permissions`, `Permit`; `OpenWorld.grant()`, `.withhold()`): a named permission held
  or withheld for a person on a project, after which the next call that needs it is answered accordingly.
- **A ticket deleted** (`act` `delete_ticket`; `OpenWorld.delete_ticket()`): by its assignee, or the owner when
  unassigned, one the agent filed or one seeded; afterwards the service answers for it as for one that never
  was. A `TicketFate` with `deleted: true` does the same when the clock passes it, in a run and in a world.
- **Switches** the emulators exposed (a page size, a tree cut short, a send answered without an id) are typed
  faults and settings of the provider's own seed, declared through `provider-faults` like any fault.
- **Reset** (`POST /reset`; `OpenWorld.reset()`): back to the seed the world was opened with, in place: the same
  id, claims, inbound targets and secrets, its clock at its start, the faults it was opened with armed again.
  Tokens its fakes minted are no longer claimed, and further seeds are gone. Its STATE goes back; its RECORD
  does not: the log so far is kept beside the world (`resets/<n>.db`) and never discarded. `events`, `calls`
  and `spans` read since the last reset, as they always have, and with `?since_reset=false`
  (`OpenWorld.calls(since_reset=False)`, `.events(...)`, `.spans(...)`) read the whole record, the stretch before
  each reset first, with `resets` on the page giving, for each reset, the index of the first item after it. Each
  stretch numbers its events from 1. `WorldView.resets` counts the resets.
- **Raw state** (`GET /state?provider=P`; `OpenWorld.raw_state()`): every version of every entity the provider
  holds, deleted ones too, in its own JSON. For a person debugging; its shape is the provider's and changes with
  it, so a test asserts on `events`, `entities` or the vendor API instead.

`GET /v1/providers` (`MinutehandClient.providers()`) lists, per installed provider, which of these it has.

### Requests a test builds itself

A test that must call the service as the platform does, with a request it builds, asks the world for the
credential the platform would send with it (`POST /inbound-credential`, `MintInbound`;
`OpenWorld.inbound_credential(provider, InboundCredentialAsk(...))`): for Slack, `X-Slack-Request-Timestamp` and
`X-Slack-Signature` over the body at the timestamp given, signed with the world's inbound secret for Slack; for
the Bot Framework, `Authorization: Bearer <JWT>` for the `service_url` and `audience` given, signed with the key
the fake publishes. A world with no Slack inbound target has no signing secret, and the request is refused. The
supported path is still the acts above, which build, sign and push the request themselves.

## Each world is a run

A world is a run in the state directory, named by its `world_id`:

```
<state>/runs/<world_id>/world.db       its log
<state>/runs/<world_id>/scenario.json  the seed as the world plays it
<state>/runs/<world_id>/world.json     its name and claims (marks it a standing world)
<state>/runs/<world_id>/record.json    once closed: stop `closed`
<state>/runs/<world_id>/result.json    once closed: the checks as it stood
<state>/runs/<world_id>/resets/<n>.db  its log before its n-th reset
<state>/runs/lobby-<id>/world.db       calls no world claimed
```

`minutehand findings <world_id>`, `minutehand view` and the MCP tools read it like any run. Closing a world keeps
the newest `--keep` closed worlds (default 100) and removes older ones. A world open when the server stops is
closed then.

## From pytest

`minutehand.testing` ships a client and a plugin; pytest loads the plugin from its entry point once `minutehand`
is installed. It adds fixtures and nothing else, and imports nothing of Minutehand until one is requested.

```python
# conftest.py
import pytest
from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed


@pytest.fixture
def minutehand_spec(request) -> CreateWorld:
    return CreateWorld(
        seed=Seed.model_validate({"people": [{"key": "sofia", "name": "Sofia", "email": "sofia@example.com"}]}),
        claims=Claims(tokens=[f"xoxb-{request.node.name}"]),
    )


# test_reminders.py
def test_the_reminder_reaches_sofia(minutehand_world, gateway):
    gateway.remind(token=minutehand_world.view.claims.tokens[0])
    minutehand_world.assert_message(containing="reminder", to="sofia@example.com")
```

| Fixture | Scope | What |
|---|---|---|
| `minutehand` | session | `MinutehandClient` to `$MINUTEHAND_URL`, or to a server started in this process |
| `minutehand_spec` | test | The suite defines it; the default fails with how to |
| `minutehand_world` | test | `OpenWorld`: `events()`, `entities()`, `calls()`, `unmatched_calls()`, `captured_calls()`, `raw_state()`; `say()`, `reply()`, `happen()`, `press()`, `move_ticket()`, `edit_ticket()`, `delete_ticket()`; `seed()`, `add_person()`, `remove_person()`, `deactivate_person()`, `reactivate_person()`, `grant()`, `withhold()`, `declare_faults()`, `arm()`, `reset()`, `inbound_credential()`; `advance()`, `checks()`, `assert_events()`, `assert_message()`, `assert_ticket()`; closed after the test |

A failed `assert_*` prints the world's latest changes. `AsyncMinutehandClient` is the same client for an async
suite.

## One container in a stack

`docker run <image>` serves: proxy 8080, control API 8081, OTLP 4318. For a Compose stack:

```
minutehand env --format compose --serve-as minutehand --service platform --service worker --no-proxy firestore
```

writes the override: the `minutehand` service (healthy once its control API answers, its CA directory in the
named volume `minutehand-ca`), and each named service given the variables, the CA read-only at
`/etc/minutehand/minutehand-ca-bundle.pem`, and a wait for it to be healthy:

```yaml
services:
  minutehand:
    image: minutehand
    command: [serve, --host, 0.0.0.0, --agent-host, minutehand]
    volumes: ["minutehand-ca:/var/lib/minutehand/ca"]
    healthcheck:
      test: [CMD, /opt/minutehand/bin/python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/v1/health')"]
      interval: 1s
      retries: 30
  platform:
    environment:
      HTTPS_PROXY: http://minutehand:8080
      NO_PROXY: 127.0.0.1,platform,worker,firestore,minutehand,localhost
      SSL_CERT_FILE: /etc/minutehand/minutehand-ca-bundle.pem
      REQUESTS_CA_BUNDLE: /etc/minutehand/minutehand-ca-bundle.pem
      OTEL_EXPORTER_OTLP_ENDPOINT: http://minutehand:4318
      # ... and the other CA and proxy spellings
    volumes: ["minutehand-ca:/etc/minutehand:ro"]
    depends_on: {minutehand: {condition: service_healthy}}
volumes:
  minutehand-ca: {}
```

A container that cannot share a volume downloads the bundle at start from `GET http://minutehand:8081/v1/ca.pem`
(`tests/packaging/test_stack.py` does exactly that from a second container on the same network). The test
process on the host reaches the control API at the published port and sets `MINUTEHAND_URL`.

## Measured

On the development machine (macOS, Python 3.13), 2026-10-04, `tests/serve/test_footprint.py` and a throwaway
script over the same code:

| Measure | Value |
|---|---|
| `minutehand serve` started to `/v1/health` answering | 0.56–0.73 s at low load; up to 4 s with the machine's load average at 20. The in-process test bound is 10 s. |
| Memory after start | 127 MB RSS |
| Memory over 2,000 worlds opened, read and closed | 128 MB after 100, 83 MB after 1,000, 81 MB after 2,000: no growth |
| One world opened, read and closed | 6–9 ms |

## What it does not do

- **A channel archived** by a person: no act.
- **Retries of a pushed event or webhook.** Slack's retries are sent; Notion's and Graph's deliveries are sent
  once.
- **Fixed ids the emulators used** (`U001`, `C001GENERAL`, a fixed Asana gid): ids here derive from names and
  positions, so a test reads them back from the world (`entities`) or the vendor API after the world opens.
- **AWS per world by credential.** A SigV4 request carries its access key id in a scheme the OAuth standards do
  not define, and the router reads no provider's own format; AWS calls reach a world by a host it claims or the
  default world.
