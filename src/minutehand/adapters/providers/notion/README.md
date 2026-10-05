# Notion provider

Written from Notion's public API reference, by reading it. No vendor code, payload or
error text is copied; every response body and message here is this provider's own.

**API version:** `2022-06-28`, what `notion-client` 2.2.1 sends. A call carrying any other
`Notion-Version` is refused with a `validation_error` that says so. A call with no version
is refused with `missing_version`.

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
| OAuth | `POST /v1/oauth/token`: Basic client auth, JSON body, `authorization_code` (single use, redirect URI checked) and `refresh_token` |

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
missing capability is `restricted_resource`.

**Faults:** declared in the seed (`NotionSeed.faults`):
- `rate_limited`: 429 with `Retry-After`. `notion-client` 2.2.1 does not retry; it raises `APIResponseError`.
- `conflict`: 409 `conflict_error` on block edits.

A bad property is refused with `validation_error` without any fault being declared.

**Ids:** UUIDs, accepted with or without dashes. A seed derives each id from its workspace
and key. A created object's id is derived from the event that made it.

**Timestamps:** taken from the run's clock and rounded down to the minute, as the API
serves them.

**People acting without the agent:** `NotionProvider.person_edits`, `person_sets_property`,
`person_comments` and `person_archives`. Each is recorded as actor `PERSON`, at the
clock's time, and is visible to the API through `last_edited_by` and `last_edited_time`.
Neither the run loop nor the standing mode calls them yet.

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
- Webhooks: neither subscriptions nor signed deliveries.
- File uploads, file, pdf, video and audio blocks.
- Page move and markdown endpoints.
- OAuth introspect and revoke.

**Blocks and properties**
- Blocks: column lists, synced blocks, embeds, equation blocks, `link_to_page`, breadcrumb and table of contents are refused, saying so.
- Properties: formula, rollup, files and unique_id.

**Search and limits**
- Search relevance ranking: results come newest first.
- Rate limits, apart from the faults the seed declares.
- Payload size limits, apart from these: 2000 characters per text, 100 rich text items, 100 children and 2 levels of nesting per request, and a page size of 100.
