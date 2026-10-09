# Microsoft provider

Written from Microsoft's public documentation of the identity platform, the Bot Framework connector and Microsoft
Graph `v1.0`, by reading it. No vendor code or vendor example payload is in this directory: every response body,
error message and example here was written for this provider.

One provider (`microsoft`), because `graph.microsoft.com` serves Teams, files, mail and calendars alike and a host belongs to one
manifest, and because the tenant, its bot and its users are one directory every surface signs in against.

| Host | Module | What it answers |
|---|---|---|
| `login.microsoftonline.com` | `signin.py` | Token endpoint (`client_credentials`, `authorization_code`, `refresh_token`), `authorize` with no browser, OpenID metadata, key set |
| `login.botframework.com` | `signin.py` | The Bot Framework's OpenID metadata and key set (endorsed `msteams`) |
| `smba.trafficmanager.net` | `connector.py` | Send, reply, update, delete, create a 1:1 conversation, members, paged members, one member, team details, team channels |
| `graph.microsoft.com` | `graph_teams.py`, `graph_files.py`, `graph_mail.py`, `graph_calendar.py`, `subscriptions.py` | Users, `/me`, teams, `joinedTeams`, `primaryChannel`, channels, chats (made, one-on-one or group), their messages (posted, replied to, `delta`) and members; sites, drives, items by id and path, children, content, upload (simple and session), folders, move, rename, copy, delete, invite, sharing links, permissions, search, `delta`; `sendMail`, messages and mail folders listed, read, marked read, replied to (`reply`, `replyAll`), drafted (`POST /messages`, changed, `send`, `createReply`, `createReplyAll`, `createForward`), `forward`, `move`, `copy`, attachments (files, up to 3 MB), deleted, `delta`; events made (daily and weekly series too, with `instances`), read, listed, changed, deleted, cancelled and answered (`accept`, `tentativelyAccept`, `decline`), file attachments, `calendarView` and its `delta`, `getSchedule`; subscriptions |
| `*.sharepoint.com` | `app.py` | The pre-authenticated download, upload-session and copy-monitor URLs Graph hands out |

## How it behaves

- **Graph's surface is Microsoft's.** `surface.SERVED` lists the operations of Graph `v1.0`'s published OpenAPI
  description this provider answers (200 of the 2,103 in the subset kept under `tests/data/microsoft_graph_v1/`);
  every other call is refused by name, 501 `not_implemented`, before any surface reads it. So is any call whose
  answer Microsoft does not document (CLAIMS.md, "Refused by name"), and any property of a request body that would
  otherwise be dropped: what is sent is kept as sent, and Graph generates only what Graph assigns.
- **Minutehand does not enforce credentials.** Any client id and secret get a token, any tenant is the world's, a
  code or refresh token is read only for the user it names, and Graph, the connector and SharePoint's
  pre-authenticated URLs accept any token or none (a call naming nobody is the tenant's application; a connector
  call, the world's bot). No token carries `roles` and no scope is checked. What the world says still refuses: a
  disabled or removed user cannot sign in, a user's token reaches only their own mailbox, calendar and OneDrive, a
  bot only the conversations it is installed in. CLAIMS.md lists every check removed.
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
  `subscriptions.notify`; a chat's or channel's messages of every new message; a mailbox's messages (all, or one
  folder's: `/me/mailFolders('Inbox')/messages`) of every message put there, and its events of every change,
  each for at most 10,080 minutes.
- **Mail** (`graph_mail.py`): every user has a mailbox with Inbox, Sent Items, Drafts and Deleted Items, on `/me`
  and `/users/{id | userPrincipalName | mail}`, a folder named in any case or by id, as a segment or
  `mailFolders('Inbox')`. A sent message is one copy per mailbox: the sender's in Sent Items, read, and one unread
  in the Inbox of each recipient who is a user of the tenant, each with its own id and all with one
  `conversationId`. A new message starts a conversation; `reply` and `replyAll` stay in it. A reply goes to the
  message's `replyTo`, else its sender (from Sent Items, the mailbox itself); a reply to all also to every
  recipient; a comment with a body is 400 (message-reply, message-replyall). The sender's copy is what the run reads: a `MessageSnapshot`
  of subject and text, its recipients by their scenario email, its conversation as the channel, so an email is an
  ask of each person and a reply in the same conversation is a follow-up of the same ask. Lists take `$top` (10 by
  default), `$skip`, `$select`, `$filter` on `isRead`, the date properties, `from`/`sender` address,
  `conversationId`, `subject`, `id`, `importance`, joined by `and`, and `$orderby` on the
  dates and `subject`; with both, an `$orderby` that does not open the `$filter` is 400 `InefficientFilter`. A list
  without `$orderby` that would hold more than one message (or event) is 501: no Microsoft page or public
  recording states a default order (only `$search` results are documented sorted), so **send `$orderby`**, e.g.
  `receivedDateTime desc` for messages, `start/dateTime` for events and `calendarView`.
  `internetMessageId` is left out (Exchange assigns it from its own hosts).
  `Prefer: outlook.body-content-type="text"` answers bodies as text; otherwise a body is HTML; a body is stored as
  sent either way. A message and an event carry `changeKey` and `@odata.etag` W/"changeKey". A folder's
  `messages/delta` lists the folder, then what changed in it and, as `@removed`, what left it. A user's token
  reaches only their own mailbox (403 `ErrorAccessDenied`); an application's reaches all.
- **Calendars** (`graph_calendar.py`): `/events`, `/calendar/events`, `/calendarView` and `/calendar/calendarView`
  (`startDateTime` and `endDateTime` required), `/calendar`, `/calendars`, `/calendar/getSchedule`. An event is one
  item in its organizer's calendar, seen with the same id from each attendee's; `isOrganizer` and
  `responseStatus` are the reading mailbox's. Times are answered in UTC; a `timeZone` sent is UTC or an IANA name.
  An event made with attendees sends a meeting request (`eventMessageRequest`, `meetingMessageType:
  meetingRequest`) from the organizer carrying the event's body, in the event's own conversation, carrying Accept, Tentative and Decline as
  `MessageSnapshot.actions`: the agent asking each attendee. A change of subject, time or place clears every answer
  and sends a new request; a new attendee gets one of their own. `transactionId` makes a retried POST answer the
  event it already made. `getSchedule` answers each address's items and `availabilityView` (0 free, 1 tentative,
  an invitation not yet accepted; 2 busy; 3 out of office, a person's absence), a declined meeting not counted.
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

**By email and on invitations.** A reply to a message in a mailbox lands (`LandsReplies.land`): an email from the
person to whoever sent it, `RE:` its subject, in its conversation, landing in that mailbox's Inbox at its moment and
notified to a subscription on it; nothing is pushed to the bot, so an agent that talks to people only by email
declares no Microsoft inbound target. The landing wakes the agent when a live subscription watches a mailbox (or, for
an invitation's answer, a calendar) it lands in (`LandsReplies.heard`); otherwise the agent finds it on its next
poll, as a Gmail reply is found. A press (`Scripted` `press: {label: Accept}`, or
`Tentative`, `Decline`) on a meeting request sets the person's `responseStatus` on the event, recorded as their
press (`InteractionSnapshot`) and their change to the event, and sends the organizer `Accepted: …` (a response
message, `answerable: false`: it tells, it asks nothing) in the event's conversation. A press on a request a newer
one replaced, or whose event is gone, changes nothing. The seed (`MicrosoftSeed`) adds `mailbox`, a user of the
agent's own (`key`, `name`, `local` part at the tenant's domain) that seeded mail and events name by its key;
`emails` already sent (`by`, `to`, `cc`, `subject`, `text`, `ago`, `read`, `in_reply_to` an earlier one's `key`);
and `events` already planned (`organizer`, `attendees` with each one's `response`, `subject`, `at`, `lasts`,
`location`). A name that is no person and not the mailbox, an answer to no earlier email, or someone named twice
on one event is refused before anything of them is written.

**In Teams.** Through the shared ports: `say`, `deliver`, `happen` (posts, edits, deletes, reactions, joins, commands as a
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

- Every Graph operation outside `surface.SERVED` is 501 by name: among them `$batch`, sending chat or channel
  messages through Graph (a bot sends through the connector), drafts (`POST /messages`, `createReply`, `send`),
  `forward`, `move`, `copy`, attachments, child folders, event `delta`, `instances`, `cancel`, `forward`, other
  calendars, thumbnails, versions, check-in and check-out. `$orderby` beyond a folder's `name`,
  `lastModifiedDateTime` and `size` and what mail and events list above, `$search`, `$count`, `ConsistencyLevel`,
  an unread `$filter` clause or an `or` are 501 too.
- Mail has no categories or flags (a `PATCH` of anything but `isRead`, or of a draft's subject, body, recipients or importance, is 501), and `sendMail` always saves to Sent
  Items. A message deleted from Deleted Items is gone, and `delta` lists it as removed. A draft or a forward has no sender or subject Graph composes, and a message with attachments is not forwarded. A reply's `body` is
  left out (Graph composes it with the quoted original, and no source says how); the run records what was written.
  A tentative or declining answer to an invitation carries no subject (only "Accepted: …" is recorded). Deletions notify no subscription.
- Calendars have recurrence only as daily and weekly series (an occurrence is read, never changed), no online meetings, no `findMeetingTimes`, no working hours in `getSchedule`, no
  `Prefer: outlook.timezone` other than UTC and no Windows time zone names (501). An attendee's copy of an event
  is the organizer's one item, so an attendee cannot change or delete it (501); deleting an event sends no
  cancellation, `cancel` does.
- A person writes by email only in reply: `say` and messaging happenings are Teams, and need the agent to declare a
  Microsoft inbound target.
- No lifecycle notifications (`reauthorizationRequired`, `missed`), no encrypted resource data, no rich
  notifications.
- Reactions are recorded and pushed but not listed on Graph's `chatMessage.reactions`; a deleted message is gone
  from Graph's lists rather than listed with `deletedDateTime`.
- Converting a file (`?format=pdf`), thumbnails, versions history, Excel and PowerPoint text, and check-in/out are
  not served. Content is held in the store, a whole file per version.
- A delta token never expires with age (Graph publishes no lifetime); `resyncRequired` is a declarable fault.
- Sign-in has no browser: `authorize` answers at once for the user named in `login_hint`, and without one is 501.
  No certificate credentials (`client_assertion`), no on-behalf-of, no device code.
- Opening the bot's app in Teams pushes nothing, so `PersonOpensAgent` is refused.
- The Bot Framework's OpenID metadata and keys (`login.botframework.com`) carry no tenant and no credential, so under
  `minutehand serve` a bot fetching them is routed only by a world claiming that host (one at a time) or the default
  world.
