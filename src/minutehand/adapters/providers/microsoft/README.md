# Microsoft provider

Written from Microsoft's public documentation of the identity platform, the Bot Framework connector and Microsoft
Graph `v1.0`, by reading it. No vendor code or vendor example payload is in this directory: every response body,
error message and example here was written for this provider.

One provider (`microsoft`), because `graph.microsoft.com` serves Teams and files alike and a host belongs to one
manifest, and because the tenant, its bot and its users are one directory every surface signs in against.

| Host | Module | What it answers |
|---|---|---|
| `login.microsoftonline.com` | `signin.py` | Token endpoint (`client_credentials`, `authorization_code`, `refresh_token`), `authorize` with no browser, OpenID metadata, key set |
| `login.botframework.com` | `signin.py` | The Bot Framework's OpenID metadata and key set (endorsed `msteams`) |
| `smba.trafficmanager.net` | `connector.py` | Send, reply, update, delete, create a 1:1 conversation, members, paged members, one member, team details, team channels |
| `graph.microsoft.com` | `graph_teams.py`, `graph_files.py`, `subscriptions.py` | Users, `/me`, teams, channels, chats, their messages and members; sites, drives, items by id and path, children, content, upload (simple and session), folders, move, rename, copy, delete, invite, sharing links, permissions, search, `delta`; subscriptions |
| `*.sharepoint.com` | `app.py` | The pre-authenticated download, upload-session and copy-monitor URLs Graph hands out |

## How it behaves

- **Tokens are real RS256 JWTs**, signed with one key derived from a fixed seed (`keys.py`) and published by both
  metadata documents. Graph and the connector read the tenant, app and user from the bearer token. A pushed
  activity carries a token issued by `https://api.botframework.com` for the bot's app id with the `serviceurl`
  claim; a bot that validates against the published metadata accepts it.
- **Token lifetimes are real time** (`iat`, `nbf`, `exp`), because the caller checks them on its own clock.
- **Ids are derived from the scenario's name** (`state.directory_of`): tenant id, domain, SharePoint host, the bot's
  app id and secret, the team and its General channel. A service is configured with them before the run.
- **A message is the connector's activity**; Graph reads it in its `chatMessage` shape. An Adaptive Card is kept
  as the JSON the bot sent; `cards.py` lists its text, inputs and buttons, and `MessageSnapshot.actions` carries the
  buttons.
- **A channel post or group-chat message reaches the bot only when it @-mentions it**, as in Teams. A person's
  reply in a channel is threaded and mentions the bot.
- **`GET …/content` answers 302** to a URL on the site's host carrying `tempauth`.
- **A seeded document is a real `.docx`** (`docx.py`), so a reader such as `python-docx` opens it; search reads its
  paragraphs.
- **Subscriptions** are validated by calling `notificationUrl` with `validationToken` and requiring it echoed, and
  expire on the run's clock. A drive's root is notified of every change to an item in it: an agent's change as
  its call is answered, a person's when the run tells watchers (`NotifiesChanges.notify`), through the same
  `subscriptions.notify`; a chat's or channel's messages of every new message.
- **Faults** are `MicrosoftSeed.faults` in the scenario's Microsoft `ProviderSeed`, answered in front of the surface
  the `call` names (`"METHOD /path prefix"` or `"/path prefix"`), in that surface's error shape, with `Retry-After`
  for a rate limit. **A held file** is `MicrosoftSeed.holds` (a seeded document, the person holding it, from an
  offset, for a while or for good): every write to it is refused 423 `resourceLocked` in that window. Both can be
  declared on an open standing world (`DeclaresFaults`). A fault answered `{"kind": "without_id"}` lets a connector
  send through and answers it 201 without its `id` (nor a new conversation's `activityId`). Failing the next sends
  is `{"call": "POST /teams/v3/conversations", "answer": {"kind": "refused", "error": "generalException"}}`; failing
  the next Graph reads is `{"call": "GET /v1.0/teams", "answer": {"kind": "refused", "error":
  "InvalidAuthenticationToken"}}`.
- **Documents** say who created them (`DocumentSnapshot.owner`: a person's email or the app's name) and, in a team's
  library, the site they are in (`space`; None in a person's OneDrive). Access given (`invite`, `createLink`, a
  person's `Shared`) is a `GrantSnapshot`: Graph's `read`, `write`, `owner` as reader, writer, organizer; a link's
  `to` is `anyone` or its scope.
- **World keys** (`Manifest.world_keys`): the tenant in a sign-in path and the label of a SharePoint host, so
  `minutehand serve` routes a sign-in, its metadata and a pre-authenticated download to the tenant's world.

## People

Through the shared ports: `say`, `deliver`, `happen` (posts, edits, deletes, reactions, joins, commands as a
message), `press` (a card button: `invoke` `adaptiveCard/action` for `Action.Execute`, a `message` with `value`
for `Action.Submit`; the card's inputs filled from `Press.form`, a person picked into its first choice set).

A person installing the bot is `PersonAddsAgent` through `happen`: in a named channel or chat, or with none in their
own chat, which `MicrosoftSeed.not_installed_for` leaves without the bot until then; Teams sends
`installationUpdate` then `conversationUpdate`. A person's change to a seeded document is `ChangesDocuments.change`:
`Edited` appends a paragraph, `Renamed` keeps the extension, `Moved` makes the folder path, `Shared` grants Graph's
role (a commenter reads), `Trashed` deletes. `Commented` and `FieldSet` are refused at load: Graph `v1.0` has no
file comments and a file is no record.

An administrator changes a person's account (`ChangesPeople`, `POST /v1/worlds/{id}/people`): `deactivated` sets
`accountEnabled: false` (sign-in refused `AADSTS50057`, and their 1:1 chat refuses the bot 403
`BotNotInConversationRoster`), `reactivated` sets it back, `removed` deletes the user (Graph 404
`Request_ResourceNotFound`, sign-in 50034).

A person's `Absence` is what Graph shows of them: `GET /users/{id}/presence`, `/communications/presences/{id}` and
`POST /communications/getPresencesByUserId` answer `Away`/`OutOfOffice` with the reason as the out-of-office message
while it lasts, `Available` otherwise; `GET /users/{id}/mailboxSettings[/automaticRepliesSetting]` is `scheduled`
over the stretch that lasts now or the next one known, `disabled` when none is. An absence from the first ask is
known once the store holds the agent's first message to them, in any provider. Teams posts no automatic reply into
a chat, so none is pushed.

A test that posts an activity to the bot itself gets the Bot Framework's token for it
(`MintsInboundCredentials`, `POST /v1/worlds/{id}/inbound-credential` with `service_url` and `audience`), signed as
every pushed activity is.

## What it does not do

- No `$batch`, `$orderby` beyond a folder's `name`, `lastModifiedDateTime` and `size`, `$search`, `$count`, or
  `ConsistencyLevel`; each unknown query option is refused 400.
- Graph does not send messages (`POST …/messages` is refused 403); a bot sends through the connector.
- No lifecycle notifications (`reauthorizationRequired`, `missed`), no encrypted resource data, no rich
  notifications.
- Reactions are recorded and pushed but not listed on Graph's `chatMessage.reactions`; a deleted message is gone
  from Graph's lists rather than listed with `deletedDateTime`.
- Converting a file (`?format=pdf`), thumbnails, versions history, Excel and PowerPoint text, and check-in/out are
  not served. Content is held in the store, a whole file per version.
- A delta token expires after 30 days of the run's clock; Graph publishes no fixed lifetime.
- Sign-in has no browser: `authorize` answers at once for the user named in `login_hint`. No certificate
  credentials (`client_assertion`), no on-behalf-of, no device code.
- Opening the bot's app in Teams pushes nothing, so `PersonOpensAgent` is refused.
- The Bot Framework's OpenID metadata and keys (`login.botframework.com`) carry no tenant and no credential, so under
  `minutehand serve` a bot fetching them is routed only by a world claiming that host (one at a time) or the default
  world.
