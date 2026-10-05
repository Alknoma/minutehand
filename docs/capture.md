# Capturing outbound calls

**The rule: fake where the agent keeps state; capture everything else.**

Slack, a tracker, a document store are places the agent keeps state: it writes something there and reads it
back later, and a person acts on it. Those are providers, faked by Minutehand, and the world's log is theirs.
An email API, a webhook, an SMS gateway, a web search, a page fetch are not: the agent sends something out, or
asks something and uses the answer, and never reads its own writes back. Faking them buys nothing. They are
**captured**, by declaration, with no provider code.

Without a declaration, a host no provider claims is refused with 502, recorded, and reported by
`unmatched_call`. Built and tested: `src/minutehand/domain/outbound.py`, `adapters/proxy/capture.py`,
`tests/capture/`, `tests/e2e/test_capture_run.py`, `tests/serve/test_capture_worlds.py`.

## The three modes

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

`name` (default: the host with every other character an underscore, `api_mail_example`) is what a send's
messages are recorded under. Two declarations of one host, or of overlapping hosts, are refused. **A host a
provider claims, or a model API, cannot be declared:** the run is refused before it starts, naming both
(`outbound host 'slack.com' is declared acknowledge, and provider 'slack' claims 'slack.com'`).

A path into a body is dotted keys, `[n]` for one list item and `[*]` for every item. A JSON body is read as
JSON and a form body by its fields; anything else has no paths.

## Discovery: `--capture-unknown`

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
plain name, the query parameter of that name). `test_no_secret_reaches_the_store_from_headers_query_or_bodies`
searches the bytes of the world file, its write-ahead log and `captured.jsonl` for each.

## A send as a message to a person

With `message`, an acknowledged call is also a world event: a `MessageSnapshot` from the agent, recorded under
the declaration's `name`, to each recipient read from the body. A recipient is matched to a scenario person by
email, in any case, or by `handles`; an address that matches nobody stays in the message as written, and the
run's outbound summary lists it ("sent to someone the scenario does not know"). The text is the first candidate
present, HTML reduced to its text, with the subject line before it. A body with no recipient or no text is
kept as a call, with a note saying why it is no message.

So the ledger, the scorecard ("messages to people") and the expectations (`person_asked`, `relayed`) see an
email exactly as they see a Slack message, and `repeated_message` flags the same email sent twice to the same
recipient within five simulated minutes.

**People do not reply through a captured channel in this version.** Nobody answers an email. The rule the
ledger follows: a message opens a wait only when its recipient can answer where it was sent
(`MessageSnapshot.answerable`); a captured send is never one, so it **tells and does not ask**, whoever it
went to, even a `Silent` person, whose every Slack message is a wait. An email to someone with a wait already
open is a follow-up on that wait: they can read it. The scripted replier does not count it as one of their
asks, so a script's `to_ask: 1` is still their first Slack message. What this gets wrong: an agent that asks a
real question by email and waits for the answer is scored as having asked nothing, and its waiting is never
measured.

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
- People do not answer a captured send, and a captured send opens no wait (above).
- A client that pins certificates, or ignores proxy settings, is out of reach, as for providers.
- `replay` answers carry the recorded content type and nothing else of the recorded headers.
- A pass-through call to a host that cannot be reached is kept with 502 and a note; the agent sees the 502.
