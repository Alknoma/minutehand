# Slack: where each behaviour comes from

Each row is pinned by a test that drives the fake with stock `slack_sdk` through the proxy
(`tests/providers/slack/test_slack_vendor_claims.py` unless named otherwise). **Documented** means Slack's public
reference says it, at the page given. **Observed** means Slack's own service was seen doing it, and the recording is
in `tests/providers/slack/data/observed/` (where and how it was taken: `tests/providers/slack/data/README.md`).
**Documented gap** means the page leaves the case open, so the fake refuses it 501 by name instead of choosing.
**World data** means the scenario says it. Nothing else is a source.

## Coverage

Every Web API method Slack lists is either served or refused 501 `not_implemented`, the method named
(`methods.UNSERVED`; `test_slack_method_coverage.py`). The list is the union of Slack's methods index, Slack's OpenAPI
file and the pinned `slack_sdk`'s `WebClient`: 393 methods, 23 served, 370 refused by name. A name none of them
lists is `unknown_method`, as Slack answers it.

| Claim | Class | Test | Source |
|---|---|---|---|
| A name Slack has no method by is HTTP 200 `{"ok":false,"error":"unknown_method","req_method":…}` | observed | `test_slack_method_coverage.py::test_a_name_slack_has_no_method_by_is_refused_unknown_method_as_slack_answers_it` | `data/observed/unknown_method.http` |
| An argument Slack documents for a served method and the fake does not model (`app.UNSERVED_ARGUMENTS`: `username`, `icon_*`, `link_names`, `parse`, `mrkdwn: false`, `metadata`, `markdown_text`, `as_user: true`, `unfurl_links: true`, `include_locale`, `prevent_creation`, `interactivity_pointer`, a refresh-token grant, …) is refused 501 naming it; at its default it is served | documented | `test_slack_fidelity.py::test_an_argument_the_fake_does_not_model_is_refused_naming_it`, `::test_an_unmodelled_argument_at_its_default_is_served` | each method's page under https://docs.slack.dev/reference/methods/ |

## Credentials

Authentication is out of scope (`docs/design.md`, "Authentication is out of scope"). Minutehand never refuses a call for its credential: any token, or none, is answered, and the token only picks which
of the world's workspaces answers (one that declares or minted it, else the first that declares none, else the
first). What a caller sees still follows world data: a channel the app is not in stays `not_in_channel` or
`channel_not_found` whatever the token
(`test_slack_fidelity.py::test_a_channel_the_app_is_not_in_is_refused_not_in_channel_whatever_the_token`). Each
check Slack makes and this stack does not test:

| Slack refuses | Slack's answer | Test that it passes here |
|---|---|---|
| A call with no token | `not_authed` (`data/observed/not_authed.http`) | `test_slack_fidelity.py::test_any_token_or_none_is_answered_as_the_apps_bot` |
| A token it never issued, of any shape | `invalid_auth` (`data/observed/invalid_auth.http`) | the same test |
| A token the scenario's declared Slack sign-in does not name | `invalid_auth` | `test_slack_fidelity.py::test_a_token_no_declared_sign_in_names_is_answered_too` |
| An app-level (`xapp-`) token on any method but `apps.connections.open`, and a bot token there | `not_allowed_token_type` (https://docs.slack.dev/reference/methods/apps.connections.open) | `test_slack_socket_mode.py::test_any_token_is_answered_on_apps_connections_open_and_an_app_level_token_on_any_method` |
| A Socket Mode URL whose ticket was used, or never issued | the WebSocket upgrade | `test_slack_socket_mode.py::test_a_socket_url_is_let_in_whatever_ticket_it_carries` |
| `oauth.v2.access` with no client id or secret, or a code already exchanged | `invalid_client_id`, `bad_client_secret`, `invalid_code` | `test_slack_through_the_proxy.py::test_any_install_code_is_exchanged_for_a_new_bot_token_every_time` |
| A file's `url_private` with no token | a redirect to the sign-in page | `test_slack_through_the_proxy.py::test_a_file_downloads_with_a_bot_token_and_without_one` |
| A `response_url` whose secret part is wrong | not found | none: the hook is found by its id alone |
| A token without the method's scope | `missing_scope` | none: no token carries scopes; a scenario can still declare the code as a fault (below) |

## Behaviour

| Claim | Class | Test | Source |
|---|---|---|---|
| Opening a DM with a member answers an IM (`D…`) id | documented | `test_opening_a_dm_with_a_member_answers_an_im_id` | https://docs.slack.dev/reference/methods/conversations.open |
| An id in `users` that is no member is `user_not_found`, and no DM is made | documented | `test_opening_a_dm_with_an_id_that_is_no_member_is_refused_user_not_found` | https://docs.slack.dev/reference/methods/conversations.open |
| One unknown id in a group DM refuses the whole call | documented | `test_one_unknown_id_in_a_group_dm_refuses_the_whole_open_user_not_found` | https://docs.slack.dev/reference/methods/conversations.open |
| Every member carries `tz` and `tz_offset` | documented | `test_every_member_is_served_with_a_timezone` | https://docs.slack.dev/reference/methods/users.info |
| An unknown channel id, or a channel's name, is `channel_not_found` and is never provisioned | documented | `test_a_channel_that_is_not_an_existing_id_is_refused_channel_not_found` | https://docs.slack.dev/reference/methods/conversations.info |
| A member id as `channel` posts into that member's DM with the app | documented | `test_a_member_id_as_the_channel_posts_into_the_dm_with_the_app` | https://docs.slack.dev/reference/methods/chat.postMessage |
| A member id nobody has as `channel` is `channel_not_found` | documented | `test_a_member_id_nobody_has_as_the_channel_is_refused_channel_not_found` | https://docs.slack.dev/reference/methods/chat.postMessage |
| Reading or posting where the app was never invited is `not_in_channel` | documented | `test_reading_or_posting_in_a_channel_without_the_app_is_refused_not_in_channel` | https://docs.slack.dev/reference/methods/conversations.history, https://docs.slack.dev/reference/methods/chat.postMessage |
| `is_member` is the caller's own membership | documented | `test_is_member_is_the_apps_membership_not_anyone_elses` | https://docs.slack.dev/reference/objects/conversation-object |
| `include_num_members` adds the channel's `num_members` | documented | `test_slack_fidelity.py::test_include_num_members_counts_the_channels_members` | https://docs.slack.dev/reference/methods/conversations.info |
| Deleting a message nobody posted is `message_not_found` | documented | `test_deleting_a_message_nobody_posted_is_refused_message_not_found` | https://docs.slack.dev/reference/methods/chat.delete |
| Reacting to a message nobody posted is `message_not_found` | documented | `test_reacting_to_a_message_nobody_posted_is_refused_message_not_found` | https://docs.slack.dev/reference/methods/reactions.add |
| More than 50 blocks in a message is `invalid_blocks`; 50 is accepted | documented | `test_a_message_of_more_than_fifty_blocks_is_refused_invalid_blocks` | https://docs.slack.dev/reference/block-kit/blocks, https://docs.slack.dev/reference/methods/chat.postMessage |
| A block without a `block_id`, and an interactive element without an `action_id`, is given one; one the agent gave is kept | documented | `test_slack_through_the_proxy.py::test_the_async_client_opens_a_dm_posts_a_card_and_reads_it_back_with_its_ids_and_profile`, `test_slack_fidelity.py::test_a_posted_message_reads_back_exactly_as_written` | https://docs.slack.dev/legacy/legacy-messaging/migrating-outmoded-message-compositions-to-blocks ("If you don't specify a `block_id`, one will be automatically generated"; the same for `action_id`) |
| `text`, `blocks` and `attachments` read back exactly as posted or updated; a reaction's `name` exactly as sent | world data | `test_slack_fidelity.py::test_a_posted_message_reads_back_exactly_as_written`, `::test_an_updated_message_reads_back_exactly_as_rewritten`, `::test_a_reaction_name_is_kept_as_sent` | what the agent sent; the arguments that ask Slack to rewrite text (`link_names`, `parse`) are refused by name |
| A view's fields (`private_metadata`, `callback_id`, `external_id`, `submit_disabled`, …) read back as published | world data | `test_slack_fidelity.py::test_a_home_tab_reads_back_every_field_it_was_published_with` | what the agent sent; fields: https://docs.slack.dev/reference/views/home-tab-views |
| `as_user=true` is refused by name: the page says it "Can only be used by classic apps" and lists `as_user_not_supported` "with workspace apps", and says nothing of a granular bot token | documented gap | `test_slack_fidelity.py::test_an_argument_the_fake_does_not_model_is_refused_naming_it` | https://docs.slack.dev/reference/methods/chat.postMessage |
| An update with `blocks` draws those blocks; `text` is written as given | documented | `test_an_update_draws_the_blocks_it_is_given` | https://docs.slack.dev/reference/methods/chat.update |
| An update with `text` and no `blocks` drops the old blocks | documented | `test_an_update_with_text_and_no_blocks_drops_the_old_blocks` | https://docs.slack.dev/reference/methods/chat.update |
| An updated message carries `edited` (`user`, `ts`) | documented | `test_slack_through_the_proxy.py::test_an_update_round_trips_its_blocks_and_an_empty_list_clears_them` | https://docs.slack.dev/reference/events/message/message_changed ("sent when a message … is edited using the chat.update API method", `edited` in its example) |
| `chat.postMessage` past 40,000 characters is posted and truncated, never refused | documented | `test_a_message_past_forty_thousand_characters_is_posted_and_truncated` | https://docs.slack.dev/reference/methods/chat.postMessage |
| `chat.update` with `text` past 4,000 characters is `msg_too_long`, and the message keeps its text | documented | `test_an_update_past_four_thousand_characters_is_refused_msg_too_long` | https://docs.slack.dev/reference/methods/chat.update |
| `missing_scope`, `token_revoked`, `invalid_auth` or any other code reach the caller as Slack sends them, as faults the scenario declares (`SlackSeed.faults`, `Refused`) | documented | `test_slack_through_the_proxy.py::test_a_declared_refusal_reaches_the_sdk_as_slack_sends_it_once` | https://docs.slack.dev/reference/methods/conversations.list, https://docs.slack.dev/reference/methods/auth.test |
| `ratelimited` is HTTP 429 with `Retry-After`, as a declared fault; the fake does not rate-limit on its own and sends no other rate-limit header | documented | `test_slack_through_the_proxy.py::test_a_rate_limit_answers_429_with_retry_after_and_then_passes` | https://docs.slack.dev/apis/web-api/rate-limits |
| A thread parent carries `reply_count`, `reply_users`, `reply_users_count`, `latest_reply` | documented | `test_a_thread_parent_carries_its_reply_summary` | https://docs.slack.dev/messaging/retrieving-messages#threading |
| A reply carries `parent_user_id`; `conversations.replies` answers the parent first, then the replies | documented | `test_slack_fidelity.py::test_a_reply_carries_its_parents_author_and_a_broadcast_reply_is_in_history` | https://docs.slack.dev/messaging/retrieving-messages#threading |
| History holds the roots; a thread's replies come from `conversations.replies` | documented | `test_history_holds_thread_roots_and_replies_come_from_conversations_replies` | https://docs.slack.dev/reference/methods/conversations.history ("To retrieve a message from a thread, check out `conversations.replies`") |
| `reply_broadcast` puts the reply in history too, as a `thread_broadcast` carrying its `root` | documented | `test_slack_fidelity.py::test_a_reply_carries_its_parents_author_and_a_broadcast_reply_is_in_history` | https://docs.slack.dev/reference/methods/chat.postMessage, https://docs.slack.dev/reference/events/message/thread_broadcast |
| A `thread_ts` naming a reply, or naming no message, is refused 501 by name: the page says only "Avoid using a reply's ts value" and lists no error for either | documented gap | `test_slack_fidelity.py::test_a_thread_ts_naming_a_reply_or_no_message_is_refused_by_name` | https://docs.slack.dev/reference/methods/chat.postMessage |
| `inclusive` includes the message at `latest` | documented | `test_history_honours_inclusive_at_latest` | https://docs.slack.dev/reference/methods/conversations.history |
| Every listing pages by `cursor`; `next_cursor` is `""` on the last page; an out-of-range `limit` is adjusted, not refused | documented | `test_conversations_list_pages_by_cursor_and_excludes_archived_on_request`, `test_users_list_pages_by_cursor`, `test_conversations_members_pages_by_cursor` | https://docs.slack.dev/apis/web-api/pagination |
| Page sizes: `conversations.list` 100 by default and at most 999, `conversations.history` 100 and at most 999, `conversations.members` 100, `conversations.replies` 1000; at most 1000 where a page names no ceiling | documented | `test_slack_fidelity.py::test_replies_come_a_thousand_to_a_page_when_no_limit_is_given` | each method's page; https://docs.slack.dev/apis/web-api/pagination ("The `limit` parameter maximum is `1000`") |
| `users.list` with no `limit` answers every member | documented | `test_slack_fidelity.py::test_users_list_with_no_limit_answers_every_member` | https://docs.slack.dev/reference/methods/users.list |
| `exclude_archived` leaves archived channels out | documented | `test_conversations_list_pages_by_cursor_and_excludes_archived_on_request` | https://docs.slack.dev/reference/methods/conversations.list |
| A member's profile carries no `email` when the member has none shown (`SlackSeed.without_email`); `users.lookupByEmail` does not find them | documented | `test_slack_people_kinds.py::test_a_member_declared_without_email_is_listed_with_no_email_and_not_found_by_it` | https://docs.slack.dev/reference/objects/user-object, https://docs.slack.dev/reference/methods/users.list |
| Slackbot is `USLACKBOT`, named `slackbot`, listed by `users.list`, `is_bot: false`, with no email | documented | `test_slack_people_kinds.py::test_slackbot_is_listed_and_answered_as_slack_serves_it` | https://docs.slack.dev/apis/web-api/pagination (its `users.list` example), https://docs.slack.dev/reference/objects/user-object ("Slackbot is special, so `is_bot` will be false for it") |
| A guest is `is_restricted`, a single-channel guest also `is_ultra_restricted`, a deactivated member `deleted` and still listed, another app's bot `is_bot` | documented | `test_slack_people_kinds.py::test_guests_bots_and_deactivated_members_carry_their_own_flags` | https://docs.slack.dev/reference/objects/user-object, https://docs.slack.dev/reference/methods/users.list |
| A person's absence with a reason shows that reason as `status_text` until it ends (`status_expiration`), `users.getPresence` `away`, `dnd.info` snoozed until its end; nothing else is written into the status, and `dnd.info` for someone never away carries no schedule | world data | `test_slack_fidelity.py::test_an_absence_shows_its_reason_as_the_status_and_nothing_the_scenario_did_not_give` | the scenario's `Person.absences`; fields: https://docs.slack.dev/reference/methods/users.profile.get, https://docs.slack.dev/reference/methods/dnd.info |
| A modal title past 24 characters is refused | documented | `test_a_modal_title_past_twenty_four_characters_is_refused` | https://docs.slack.dev/reference/views/modal-views |
| A view of more than 100 blocks is refused `invalid_arguments` | documented | `test_a_view_of_more_than_a_hundred_blocks_is_refused_invalid_arguments` | https://docs.slack.dev/reference/block-kit/blocks, https://docs.slack.dev/reference/methods/views.publish |
| `apps.connections.open` answers a `wss://…/link/?ticket=…&app_id=…` URL | documented | `test_slack_socket_mode.py::test_what_a_person_says_reaches_a_socket_mode_agent_as_an_envelope_it_acknowledges` | https://docs.slack.dev/reference/methods/apps.connections.open |
| A Socket Mode connection opens with `hello`; an event arrives as an `events_api` envelope the app acknowledges with its `envelope_id` | documented | `test_slack_socket_mode.py::test_what_a_person_says_reaches_a_socket_mode_agent_as_an_envelope_it_acknowledges` | https://docs.slack.dev/apis/events-api/using-socket-mode |
| Three seconds to acknowledge; three retries; each send its own `envelope_id` (a "unique identifier"); `retry_attempt` counted up; no `retry_reason`, which no page gives for Socket Mode | documented | `test_slack_socket_mode.py::test_an_envelope_the_agent_never_acknowledges_is_sent_again_and_then_fails_the_agent_is_refused` | https://docs.slack.dev/tools/bolt-python/concepts/acknowledge, https://docs.slack.dev/apis/events-api, https://docs.slack.dev/apis/events-api/using-socket-mode; `retry_attempt`: `slack_sdk.socket_mode.request.SocketModeRequest` |

## Where Slack's reference and this fake knowingly differ

- **Text Slack would rewrite.** Slack turns bare URLs into links and emoji into their colon names ("Your message may
  mutate", https://docs.slack.dev/reference/methods/chat.postMessage). The fake keeps what the agent sent.
- **A member id as `channel`.** The `chat.postMessage` page says three things: that it opens the bot's 1:1 DM, that
  it lands in the person's App Home, and that it lands in their DM with Slackbot. This fake does the first.
- **The app's own writes as events.** `message_changed` "is sent when a message … is edited using the chat.update
  API method". The fake pushes events only for what people do, never for the agent's own `chat.postMessage`,
  `chat.update` or `chat.delete`.
- **Retries are not spaced out.** Slack retries a request URL "nearly immediately", then after one and five minutes
  (https://docs.slack.dev/apis/events-api); no simulated time passes while the agent is being called, so the retries
  here follow each other.
- **`oauth.v2.access` answers `scope`** with the scopes of the methods this fake serves. Slack answers the scopes the
  app asked for at install, which the world does not hold; this is the one answered value no source gives.
