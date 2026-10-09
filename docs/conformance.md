# The conformance suite

`tests/conformance/` holds every provider to the questions that should have ONE answer across providers: what a
seed means, what a lifecycle leaves in the record, who did what and when, which ids hold still, what the record
keeps, where state lives, whose clock it is, what a fault looks like, and how a listing pages. Each provider was
built and tested by its own author; this suite is the shared one, written from outside.

It tests only through what an outside consumer has: each vendor's public API (with httpx or the vendor's own client
library, through the proxy and its CA), the control API of `minutehand serve`, and the world's record (`events`,
`entities`, `calls`, `unmatched`). It starts `minutehand serve` as its own process (the installed command, free
loopback ports, its state in a temporary directory) and reaches it through the shipped pytest plugin
(`minutehand.testing`), whose `minutehand` fixture reads `MINUTEHAND_URL`. Nothing in `src/` is imported but the
public models (`adapters/control/wire.py`, `domain/scenario.py`, `domain/world.py`), the client and the manifests
that say which providers are installed.

It is marked `conformance` and runs in the default test run (`uv run pytest -q -n auto`). One case is one
(provider, property, case); a property applies to a provider by the family its manifest puts it in.

## What it guarantees

| # | Property | The rule each case states |
|---|---|---|
| 1 | Seeded is held or refused | Every field of the shared seed models, seeded with a distinctive value, is read back through the vendor's API (and the neutral view, where it has a field for it), or creating the world is refused naming the field and the provider. Never accepted and absent. |
| 2 | Family equivalence | The same lifecycle driven through each provider of a family leaves the neutral record the neutral model states, and the same whole neutral sequence as the family's first provider. |
| 3 | What people do, everywhere | Every happening and control-API act of a family works on each provider of it — seen through the vendor's API as that person's, recorded as theirs at the world's clock — or is refused at the call naming the provider. So are account changes and permissions. |
| 4 | Any account can act | A member, an account with no email, one known by a vendor login, a bot: each signs in when it has a credential, is listed, is assigned / messaged / shared with, acts through the control API, is deactivated — or that operation is refused with its reason; never "no such person". Any credential, none and one nobody seeded included, is accepted and acts as the seed's default identity (the account the world's own credential acts as). |
| 5 | Ids are stable | Every seeded thing's vendor id is the same after a live addition and in a second world whose seed lists one more of each kind first; ids match the vendor's documented format; a seed that may declare an id gets exactly it. |
| 6 | The record is complete and verbatim | Every request is in the world's calls once, in order, byte-identical both ways (binary too), with no credential kept. |
| 7 | State lives only in the record | Reset returns the seeded answers; a world reopened in a used server answers as in a fresh one; two worlds changed at once never see each other. |
| 8 | Time is the world's | What is created after the clock is set to an unusual moment carries that moment in the vendor's format; advancing moves new timestamps and nothing else. |
| 9 | Faults | Every fault kind a provider declares can be declared on a live world, answers the vendor's documented status and shape through its real client library (its typed error), is consumed or expires, and is recorded; an armed fault answers once and is recorded. |
| 10 | Listing is consistent | Every list paged at the vendor's smallest page over more than a page returns each item once, in a stable order, the same set as unpaged. |

`test_p00_every_provider_has_a_driver.py` fails a provider with no driver, a driver that does not drive a family its
manifest puts the provider in, a vendor exception with no known capability or no reason, and a missing id format.

What the neutral model cannot witness, the vendor's API alone is the witness for: people have no neutral snapshot;
labels, comments, channel settings and files have no neutral field; and a `TicketSnapshot`'s event says a PERSON
changed it but not which one, so "by that person" is read from the vendor (a comment's author, an activity).

## Adding a driver for a new provider

A new provider directory is picked up with no registration: its manifest says its families (`kinds`: `ticket` →
tickets, `message` → messaging, `document` → documents; every provider is in accounts), and every case of every
property that applies is generated for it. Until `tests/conformance/drivers/<key>.py` exists, each of those cases
fails, saying which file to add.

The driver defines `DRIVER`, an instance of a `Driver` subclass (`tests/conformance/contract.py`):

- `provider`, and `session`: the `Session` class it returns, subclassing `Tickets`, `Messaging` and/or `Documents`
  for each family.
- `world(seed, tag, logins=None)`: the `CreateWorld` to open — the seed plus the provider's own seed and claims
  unique to `tag`, the same for the same tag; a credential for every person where the vendor has them.
- `connect(api, world, person=None)`: a session as the agent or a person. Every request goes through `api.http`,
  which keeps what was sent and answered for property 6.
- `id_formats` (a documented regular expression per kind, its source cited beside it), `page_floor`,
  `vendor_login`, and where the provider has them `faults()`, `declared_ids()`, `permission()`,
  `signed_in()`.
- `absent`: capability → the vendor's reason it has no such thing. Names come from `CAPABILITIES`; each declared
  exception is reported at the end of the run under "declared vendor exceptions", never silently skipped.

A driver performs each operation the way a real client does and raises when the vendor refuses (`ok`,
`VendorRefused`). It never works around the fake: a wrong answer is a result.

## The known-failures table

`tests/conformance/known_failures.py` lists every (provider, property, case) that fails on today's trunk with the
failure as observed, in one line. A listed case is marked `xfail(strict=True)`: while it fails the run is green;
the moment it passes, the run fails until its entry is removed, so every fix is recorded where the next reader sees
it. An unlisted failure fails the run. The table may only shrink: a property is never weakened to make a provider
pass, and a vendor that genuinely differs is a declared, reasoned exception in its driver instead.

## The matrix

Generated from a run of `test/conformance` with `integration-main` merged (`eb0663d`) on 2026-10-07. Each cell is
**pass / known failure / not applicable**, counted in cases; — means the property does not apply to the provider's families. Every known failure is one line of
`tests/conformance/known_failures.py`, which says what was observed.

| Provider | 1 Seeded | 2 Family | 3 People act | 4 Accounts | 5 Ids | 6 Record | 7 State | 8 Time | 9 Faults | 10 Listing |
|---|---|---|---|---|---|---|---|---|---|---|
| asana | 8 / 3 / 3 | 0 / 1 / 0 | 9 / 6 / 0 | 1 / 0 / 3 | 1 / 1 / 1 | 1 / 1 / 0 | 3 / 0 / 0 | 1 / 0 / 0 | 2 / 0 / 0 | 2 / 0 / 0 |
| aws | 0 / 1 / 6 | — | 1 / 0 / 3 | 0 / 0 / 4 | 0 / 0 / 3 | 1 / 1 / 0 | 2 / 0 / 1 | 0 / 1 / 0 | 1 / 0 / 0 | 0 / 0 / 1 |
| github | 0 / 5 / 2 | — | 4 / 0 / 0 | 3 / 1 / 0 | 0 / 2 / 1 | 0 / 2 / 0 | 3 / 0 / 0 | 1 / 0 / 0 | 4 / 0 / 0 | 0 / 1 / 0 |
| google_cloud_tasks | 1 / 0 / 6 | — | 1 / 0 / 3 | 0 / 0 / 4 | 0 / 0 / 3 | 0 / 1 / 1 | 3 / 0 / 0 | 1 / 0 / 0 | 1 / 0 / 0 | 0 / 0 / 1 |
| google_workspace | 8 / 2 / 16 | 0 / 1 / 2 | 11 / 1 / 12 | 0 / 0 / 4 | 0 / 1 / 2 | 1 / 2 / 0 | 3 / 0 / 0 | 1 / 0 / 0 | 11 / 0 / 0 | 1 / 0 / 2 |
| jira | 7 / 5 / 2 | 1 / 1 / 0 | 15 / 0 / 0 | 2 / 1 / 1 | 2 / 1 / 0 | 1 / 1 / 0 | 3 / 0 / 0 | 1 / 0 / 0 | 2 / 0 / 0 | 2 / 0 / 0 |
| microsoft | 9 / 15 / 2 | 0 / 4 / 0 | 19 / 4 / 1 | 1 / 2 / 1 | 1 / 1 / 1 | 1 / 2 / 0 | 3 / 0 / 0 | 1 / 0 / 0 | 6 / 0 / 0 | 3 / 0 / 0 |
| notion | 2 / 5 / 9 | 0 / 2 / 0 | 9 / 1 / 1 | 0 / 0 / 4 | 1 / 1 / 1 | 1 / 2 / 0 | 3 / 0 / 0 | 0 / 1 / 0 | 3 / 0 / 0 | 2 / 0 / 0 |
| slack | 15 / 2 / 0 | 2 / 2 / 0 | 17 / 0 / 0 | 0 / 3 / 1 | 3 / 0 / 0 | 1 / 1 / 0 | 3 / 0 / 0 | 1 / 0 / 0 | 5 / 0 / 0 | 2 / 0 / 0 |
| youtrack | 7 / 4 / 3 | 1 / 1 / 0 | 15 / 0 / 0 | 1 / 2 / 1 | 2 / 1 / 0 | 1 / 1 / 0 | 3 / 0 / 0 | 1 / 0 / 0 | 3 / 0 / 0 | 2 / 0 / 0 |
| **all** | **57 / 42 / 49** | **4 / 12 / 2** | **101 / 12 / 20** | **8 / 9 / 23** | **10 / 8 / 12** | **8 / 14 / 1** | **26 / 3 / 1** | **7 / 3 / 0** | **38 / 0 / 0** | **14 / 1 / 4** |

### Not applicable, and why

Each line is a capability a driver declares the vendor has no such thing, with the vendor's reason; every case needing it
holds vacuously for that provider and is reported at the end of the run. A provider that merely does not read a field (a seeded `SignIn` it ignores) is never one of these: that is
a known failure.

- asana accounts.bot: Asana's user resource has no field saying an account is a bot or an app; integrations act as ordinary users or service accounts the API does not mark.
- asana accounts.no_email: Every Asana account signs in by its email address, so no Asana user exists without one.
- asana accounts.title: An Asana user resource carries a gid, name, email, photo and workspaces; it has no job title.
- asana accounts.vendor_login: Asana has no login apart from the account's email address; there is no username.
- asana: its docs let a seed declare no id
- aws accounts.people: the scenario's people are no AWS principals: the aws provider seeds no IAM user, role or access key for a person (AwsProvider.seed declares no resources), so AWS has no account of theirs to list or act as
- aws listing.people: the scenario's people are no AWS principals: the aws provider seeds no IAM user, role or access key for a person (AwsProvider.seed declares no resources), so AWS has no account of theirs to list or act as
- aws state.concurrent: SigV4 is no OAuth credential: a world claims the host, or is the default, one at a time (docs/serve.md), and moto answers only the regions it knows, so no two worlds can claim hosts of their own
- aws: its docs let a seed declare no id
- aws: nothing of the shared seed is its to show
- github accounts.deactivated: GitHub.com keeps no deactivated account an organization still lists: a removed member drops out of the members listing, and suspending a user exists only on GitHub Enterprise Server.
- github accounts.title: A GitHub account has a name, company, location and bio, and no job title anywhere in the REST or GraphQL user objects.
- github: its docs let a seed declare no id
- google_cloud_tasks accounts.people: the scenario's people are no Cloud Tasks principals: Cloud Tasks has queues and tasks and no accounts, and who may call it is IAM's business (https://cloud.google.com/tasks/docs/reference-access-control), so it has no account of theirs to list or act as
- google_cloud_tasks accounts.whoami: the Cloud Tasks API has no call that answers who the caller is: its resources are locations, queues and tasks (https://cloud.google.com/tasks/docs/reference/rest); reading a token's identity is Google's token endpoint's business, which this provider does not serve
- google_cloud_tasks listing.people: the scenario's people are no Cloud Tasks principals: Cloud Tasks has queues and tasks and no accounts, and who may call it is IAM's business (https://cloud.google.com/tasks/docs/reference-access-control), so it has no account of theirs to list or act as
- google_cloud_tasks: its docs let a seed declare no id
- google_cloud_tasks: nothing of the shared seed is its to show
- google_workspace accounts.people: Drive v3 lists no accounts: it knows users only as a file's owners, last modifier and permissions, and as `about.get`'s own user (https://developers.google.com/workspace/drive/api/reference/rest/v3/about), and Gmail and Calendar know people only as addresses; listing a domain's accounts is the Admin SDK Directory API's `users.list`, a separate product for Workspace domains
- google_workspace listing.people: Drive v3 has no account listing to page (see accounts.people); the Admin SDK Directory API's users.list is a separate product
- google_workspace messaging.channels: Gmail has no channels: mail is exchanged between addresses and grouped in threads, and a mailbox's labels hold no members (https://developers.google.com/workspace/gmail/api/guides/threads); the provider refuses a seeded channel, naming itself
- google_workspace messaging.edit: a sent email cannot be changed: Gmail's messages resource offers send, insert, import, modify (labels only), trash and delete, and no edit (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)
- google_workspace: its docs let a seed declare no id
- jira accounts.guest: A Jira Cloud user says nothing of being a guest: its accountType is atlassian, app or customer, and no field of the REST v3 user marks an account as invited from outside.
- jira accounts.title: A Jira Cloud user object carries no job title: the REST v3 user has an accountId, a display name, an email, an account type, avatars, a time zone and whether it is active, nothing more.
- jira accounts.vendor_login: Jira Cloud accounts have no username since Atlassian's 2019 privacy change: an account is named only by its accountId, its display name and its email.
- microsoft accounts.bot: Graph lists users at /users and an app or bot is a service principal, never a user, so no account the listing answers is a bot (https://learn.microsoft.com/en-us/graph/api/resources/user)
- microsoft documents.comments: Microsoft Graph v1.0 has no API for a file's comments: a driveItem has no comments relationship (https://learn.microsoft.com/en-us/graph/api/resources/driveitem), as the provider's README says
- microsoft messaging.topic: a Teams channel has a displayName and a description and no topic; only a group chat has a topic (https://learn.microsoft.com/en-us/graph/api/resources/channel)
- microsoft: its docs let a seed declare no id
- notion accounts.deactivated: Notion's user object has no active or deactivated state; a member who leaves is no longer listed (https://developers.notion.com/reference/user)
- notion accounts.guest: Notion's user object does not say whether a person is a guest, and its user listing leaves guests out (https://developers.notion.com/reference/get-users)
- notion accounts.no_email: every Notion person signs in with an email address; only a bot user has none, and a bot is not a person (https://developers.notion.com/reference/user)
- notion accounts.person_credentials: Notion issues credentials only to integrations: every API call acts as the integration's bot user, never as a person (https://developers.notion.com/docs/authorization)
- notion accounts.title: Notion's user object has an id, a name, an avatar and a person's email, and no job title (https://developers.notion.com/reference/user)
- notion accounts.vendor_login: a Notion user has no login or username of its own, only a name and an email (https://developers.notion.com/reference/user)
- notion documents.file: an uploaded file in Notion is a file block inside a page, never a document of its own
- notion documents.presentation: Notion has no presentation document; every document is a page of blocks
- notion documents.sharing: Notion's public API has no endpoint that shares a page with a person or reads who it is shared with; sharing is done in the Notion app (https://developers.notion.com/reference/intro)
- notion documents.spaces: Notion's page object does not say which teamspace a page sits in, so the API cannot place a page in a shared space or read one back (https://developers.notion.com/reference/page)
- notion documents.spreadsheet: Notion has no spreadsheet document: tabular data is a database, which is not a page
- notion: its docs let a seed declare no id
- slack accounts.vendor_login: Slack retired usernames: the user object's `name` is documented as 'Don't use this. It once indicated the preferred username for a user, but that behavior has fundamentally changed since' (https://docs.slack.dev/reference/objects/user-object); an account is its member id and display name
- youtrack accounts.bot: YouTrack's User entity has no bot or app flag: an integration acts through a permanent token or a Hub service belonging to an ordinary user account.
- youtrack accounts.guest: YouTrack's only guest is the single anonymous 'guest' account unauthenticated visitors act as; nobody is invited as a guest from outside, so no person's account can be one.
- youtrack accounts.title: YouTrack's User entity has no job title: login, full name, email, avatar, banned, guest and online are all it answers.
