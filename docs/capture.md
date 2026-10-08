# Capturing outbound calls

**The rule: fake where the agent keeps state; capture everything else.**

Slack, a tracker, a document store are places the agent keeps state: it writes something there and reads it
back later, and a person acts on it. Those are providers, faked by Minutehand, and the world's log is theirs.
An email API, a webhook, an SMS gateway, a web search, a page fetch are not: the agent sends something out, or
asks something and uses the answer, and never reads its own writes back. Faking them buys nothing. They are
**captured**, by declaration, with no provider code. An API the agent does write to and read back, which no
provider fakes, is declared `store`: what it writes is kept as sent, in the run's world, and read back unchanged.

Without a declaration, a host no provider claims is refused with 502, recorded, and reported by
`unmatched_call`. Built and tested: `src/minutehand/domain/outbound.py`, `adapters/proxy/capture.py`,
`adapters/proxy/stored.py`, `tests/capture/`, `tests/e2e/test_capture_run.py`, `tests/serve/test_capture_worlds.py`.

**Authentication always passes on a declared host.** Minutehand never reads, checks or keeps the credentials the
agent sends a declared host: any key, or none, is let in, whatever the mode.

## The modes

One entry per host, or per `*.` wildcard, under `outbound` in the agent file:

```yaml
outbound:
  - host: api.mail.example
    kind: acknowledge
    answer: {status: 202, json_body: {id: queued}}       # default: 200 {}
    routes:                                              # optional, first match wins
      - {method: POST, path: "/v3/batch/*", answer: {status: 201, text: batched}}
    message:                                             # optional: each call is also a message to a person
      recipients: ["personalizations[*].to[*].email"]
      text: [text, {html: "content[0].value"}]           # candidates, first present wins; html reduced to text
      subject: [subject]
      handles: {"+15550100": sofia}                      # a recipient value that is not an email -> Person.key
    redact: ["personalizations[*].custom_args.secret"]   # body fields kept as [redacted], besides credentials
    body_limit: 1048576                                  # bytes of a text or JSON body kept (the default)

  - host: search.example
    kind: pass_through
    in_forks: replay                                     # the default; or pass_through

  - host: "*.weather.example"
    kind: replay
    source: {kind: run, run: 3f2a9c1e07bb}               # or {kind: directory, directory: fixtures/weather}
    on_miss: refuse                                      # the default; or pass_through
    ignore_query: [nonce, ts]
    ignore_body: [sent_at, request_id]
```

| Mode | The call | What is kept | For |
|---|---|---|---|
| `acknowledge` | Never leaves the machine. Answered with the declared status, headers and JSON or text body, or a route's | The request, the declared answer | Sends: an email, a webhook, an SMS |
| `pass_through` | Sent to the real host unchanged; the answer reaches the agent chunk by chunk as it arrives (the tee `--record-model-calls` uses) | The request and the real answer | Lookups: a search, a page fetch |
| `replay` | Answered from an earlier run's recording of the same call, marked `x-minutehand-replayed: <source>` | The request and the replayed answer, with `replayed_from` | Lookups whose answers must not drift between runs |
| `store` | Never leaves the machine. Kept in the run's world and answered from it, by REST collections (below) | The request, the answer, and each item as a world event | An API the agent writes to and reads back that no provider fakes: a CRM, a notes API |
| `forward` | Sent to an external emulator the same file declares (`emulators:`), streamed, with `x-minutehand-world`, `x-minutehand-wake`, `x-minutehand-time` and a continued `traceparent` added to the forwarded copy only | The request and the emulator's answer, with the emulator, the operation and what the answer was (`Exchange.outcome`) | A service with a fake outside Minutehand: `docs/external-emulators.md` |

`name` (default: the host with every other character an underscore, `api_mail_example`) is what a send's
messages, and a store's items, are recorded under; it may not be a provider's name. Two declarations of one host,
or of overlapping hosts, are refused. **A model API cannot be declared:** the run is refused before it starts,
naming it. A host a provider claims can be (below, "Hosts a provider claims").

A path into a body is dotted keys, `[n]` for one list item and `[*]` for every item. A JSON body is read as
JSON and a form body by its fields; anything else has no paths.

## `store`: what the agent writes, kept as sent

```yaml
outbound:
  - host: api.crm.example
    kind: store
    collections:
      - path: /v1/contacts                    # GET lists, POST creates; /v1/contacts/{id}: GET, PUT, PATCH, DELETE
        id: {at: id, format: uuid}            # uuid (default) | integer | prefixed (with prefix: "ct_") | sent
        stamps:                               # optional: moments the API writes, from the run's clock
          - {at: created_at}                  # on: create (default) | write; format: iso8601 | epoch_seconds | epoch_milliseconds
          - {at: updated_at, on: write}
        listing:                              # how a GET of the collection answers
          items_at: results                   # absent: the bare JSON list
          envelope: {object: list}            # fields answered beside the items, as given
          limit_param: limit                  # a page of at most ?limit= items (default_limit: 100)
          cursor_param: after                 # ?after=<the last id the agent has>
          next_at: paging.next.after          # where the next page's cursor goes; absent on the last page
        created_status: 201                   # the defaults
        deleted_status: 204
      - path: /v1/companies/{company}/notes   # a `{name}` segment: each company's notes are a collection of their own
        name: notes                           # what an assessment counts it by; default: the last literal segment
    routes:                                   # optional, as for acknowledge: answered first
      - {method: POST, path: /v1/contacts/search, answer: {json_body: {results: []}}}
    answer: {status: 404, json_body: {message: not found}}   # any other call; default: 200 {}
```

**Data stays as sent.** A POST to a collection stores the JSON object the agent sent, every field and value as
sent, and answers it; Minutehand writes into it only what the collection declares the API assigns: the id at `id.at`
and each of `stamps`. A GET of the item answers it exactly as stored; a GET of the collection lists the items in
the order they were created, each as stored, in the declared envelope. PUT replaces the item with what it sends,
PATCH merges the top-level fields it sends over the stored ones; either way the id and the `on: create` stamps stay
as the API assigned them, and the `on: write` stamps move. DELETE removes it and answers the declared status with no
body. An item no call created is 404; a body that is not a JSON object, a `sent` id missing from the body, an
unknown cursor or a bad limit is 400; a second create of a `sent` id is 409; each says why in
`{"error": ..., "host": ...}`. Any other method, and any path under no collection, is answered by the first route
that matches, else by `answer`. The JSON's spacing is not kept, its fields and values are.

An id is made from the event that creates the item: `integer` is its sequence number, `prefixed` the prefix and
that number, `uuid` a UUID derived from the host, the collection's path and that number; so a fork, or a rerun,
hands out the same ids. `sent` keeps the id the agent sent at `id.at`.

**Secrets still never reach the store:** a field named as a credential (`redact.CREDENTIAL_KEYS`: `token`,
`password`, `api_key`, ...) or listed in `redact` is stored, and read back, as `[redacted]`; this is the one way
an item differs from what was sent.

Each item is a world event: a `STORED` entity under the declaration's `name` (`StoredSnapshot`: the host, the
collection, the path it is under, its id, and the item as stored), created, updated and deleted by the agent. So
it is in the run's file and nowhere else, a fork sees the items as they stood at its checkpoint (and writes its
own after it), `writes: {things: [stored]}` counts each write, and an assessment counts what a
collection holds (`docs/assessments.md`):

```yaml
assess:
  - id: one_contact_per_lead
    count: {stored: {host: api.crm.example, collection: contacts, values: {stage: lead}}}
    at_most: 1
```

`tests/capture/test_store.py`, and a whole run and its fork in `tests/e2e/test_capture_run.py`.

## Hosts a provider claims

A host a provider claims may also be declared, in any mode. The provider answers every call it serves, exactly as
without the declaration. A call it says it does not serve (`domain.errors.NotServed`: a Slack Web API method the
fake leaves out, a Graph `$filter` it does not read, a Cloud Tasks method; the 501 that names the method) goes to
the declaration instead and is answered as the declaration says: kept by a `store`, acknowledged, passed through,
replayed. Its record says so: no provider, captured as declared, and `Captured.not_served_by` naming the provider
that did not serve it; the run's outbound summary counts them (`2 not served by slack, answered as declared`).
Without a declaration for the host, the call is refused by name, 501 in the vendor's shape, as before. Only a
`NotServed` falls through: a call a provider refuses as the real service would (a 404, Slack's `ok: false`) is
the provider's answer, and a gRPC call or a socket is never handed on.

```yaml
outbound:
  - host: slack.com
    name: slack_extra                       # not `slack`, the provider's own name
    kind: store
    collections: [{path: /api/reminders.add}]   # reminders.add, which the Slack fake does not serve
```

`test_a_method_slack_does_not_serve_falls_through_to_the_declared_store_and_what_it_serves_does_not`,
`test_a_method_slack_does_not_serve_without_a_declaration_is_refused_by_name`.

A `forward` host's emulator that is down or does not answer is never bypassed: the call is answered 502 or 504
naming it and kept `unavailable`, and the run's environment failed (exit 2). A fork after the run first used an
emulator is refused, since what it holds is outside the record (`docs/external-emulators.md`).

## Discovery: `--capture-unknown`

**Only reads: `--capture-unknown reads`.** A GET, HEAD or OPTIONS to an undeclared host is passed through and kept, as below; any other method is refused with 502 and recorded, as without the flag, so nothing is written anywhere real. A read an API sends as a POST (GraphQL, an RPC) is refused: declare its host. `UnknownHosts.READS`, `tests/capture/test_modes.py`.

For an agent's first run, when nobody knows yet what it calls: `minutehand run … --capture-unknown` (and `fork`,
and `serve`) passes every undeclared, unclaimed host through and keeps the call, instead of refusing it. The
run then ends with each host, its calls and how they were answered, and a declaration for each host nobody
declared:

```
outbound calls
  search.example: 3 calls, passed through, undeclared (--capture-unknown)
  api.mail.example: 1 call, passed through, undeclared (--capture-unknown)

to capture the hosts nobody declared, add to the agent file (acknowledge: answered here
and never sent; pass_through: sent to the real host; replay: answered from a run):
  outbound:
  - host: search.example
    kind: pass_through
  - host: api.mail.example
    kind: acknowledge
```

A host called only with POST, PUT, PATCH or DELETE is suggested as `acknowledge`, anything else as
`pass_through`. **Discovery sends for real:** in that first run, the email above went out. Run it where the
agent's credentials reach a sandbox, or not at all against production keys. Off by default. Without the flag,
the same summary lists refused hosts (`refused: nobody declares it`) and suggests their declarations too.

## What is kept

Each captured call is an `Exchange` carrying `Captured` (`domain/world.py`), in the run's `exchange` table and,
when the run ends, in `<state>/runs/<run_id>/captured.jsonl`: method, host, path with its query, status, the
caller's `traceparent`, the wake and simulated time (on `RecordedCall`), the real start and end, the mode and
what answered it (`declaration`, `real_host`, `recording`, `refusal`), and each body: its content type, its
whole size, a SHA-256 of the redacted whole, and the text up to `body_limit`. A body whose content type is
neither text, JSON, XML nor a form is kept as its length and type only (`binary`); a longer text body is cut
(`truncated`). A passed-through answer longer than 64 MiB reaches the agent whole and is kept as its length.

**Secrets never reach the store.** No header is kept, so `Authorization`, `Cookie` and API-key headers never
are. Query parameters named as credentials (`redact.CREDENTIAL_KEYS`, plus `key`, `api_key`, `signature`,
`sig`, `x-amz-*` credentials and the rest of `redact.CAPTURED_QUERY_KEYS` on a captured host) and body fields
so named are `[redacted]`, and so is every body field the declaration lists in `redact` (and, when it is a
plain name, the query parameter of that name). Redaction happens before the store sees a body, so before it is
hashed and kept once (`docs/design.md`, "Bytes kept once"). `test_no_secret_reaches_the_store_from_headers_query_or_bodies`
searches the bytes of the world file, its write-ahead log, `captured.jsonl`, and every stored body and snapshot
file decompressed, for each.

Keeping a body once changes nothing above: the limits, what is kept of a binary or a cut body, and the bytes
read back are what they were. A body the run sees many times (the same listing, the same upload) costs its
bytes once per world file.

## A send as a message to a person

With `message`, an acknowledged call is also a world event: a `MessageSnapshot` from the agent, recorded under
the declaration's `name`, to each recipient read from the body. A recipient is matched to a scenario person by
email, in any case, or by `handles`; an address that matches nobody stays in the message as written, and the
run's outbound summary lists it ("sent to someone the scenario does not know"). The text is the first candidate
present, HTML reduced to its text, with the subject line before it. A body with no recipient or no text is
kept as a call, with a note saying why it is no message.

So the ledger, the scorecard ("messages to people") and the expectations (`person_asked`, `relayed`) see an
email exactly as they see a Slack message, and a team's rules count it among `messages` (a rule with
`gap_at_least: PT5M` on messages to one person flags the same email sent twice within five simulated minutes).

**People answer a captured send when the declaration says how an answer reaches the agent.** An email API's
inbound parse, an SMS gateway's webhook: the agent has an endpoint where answers arrive, and `replies` declares
the request that endpoint expects, with no provider code:

```yaml
  - host: api.mail.example
    kind: acknowledge
    answer: {status: 202, json_body: {id: "{message_id}"}}     # an id made for each send
    message: {recipients: ["personalizations[*].to[*].email"], text: ["content[0].value"], subject: [subject]}
    replies:
      url: http://127.0.0.1:8790/inbound/email                  # the agent's own inbound webhook
      method: POST                                              # or PUT
      headers: {x-source: mail}                                 # sent as given
      body: {id: "{reply_id}", from: "{from}", text: "{text}", in_reply_to: "{in_reply_to}"}
      form: false                                               # true: a flat body sent as a form
      thread: id                                                # path into the send's own answer -> {in_reply_to}
      signing:                                                  # optional: HMAC-SHA256 over the body
        secret: {kind: generated, env: MAIL_SIGNING_SECRET}     # or {kind: from_env, env: ...}, as inbound targets
        header: X-Mail-Signature
        format: "sha256={hex}"                                  # {hex} or {base64}; {timestamp} signs "<ts>.<body>"
        timestamp_header: null                                  # a header carrying {timestamp} alone, if any
```

Without `body`, the reply is sent in the default shape, every field under its own name (`DeliveredReply`,
`deliverReply` in `schemas/agent-api.openapi.json`). The body is structure, and each string in it may name `{reply_id}`, `{from}`, `{from_name}`, `{to}`,
`{subject}` (`Re: ` and the send's), `{text}`, `{in_reply_to}` and `{sent_at}`; any other name is refused when
the file is read. `{message_id}` in an acknowledged answer is replaced by `<name>-<seq>`, so each send has the id a
real email API would hand back, and `thread` reads it back from the answer as stored. A secret `generated` is
handed to the agent's command in its variable, as for an inbound target.

Then a send to a scenario person is answerable (`MessageSnapshot.answerable`): it is one of their asks for the
scripted replier (`to_ask: 1` is their first email, or their first Slack message, whichever came first), a
model-written person reads it, and an answer is decided as for any message. When it falls due, the answer is
written into the world as the person's message, threaded under the send, and then delivered; an answer other
than 2xx from the agent stops the run `AGENT_FAILED`, saying which. The ledger, the expectations (`person_asked`,
`relayed`), the follow-up checks and the scorecard read it exactly as a chat message
(`tests/architecture/test_people_answer.py`).

Without `replies`, nobody can answer: the send **tells and does not ask**, whoever it went to, even a `Silent`
person. An email to someone with a wait already open is still a follow-up on that wait.

## Replay, and why it misses

A call matches a recording on method, host, path, the query with `ignore_query` removed and sorted, and a hash
of the redacted body with `ignore_body` removed (JSON with its keys sorted, a form with its fields sorted). The
nth identical call gets the nth recorded answer to it, and the last one again after that. On a miss,
`on_miss: pass_through` sends the call on and keeps it (`note: not replayed: …`); `refuse` answers 502 saying
what the recording holds.

What makes matching fail in practice:

- **A value that changes on every call** in the query or body: a timestamp, a nonce, a request id, a
  pagination cursor from an earlier answer, a signature over any of these (AWS SigV4 in the query, a
  webhook's HMAC). Each has to be listed in `ignore_query` or `ignore_body`; a signature in a header does not
  matter, since headers are never matched.
- **A body that does not parse:** a multipart upload (its boundary is random), a compressed request body, a
  body in a format other than JSON or a form. It is matched by the hash of its whole text, so any change misses.
- **A body kept only in part:** a request longer than `body_limit` is matched by the hash of its whole, so
  `ignore_body` cannot apply to it; an answer kept only in part, or as binary, cannot be replayed at all.
- **A redaction that changed** between the recording and the replay (another `redact` list) changes the hash.
- **Order:** an agent that makes the same call more times than the recording holds gets the last answer
  again, not a fresh one.

## Forks

A fork shares its parent's log up to the checkpoint, and with it every call the parent captured by then
(`Store.fork` keeps the parent's calls up to the fork). After the fork:

| Declared | In a fork | Why |
|---|---|---|
| `acknowledge` | Answered as declared | It never left the machine anyway |
| `store` | Answered from the fork's world: the items as they stood at the checkpoint, then the fork's own writes | The items are world events, so the fork's log has them as it has everything else |
| `pass_through`, `in_forks: replay` (**the default**) | Answered from the parent's recording of the same call, the parent's calls after the fork first; on a miss, sent to the real host and kept | A fork is a comparison: it should differ from its parent only by what it changed. A search that answers differently a day later would be a second change nobody asked for |
| `pass_through`, `in_forks: pass_through` | Sent to the real host again | When the point of the fork is to see today's answer |
| `replay` | From its own source, as declared | |

`test_a_fork_replays_its_parents_lookups_by_default_and_calls_the_real_host_when_told_to` runs both.

## Standing mode

`CreateWorld.outbound` takes the same entries, per world (`docs/serve.md`). A call reaches a world's
declarations only once it is that world's by its claims (a host or a credential it carries), so two worlds
can declare the same host with different answers; a world that declares nothing refuses it.
`GET /v1/worlds/{id}/calls?captured=true` lists a world's captured calls, `?unmatched=true` its refused ones.
A replay's `{kind: run}` names a closed world or run under the server's state directory.

## Where it shows

- `minutehand run`, `fork`, `findings`: an "outbound calls" section, per host its calls and mode, and
  declarations to add for any host nobody declared (`RunRecord.outbound`).
- The viewer: an "Outbound calls" lane on the timeline (a replayed call hollow, a refused one red) and a list,
  each call opening to its redacted request and answer, a replayed one marked "REPLAYED from a recording".
- MCP: `list_outbound_calls(run_id)`; `show_evidence` gives each cited event's call its `captured` detail.
- Minutehand's own OpenTelemetry: one `SpanKind.CLIENT` span per captured call, `<method> <host>`, in the
  caller's trace when it sent a `traceparent`, with the mode and what answered it; never a body, whatever
  `MINUTEHAND_EXPORT_BODIES` says.
- `unmatched_call`, for every refused unclaimed host, says how to declare it.

## Limits

- Only the declaration says which body field is the recipient: a vendor whose shape differs (a list of
  objects with `address`, `email` or `to` is read; a nested `{"emailAddress": {"address": …}}` is not unless a
  path reaches it) needs its own paths.
- A captured send opens a wait only when its declaration says how answers reach the agent (`replies`); the
  standing mode's `act` cannot yet answer through one.
- A client that pins certificates, or ignores proxy settings, is out of reach, as for providers.
- `replay` answers carry the recorded content type and nothing else of the recorded headers.
- A pass-through call to a host that cannot be reached is kept with 502 and a note; the agent sees the 502.
