# Slack: where each behaviour comes from

Every behaviour below was asserted by the home-grown Slack emulator this provider replaced, by a test whose subject
was that emulator. Each is pinned here by a test that drives the fake with stock `slack_sdk` through the proxy
(`tests/providers/slack/test_slack_vendor_claims.py` unless named otherwise). **Documented** means Slack's public
reference says it, at the page given; **observed** means someone saw Slack do it and no page says so.

| Claim | Class | Test | Source |
|---|---|---|---|
| Opening a DM with a member answers an IM (`D…`) id | documented | `test_opening_a_dm_with_a_member_answers_an_im_id` | https://docs.slack.dev/reference/methods/conversations.open |
| An id in `users` that is no member is `user_not_found`, and no DM is made | documented | `test_opening_a_dm_with_an_id_that_is_no_member_is_refused_user_not_found` | https://docs.slack.dev/reference/methods/conversations.open |
| One unknown id in a group DM refuses the whole call | documented | `test_one_unknown_id_in_a_group_dm_refuses_the_whole_open_user_not_found` | https://docs.slack.dev/reference/methods/conversations.open |
| A token Slack never issued is `invalid_auth` (shape only, see below) | documented | `test_a_token_that_is_not_slacks_is_refused_invalid_auth` | https://docs.slack.dev/reference/methods/auth.test |
| Every member carries `tz` and `tz_offset` | documented | `test_every_member_is_served_with_a_timezone` | https://docs.slack.dev/reference/methods/users.info |
| An unknown channel id, or a channel's name, is `channel_not_found` and is never provisioned | documented | `test_a_channel_that_is_not_an_existing_id_is_refused_channel_not_found` | https://docs.slack.dev/reference/methods/conversations.info |
| A member id as `channel` posts into that member's DM with the app | documented | `test_a_member_id_as_the_channel_posts_into_the_dm_with_the_app` | https://docs.slack.dev/reference/methods/chat.postMessage |
| A member id nobody has as `channel` is `channel_not_found` | documented | `test_a_member_id_nobody_has_as_the_channel_is_refused_channel_not_found` | https://docs.slack.dev/reference/methods/chat.postMessage |
| Reading or posting where the app was never invited is `not_in_channel` | documented | `test_reading_or_posting_in_a_channel_without_the_app_is_refused_not_in_channel` | https://docs.slack.dev/reference/methods/conversations.history, https://docs.slack.dev/reference/methods/chat.postMessage |
| `is_member` is the caller's own membership | documented | `test_is_member_is_the_apps_membership_not_anyone_elses` | https://docs.slack.dev/reference/objects/conversation-object |
| Deleting a message nobody posted is `message_not_found` | documented | `test_deleting_a_message_nobody_posted_is_refused_message_not_found` | https://docs.slack.dev/reference/methods/chat.delete |
| Reacting to a message nobody posted is `message_not_found` | documented | `test_reacting_to_a_message_nobody_posted_is_refused_message_not_found` | https://docs.slack.dev/reference/methods/reactions.add |
| More than 50 blocks in a message is `invalid_blocks`; 50 is accepted | documented | `test_a_message_of_more_than_fifty_blocks_is_refused_invalid_blocks` | https://docs.slack.dev/reference/block-kit/blocks, https://docs.slack.dev/reference/methods/chat.postMessage |
| `as_user=1` from a bot token leaves the app as the author, with its `bot_id` | observed | `test_as_user_true_leaves_the_app_as_the_author` | — |
| An update with `blocks` draws those blocks; `text` is written as given | documented | `test_an_update_draws_the_blocks_it_is_given` | https://docs.slack.dev/reference/methods/chat.update |
| An update with `text` and no `blocks` drops the old blocks | documented | `test_an_update_with_text_and_no_blocks_drops_the_old_blocks` | https://docs.slack.dev/reference/methods/chat.update |
| `chat.postMessage` past 40,000 characters is posted and truncated, never refused | documented | `test_a_message_past_forty_thousand_characters_is_posted_and_truncated` | https://docs.slack.dev/reference/methods/chat.postMessage |
| `chat.update` with `text` past 4,000 characters is `msg_too_long`, and the message keeps its text | documented | `test_an_update_past_four_thousand_characters_is_refused_msg_too_long` | https://docs.slack.dev/reference/methods/chat.update |
| `missing_scope` and `token_revoked` reach the caller as Slack sends them, as declared faults (`SlackSeed.faults`, `Refused`) | documented | `test_slack_through_the_proxy.py::test_a_declared_refusal_reaches_the_sdk_as_slack_sends_it_once` (a declared fault; the fake keeps no scopes or revocations of its own) | https://docs.slack.dev/reference/methods/conversations.list, https://docs.slack.dev/reference/methods/auth.test |
| A thread parent in history carries `reply_count`, `reply_users`, `latest_reply` | documented | `test_a_thread_parent_carries_its_reply_summary` | https://docs.slack.dev/messaging/retrieving-messages |
| History holds the roots; thread replies come from `conversations.replies` | observed | `test_history_holds_thread_roots_and_replies_come_from_conversations_replies` | — |
| `inclusive` includes the message at `latest` | documented | `test_history_honours_inclusive_at_latest` | https://docs.slack.dev/reference/methods/conversations.history |
| `conversations.list` pages by cursor; `exclude_archived` leaves archived channels out | documented | `test_conversations_list_pages_by_cursor_and_excludes_archived_on_request` | https://docs.slack.dev/reference/methods/conversations.list, https://docs.slack.dev/apis/web-api/pagination |
| `users.list` pages by cursor | documented | `test_users_list_pages_by_cursor` | https://docs.slack.dev/reference/methods/users.list |
| `conversations.members` pages by cursor | documented | `test_conversations_members_pages_by_cursor` | https://docs.slack.dev/reference/methods/conversations.members |
| A modal title past 24 characters is refused | documented | `test_a_modal_title_past_twenty_four_characters_is_refused` | https://docs.slack.dev/reference/views/modal-views |
| A view of more than 100 blocks is refused `invalid_arguments` | documented | `test_a_view_of_more_than_a_hundred_blocks_is_refused_invalid_arguments` | https://docs.slack.dev/reference/block-kit/blocks, https://docs.slack.dev/reference/methods/views.publish |
| `ratelimited` is HTTP 429 with `Retry-After` | documented | `test_slack_through_the_proxy.py::test_a_rate_limit_answers_429_with_retry_after_and_then_passes` (a declared fault; the fake does not rate-limit on its own) | https://docs.slack.dev/apis/web-api/rate-limits |

## Where the old emulator and Slack's reference disagree

- **A view past 100 blocks.** The emulator answered `invalid_blocks`; `views.publish` documents no such code, and
  this fake answers `invalid_arguments`.
- **Text past 40,000 characters.** The emulator refused `msg_too_long` on `chat.postMessage`. Slack's reference for
  that method says such a message is truncated, and lists no `msg_too_long`; this fake keeps the first 40,000
  characters. `msg_too_long` is answered only where a method lists it: `chat.update` past 4,000 characters
  (https://docs.slack.dev/reference/methods/chat.update), and `chat.postEphemeral`, whose page lists the code but
  names no figure (https://docs.slack.dev/reference/methods/chat.postEphemeral) — there the 40,000 ceiling stands.
- **A member id as `channel`.** The `chat.postMessage` page says three things: that it opens the bot's 1:1 DM, that
  it lands in the person's App Home, and that it lands in their DM with Slackbot. This fake does the first.

## Not carried over

| Old emulator behaviour | Why |
|---|---|
| `missing_scope` naming `needed` and `provided` for a token without `groups:read` | Covered as a declared fault (row above); a declared `Refused` carries the code only, not `needed`/`provided`. No token carries scopes here. |
| `email` served only with `users:read.email` | Same: no token carries scopes here. |
| `token_revoked` for one fixed token string | A fixed credential is the emulator's own; the code is covered as a declared fault (row above). |
| `invalid_auth` for an `xoxb-` token nobody issued | Not yet default: only the token's shape is checked. Refusing a token the world does not know needs the scenario's `sign_ins` for `slack` written to the world at seed time (as `google_drive/seed.py` does) and checked in `_authenticate`; `seed.py` is outside this change. |
| A fixed CI token that lists private channels | A credential of the emulator's own. |
| `_post_as`, a body field that sets the author | The emulator's private dialect; a person's message here is a scenario happening. |
| `/health` | The emulator's own route. |
| The emulator's token holds exactly the install's scopes | A check on the emulator's fixture file. |
