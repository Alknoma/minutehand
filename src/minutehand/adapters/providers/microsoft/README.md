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
  expire on the run's clock. A drive's root is notified of every change to an item in it, whether the agent or a
  person made it; a chat's or channel's messages of every new message.
- **Faults** a scenario declares for `microsoft` are answered in front of the surface the `call` names (`"METHOD
  /path prefix"` or `"/path prefix"`), in that surface's error shape, with `Retry-After` for a rate limit.

## People

Through the shared ports: `say`, `deliver`, `happen` (posts, edits, deletes, reactions, joins, commands as a
message), `press` (a card button: `invoke` `adaptiveCard/action` for `Action.Execute`, a `message` with `value`
for `Action.Submit`; the card's inputs filled from `Press.form`, a person picked into its first choice set).

This provider's own, until a shared port exists: `install(person, conversation, target, world, clock)`,
`edit_file`, `rename_file(…, name=, folder=)`, `delete_file`, `share_file(person, item, with_email, roles, …)`,
`hold_file(…, held=)`.

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
