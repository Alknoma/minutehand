# Jira Cloud provider

Written from Atlassian's public REST documentation for Jira Cloud (platform v3 and Agile 1.0) and for OAuth 2.0
(3LO) apps, by reading it. No vendor code, client library or example payload is copied; every response body and
error message here is this provider's own wording in Jira's shapes.

## What it serves

| Host | Paths |
|---|---|
| `<site>.atlassian.net` | `/rest/api/3/...`, `/rest/agile/1.0/...` |
| `api.atlassian.com` | `/ex/jira/<cloudId>/rest/...` (the same site); `/oauth/token/accessible-resources` |
| `auth.atlassian.com` | `POST /oauth/token`, `refresh_token`, `authorization_code` or `client_credentials`, JSON or form; a seeded grant's refresh token rotates |

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
`endOfWeek()`, `endOfMonth()`, `startOfYear()`, `endOfYear()`, `futureSprints()`, `resolution = Unresolved`,
custom fields as `cf[n]` or by name. A query that is not JQL, and one with no restriction, is a 400; JQL Atlassian
documents that this fake does not serve is a 501 naming it.

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

**Credentials and permissions are not enforced.** Any credential, or none, is let in: a seeded API token or access
token acts as its account, a Basic username that is an account's email as that account, and anything else as the
agent. Project roles (Administrators, Member, Viewer) say only who belongs to a project: with no role an issue or
project is a 404, and anyone in a project may do anything there. `CLAIMS.md` lists every check removed.

**Surface.** `tests/data/vendor_surface/jira.json` is the subset of Atlassian's OpenAPI descriptions for the
resources this fake claims (212 operations, fetched 2026-10-08): 45 are served, 167 are refused by name with a
501 (`surface.py`), as is every documented parameter or body property a served operation does not act on.
`CLAIMS.md` cites the source of every behaviour.

Seeding: `seed.JiraSeed`, the scenario's `provider_seeds` entry for `jira`, naming a seeded ticket by its `key`
(`SeededIssue.ticket`, `SeededLink.to`, `parent`). The ticket's own labels and comments are the issue's
(`Manifest.ticket_fields` holds key, labels and comments); the seed adds comments by the agent or other accounts,
or at a moment of their own. Faults declared there (`rate_limits`), or on an open standing world through
`DeclaresFaults`: a rate limit on a method and path prefix answers 429 with `Retry-After` the given number of
times. World keys: `<site>.atlassian.net` and `/ex/jira/{cloudId}/`.

A person's acts are transitions through `apply` (`ports.transitions`), a `TicketHappening` among them: `Moves` (the fewest transitions with no
required screen), `Reassigns` (to someone or nobody), `Comments`, `Deletes`, each by the happening's person at the
run clock's time, recorded as actor PERSON with that person as the changelog's or comment's author.

Where the scenario has the people engine play Jira (`transitions_on: [jira]`, `docs/design-transitions.md`), it is
`ProvidesTransitions`: an issue assigned to a person whose status is not in the Done category is pending on them;
the transitions offered are those `GET /issue/{key}/transitions` lists from its status, each taking a comment; and
the person's move goes through the same path as `POST /issue/{key}/transitions` (`Desk.transition`), its comment as
`update.comment`, authored by them. A transition whose screen requires a field is not offered to a person: the
engine writes free text only. The agent's own transitions and creates (the workflow's initial `Create`) and every
move a person makes are recorded as transitions beside the issue's versions. No webhook is served, so a person's
move never wakes the agent: it finds it on its next read.

## While a world is open (`minutehand serve`)

- A person deletes an issue (`OpenWorld.delete_ticket`, or a take of `delete`): the issue, its
  subtasks and the links naming them go, as actor PERSON, and the API answers 404 for it.
- An account is deactivated or reactivated (`OpenWorld.deactivate_person` / `reactivate_person`): it reads
  `active: false` and is not assignable (400); its credentials still act as it, since none is checked. Removing an account is
  refused as unsupported: Jira Cloud deactivates, it does not delete.

## What it does not do

- Everything `surface.UNSERVED` names (501), and API v2 (`/rest/api/2`), webhooks, issue security, groups,
  filters, dashboards (501, naming the path). Components and versions read as empty lists. The old
  `/rest/api/3/search` answers 410, as Jira does.
- Writing `timetracking`, ranking, epics' own endpoints, sprint create or edit, board configuration.
- The browser half of the authorization-code flow (any code is traded for tokens), scopes (every token holds
  every scope), Connect and Forge apps.
- JQL `WAS`/`CHANGED`, history functions, `membersOf()` and the other fields and functions `search.py` lists as
  unserved (501, naming them), saved filters, and Lucene stemming in `text ~`: words match whole (or as a prefix
  with `*`), a quoted phrase matches in order.
- Workflow conditions, validators and post functions beyond setting the resolution; a screen's field rules
  beyond required and on-screen.
- Rate limits other than those a seed declares.
