# Notion provider

Written from Notion's public API reference and its OpenAPI document. Every error message
is Notion's, documented or reported of the real service; a case whose answer no source
gives is refused by name (501) instead of answered in made-up words. `CLAIMS.md` gives the source of each
behaviour, and `tests/data/notion_api/` the surface it is held to.

**API version:** `2022-06-28`, what `notion-client` 2.2.1 sends. A call carrying any other
`Notion-Version` is refused 501 `invalid_request`, naming it. A call with no version is
refused with `missing_version`. `notion-client` 3.x sends `2025-09-03` by default and calls
`/v1/data_sources/...` instead of `/v1/databases/{id}/query`: both are refused by name
here, so a client on 3.x must pin `notion_version="2022-06-28"`.

**Credentials are not enforced.** Any token, or none, is answered: a token the seed holds
or `/v1/oauth/token` minted acts as its integration, any other as the agent's (the first
integration the seed declares; a seed that declares no integration is refused).
No capability refuses a call, and the token endpoint refuses no client, code, redirect
URI or refresh token.

**Host:** `api.notion.com`. Nothing is served from Notion-hosted file URLs, because no
file is stored.

## Implemented

| Area | Endpoints |
|---|---|
| Search | `POST /v1/search`: `query` (case-insensitive substring of the title), `filter` by object, `sort` by `last_edited_time`, `start_cursor`/`page_size` |
| Pages | `POST /v1/pages` (under a page, a database, or the workspace for a public integration), `GET`/`PATCH /v1/pages/{id}` (properties, `archived`/`in_trash`, icon, cover), `GET /v1/pages/{id}/properties/{property_id}` |
| Blocks | `GET`/`PATCH`/`DELETE /v1/blocks/{id}`, `GET`/`PATCH /v1/blocks/{id}/children` (pagination, nesting, `after`). A page id is also a block id (`child_page`). |
| Databases | `GET`/`PATCH /v1/databases/{id}`, `POST /v1/databases`, `POST /v1/databases/{id}/query` (filters and sorts, see `query.py`) |
| Users | `GET /v1/users`, `/v1/users/me` (the bot, its owner and workspace name), `/v1/users/{id}` |
| Comments | `GET`/`POST /v1/comments` (on a page, or in a discussion) |
| OAuth | `POST /v1/oauth/token`: JSON body, `authorization_code` and `refresh_token`, for the integration the Basic client id names |

**Blocks:** paragraph, heading 1–3 (toggleable), bulleted and numbered list items, to-do,
toggle, code, quote, callout, divider, table and table rows, bookmark, external image,
and `child_page` and `child_database` stubs. A link preview can be seeded and read, but
creating one through the API is refused, as Notion refuses it.

**Rich text:** text (with links), mentions of a user, page, database or date, and
equations. Annotations and `plain_text` are filled in.

**Properties:** title, rich_text, number, select (an unknown option is added to the
schema), multi_select, status (an unknown option is refused), date, people, checkbox,
url, email, phone_number and relation. created_time, last_edited_time, created_by and
last_edited_by are read-only.

**Sharing:** an integration reaches only the pages and databases shared with it, and what
lies below them. Everything else is `object_not_found`, and search leaves it out. A
person's email is answered only to an integration that has `read_users_with_email`.

**Not served, refused by name:** every other operation of Notion's OpenAPI document
(`surface.OPERATIONS`: data sources, page move and markdown, a single comment's retrieve,
update and delete, OAuth introspect and revoke, file uploads, views, custom emojis, meeting
notes, agents and the rest) raises the shared not-served refusal, answered 501
`invalid_request` naming it. Only a path or method Notion does not document is 400
`invalid_request_url` "Invalid request URL.".

**Faults:** declared in the seed (`NotionSeed.faults`), or on an open standing world through
`DeclaresFaults` (the same `faults` fragment, an integration named by its seed key):
- `rate_limited`: 429 with `Retry-After`. `notion-client` 2.2.1 does not retry; it raises `APIResponseError`.
- `conflict`: 409 `conflict_error` on block edits.

A bad property is refused with `validation_error` without any fault being declared.

**Links:** a page's `url` is `https://app.notion.com/p/<Title>-<id>`, a database's and a
mention's `https://app.notion.com/p/<id>`, as Notion's own links read since June 2026.

**Ids:** UUIDs, accepted with or without dashes. A seed derives each id from its workspace
and key. A created object's id is derived from the event that made it.

**Timestamps:** taken from the run's clock and rounded down to the minute, as the API
serves them.

**People acting without the agent:** a `DocumentHappening` through `ChangesDocuments.change`,
on the page or row its seeded document was written as. A scenario's Notion document is a
page in the first workspace, or a seed's own page or row whose `document` names its title.
`Edited` appends paragraphs, `Renamed` sets the title (a row's title property), `Trashed`
archives, `Commented` writes a comment, `FieldSet` sets a row's property from text (a
number, `true`/`false`, option names, comma-separated names or person keys for a property
holding several). `Moved` and `Shared` are refused at load: the manifest does not list them.
Each is recorded as actor `PERSON` at the clock's time.

## Webhooks

Integration webhooks (`webhooks.py`, `NotifiesChanges`), written from Notion's webhook reference
(https://developers.notion.com/reference/webhooks) by reading it. **Unverified against the live
service**: the signature, the verification request and the payloads are this reading of the
documentation.

- **A subscription is seeded** (`NotionSeed.webhooks`: the integration's key, the URL, the
  verification token, the event types, whether it is already verified), as one is set up in an
  integration's settings; the API has no endpoint for it.
- **Verification.** An unverified subscription is first sent `{"verification_token": ...}`. At
  Notion a person pastes the token back into the settings; here it counts as verified once the
  endpoint answers 2xx. Until then nothing else is sent, and what it was owed is dropped.
- **Signature.** `X-Notion-Signature: sha256=<hex>`, the HMAC-SHA256 of the exact body bytes keyed
  with the verification token.
- **Events.** `page.created`, `page.content_updated` (`data.updated_blocks`),
  `page.properties_updated` (`data.updated_properties`, property ids), `page.deleted`,
  `page.undeleted`, `comment.created` (`data.page_id`), each with `id`, `timestamp`,
  `workspace_id`, `workspace_name`, `subscription_id`, `integration_id`, `type`, `authors`,
  `attempt_number`, `entity` and `data.parent`. Only the types a subscription asked for, and only
  for what its integration can see.
- **Whose changes.** A person's, and the integration's own (authored by its bot): the reference
  excludes neither, and a subscriber that must ignore its own writes compares `authors` with its
  bot's id. A person's change is sent when the run tells watchers (`notify`); the integration's
  own as soon as the call that made it is answered.
- **Not reproduced:** aggregation of frequent events, delivery delay and out-of-order delivery,
  retries (each event is sent once), `page.moved`, `page.locked`, data-source events.

## While a world is open (`minutehand serve`)

- A member is removed (`OpenWorld.remove_person("notion", key)`) from every workspace they belong to: `/v1/users`
  lists them no more, `/v1/users/{id}` answers 404 `object_not_found`, and what they wrote still names them.
  Deactivation is refused as unsupported: Notion has no such state for a member.
- Every page version tells its `owner` (who created it: a person's email, or the integration's name) and its
  `space` (the workspace's name).

## In the world log

Each page or row is one entity, and its blocks are stored inside it. A block edit, an
append or a delete is therefore one event per call, with the page as the entity.

| Entity | Snapshot |
|---|---|
| Page | `DocumentSnapshot`: title, the whole text (one block a line, nested blocks indented), the parent's title, and the last editor and time |
| Database row | `RecordSnapshot("database_row")`: its title and properties |
| Database | `RecordSnapshot("database")` |
| Comment | `RecordSnapshot("comment")` |

Reads and searches are recorded as `READ` and `SEARCH` events.

## Not built

**Endpoints and protocol**
- Data sources (`2025-09-03`).
- Webhook subscriptions made or listed through the API (there is no such API; they are seeded).
- File uploads, file, pdf, video and audio blocks.
- Page move and markdown endpoints.
- OAuth introspect and revoke.

**Blocks and properties**
- Blocks: column lists, synced blocks, embeds, equation blocks, `link_to_page`, breadcrumb, table of contents,
  templates, `heading_4`, tabs, and file, pdf, video, audio and uploaded images are refused 501 by name.
- Mentions: `template_mention` and `custom_emoji`, and a date mention with a time or an end (Notion documents
  the `plain_text` of a date alone), are refused 501 by name.
- Properties: formula, rollup, files, unique_id, verification, button, location, place and last_visited_time
  are refused 501 by name.

**Search and limits**
- Search relevance ranking: results come newest first.
- Rate limits, apart from the faults the seed declares.
- Payload size limits, apart from these: 2000 characters per text, 100 rich text items, 100 children and 2 levels of nesting per request, and a page size of 100.
