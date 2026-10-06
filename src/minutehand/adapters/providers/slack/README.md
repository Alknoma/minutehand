# Slack provider

Written from Slack's public Web API, Events API and interactivity reference, by reading it. No vendor code or example
payload was copied; every body and error text here is this provider's own.

## Scope

It answers what a Slack app needs: a bot that reads and writes conversations, is told what people do by the Events
API, and is used through buttons, person pickers, modals and slash commands.

Hosts: `slack.com` and `*.slack.com` (the Web API at `/api/<method>`, `files.slack.com` for `url_private`,
`hooks.slack.com` for a `response_url`).

| Call | Answered |
|---|---|
| `auth.test` | the workspace the token is for: its team id, name, bot user and bot id |
| `users.list`, `users.info`, `users.lookupByEmail` | the workspace's members; an absent person's status set (below) |
| `users.profile.get`, `users.getPresence`, `dnd.info` | a person's status, presence and do-not-disturb |
| `conversations.list/info/open/members/history/replies` | channels, IMs and group DMs of the workspace |
| `chat.postMessage/postEphemeral/update/delete`, `reactions.add` | as Slack keeps them |
| `views.open/update/publish` | modals opened with a press's `trigger_id`, the Home tab |
| `oauth.v2.access` | the install's code exchanged for a bot token of the workspace it is for |

Pushed to the app (`PushesEvents`, `PushesInteractions`), signed with the world's signing secret: messages in a DM,
a channel or a thread, `app_mention`, `message_changed`, `message_deleted`, `reaction_added`,
`member_joined_channel` (a person joining, the bot being added), `app_home_opened`, slash commands, `block_actions`
and `view_submission`. `credential` (`MintsInboundCredentials`) signs a request a test builds itself:
`X-Slack-Request-Timestamp` and `X-Slack-Signature` for a given body and timestamp.

Faults (`SlackSeed.faults`): `ratelimited` with `Retry-After`, or any of Slack's error codes, per method, counted,
from an offset, optionally only for calls carrying blocks.

## Workspaces

A world is one workspace, `T0WORKSPACE` with bot user `U0AGENTBOT`, that takes any `xoxb-` or `xoxp-` token as its
bot, unless its seed says otherwise:

```json
{"workspaces": [
  {"team_id": "TACME", "bot_user_id": "UACMEBOT", "bot_id": "BACME", "domain": "acme", "tokens": ["xoxb-acme"]},
  {"team_id": "TBETA", "bot_user_id": "UBETABOT", "bot_id": "BBETA", "domain": "beta", "tokens": ["xoxb-beta"],
   "members": ["sofia"], "oauth_code": "code-beta"}
]}
```

- A workspace that lists `tokens` takes only those and the tokens its own install minted; the token decides which
  workspace a call is answered in. One that lists none takes any token of Slack's shape. A token no workspace takes is
  `invalid_auth`.
- Two worlds with different team ids and tokens share nothing: each world's store is its own, and the proxy routes
  each token to the world that claims it.
- Within one world, each workspace has its own members (`members`, every person when left out), its own user ids for
  the same person, its own `#general`, IMs and channels. The default workspace's ids are those of earlier versions.
- A person's push comes from the first workspace they are a member of; a happening naming a channel comes from the
  first of their workspaces holding that channel with them in it; a reply, an edit, a delete or a press comes from the
  workspace of the conversation it is in. A seeded channel is in the first workspace holding all its members and
  authors.
- `oauth.v2.access` answers the workspace whose `oauth_code` the code is, else the first.

## People while the world is open

`ChangesPeople`: `deactivated` and `reactivated`. A deactivated account stays in `users.list` with `deleted: true` and
cannot be opened a DM with (`user_disabled`). Slack has no way to remove an account, so `removed` is refused as
unsupported.

An absence the scenario gives a person (`Person.absences`, anchored as the checks anchor it: at the start, or at the
agent's first message to them) shows while it lasts: `status_text` (its reason, else `Away`), `status_emoji`
`:palm_tree:`, `status_expiration` at its end; `users.getPresence` `away`; `dnd.info` snoozed until its end.

## What it does not do

- Enterprise Grid: a person in two workspaces is two unrelated users, and no shared channel spans workspaces.
- `X-Slack-Request-Timestamp` on a push is the machine's time, as an app's verifier needs; `ts` and `event_time` are
  simulated.
- Opening the Home tab writes only a read, so `act` with `opens_agent` is pushed and then answered 409 by the control
  API ("recorded nothing").
- No user tokens of their own: an `xoxp-` token acts as the bot.
- No `chat.scheduleMessage`, `conversations.create`, `files.upload`, `users.setPresence`, `dnd.setSnooze`.
