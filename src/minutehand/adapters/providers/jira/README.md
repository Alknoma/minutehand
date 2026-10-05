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

Permissions are project roles (Administrators, Member, Viewer): no role, and an issue or project is a 404; a
viewer who writes gets a 403. Site administration does not grant reading a project's issues.

Seeding: `seed.JiraSeed`, the scenario's `provider_seeds` entry for `jira`. Faults declared there: a rate limit
on a method and path prefix answers 429 with `Retry-After` the given number of times.

A person's acts (`provider.py`): `moves` (the fewest transitions with no required screen), `reassigns`,
`comments`, `deletes`, each by a named person at the run clock's time, recorded as actor PERSON with that person
as the changelog's or comment's author.

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
