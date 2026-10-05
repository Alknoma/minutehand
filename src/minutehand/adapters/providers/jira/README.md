# Jira Cloud provider

Written from Atlassian's public REST documentation for Jira Cloud (platform v3 and Agile 1.0) and for OAuth 2.0
(3LO) apps, by reading it. No vendor code, client library or example payload is copied; every response body and
error message here is this provider's own wording in Jira's shapes.

## What it serves

| Host | Paths |
|---|---|
| `<site>.atlassian.net` | `/rest/api/3/...`, `/rest/agile/1.0/...`; Basic auth (an account's email and an API token) |
| `api.atlassian.com` | `/ex/jira/<cloudId>/rest/...` (the same site, Bearer access token); `/oauth/token/accessible-resources` |
| `auth.atlassian.com` | `POST /oauth/token`, `grant_type=refresh_token`, JSON or form; refresh tokens rotate |

Issues: create (`fields`, screen-checked per issue type), get (`fields`, `expand=changelog,names`), edit
(`fields`, and `update` for labels and `set`), delete (`deleteSubtasks`), assignee, transitions (list with
`expand=transitions.fields`, do with screen fields and an `update.comment`), comments (ADF in, ADF out, plain text
in the world's snapshot), changelog, links and link types. Search: `/search/jql` (GET and POST, `nextPageToken`,
fields `*all`/`*navigable`/`-x`), `/search/approximate-count`; the retired `/search` answers 410. Projects:
search, get, create, statuses per issue type, roles and adding role members, `createmeta` issue types and
fields. Site objects: `field`, `status`, `statuscategory`, `priority`, `issuetype`, `resolution`. Users: `myself`,
`user`, `user/search`, `users/search`, `user/assignable/search`, `mypermissions`, `serverInfo`. Agile: boards by
project, a board's sprints, moving issues into a sprint.

JQL (`jql.py`, `search.py`): `AND`/`OR`/`NOT`, parentheses, `= != ~ !~ > >= < <= IN NOT IN IS IS NOT`, `EMPTY`,
quoted values with escapes, `ORDER BY` several fields, `currentUser()`, `now()`, `startOfDay()`, `endOfDay()`,
`startOfWeek()`, `startOfMonth()`, `openSprints()`, `closedSprints()`, relative dates (`-7d`, `2w`, `-4h`),
`resolution = Unresolved`, custom fields as `cf[n]` or by name. Anything it cannot read, and a query with no
restriction, is a 400.

Workflows are per project: statuses with a category (`new`, `indeterminate`, `done`) and transitions with ids,
sources and screens. Entering a done status sets `resolution` (the transition's screen value, else Done, or
Won't Do for a status declared cancelled) and `resolutiondate`; leaving it clears both. `TicketSnapshot.state`
is each status's declared outcome: by default `done` for a done category and `open` otherwise.

**Link direction.** Atlassian's reference for `POST /rest/api/3/issueLink`
(https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-links/) calls the outward issue the
link's "from" issue: the permission to link and the optional comment are on the project and issue that
`outwardIssue` names. So `{type: Blocks, outwardIssue: A, inwardIssue: B}` records "A blocks B", and a client
that puts the blocking issue on `outwardIssue` is right. On a read, an issue's `issuelinks` name the other end:
A's entry carries `outwardIssue: B` (label it with the type's `outward`, "blocks"), B's carries `inwardIssue: A`
("is blocked by"), as the issue linking model page shows
(https://developer.atlassian.com/cloud/jira/platform/issue-linking-model/). `GET /issueLink/{id}` names the ends
as the create does. Read from the documentation; not verified against a live site.

Permissions are project roles (Administrators, Member, Viewer): no role, and an issue or project is a 404; a
viewer who writes gets a 403. Site administration does not grant reading a project's issues.

Seeding: `seed.JiraSeed`, the scenario's `provider_seeds` entry for `jira`, naming a seeded ticket by its `key`
(`SeededIssue.ticket`, `SeededLink.to`, `parent`). The ticket's own labels and comments are the issue's
(`Manifest.ticket_fields` holds key, labels and comments); the seed adds comments by the agent or other accounts,
or at a moment of their own. Faults declared there (`rate_limits`), or on an open standing world through
`DeclaresFaults`: a rate limit on a method and path prefix answers 429 with `Retry-After` the given number of
times. World keys: `<site>.atlassian.net` and `/ex/jira/{cloudId}/`.

A person's acts are `TicketHappening`s through `ActsOnTickets.act`: `Moves` (the fewest transitions with no
required screen), `Reassigns` (to someone or nobody), `Comments`, `Deletes`, each by the happening's person at the
run clock's time, recorded as actor PERSON with that person as the changelog's or comment's author.

## While a world is open (`minutehand serve`)

- A person deletes an issue (`OpenWorld.delete_ticket`, or a `TicketFate` with `deleted: true`): the issue, its
  subtasks and the links naming them go, as actor PERSON, and the API answers 404 for it.
- An account is deactivated or reactivated (`OpenWorld.deactivate_person` / `reactivate_person`): it reads
  `active: false`, its credentials sign in to nothing, and it is not assignable (400). Removing an account is
  refused as unsupported: Jira Cloud deactivates, it does not delete.

## What it does not do

- API v2 (`/rest/api/2`), the old `/rest/api/3/search`, bulk create, webhooks, watchers, votes, worklogs,
  attachments, components and versions (empty lists), issue security, groups, filters, dashboards.
- Writing `timetracking`, ranking, epics' own endpoints, sprint create or edit, board configuration.
- The authorization-code grant (no browser flow: tokens are seeded), scopes (every token holds every scope),
  Connect and Forge apps, personal access tokens.
- JQL `WAS`/`CHANGED`, history functions, `membersOf()`, saved filters, and Lucene stemming in `text ~`: words
  match whole (or as a prefix with `*`), a quoted phrase matches in order.
- Workflow conditions, validators and post functions beyond setting the resolution; a screen's field rules
  beyond required and on-screen.
- Rate limits other than those a seed declares.
