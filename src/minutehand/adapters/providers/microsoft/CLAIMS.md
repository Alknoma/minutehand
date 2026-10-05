# Microsoft provider — where its behaviour comes from

Each row is a fact about Microsoft's services that an older, home-grown Teams and Graph emulator asserted about
itself, re-checked against Microsoft's public documentation and pinned here by a test. **Documented** means the
cited page says it; **observed** means no page says it and it is kept on the word of people who saw the real
service do it. Tests are in `tests/providers/microsoft/test_microsoft_vendor_claims_{signin,connector,graph}.py`.

## Sign-in (`login.microsoftonline.com`)

| Claim | Class | Test | Source |
|---|---|---|---|
| Client credentials answer `access_token`, `token_type` Bearer and `expires_in`, for the Bot Framework and for Graph | documented | `test_a_registered_app_gets_a_bearer_token_for_the_bot_framework_and_for_graph` | https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow |
| A wrong client secret is `invalid_client`, AADSTS7000215, no token | documented | `test_a_wrong_client_secret_is_refused_invalid_client` | https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes |
| An app id the directory does not know is 400 `unauthorized_client`, AADSTS700016 | documented | `test_an_app_the_directory_has_never_seen_is_refused_unauthorized_client_400` | https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes |
| A refresh token the platform issued answers a new access token | documented | `test_a_refresh_token_the_platform_issued_answers_a_fresh_access_token` | https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow |
| A refresh token it never issued is 400 `invalid_grant` | documented | `test_a_refresh_token_the_platform_never_issued_is_refused_invalid_grant` | https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow |

## Bot Framework connector (`smba.trafficmanager.net`)

| Claim | Class | Test | Source |
|---|---|---|---|
| A call with no token is 401 | documented | `test_a_send_with_no_token_is_refused_401` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| A send to a conversation that does not exist is 404 `ConversationNotFound`, nothing delivered | documented | `test_a_send_to_a_conversation_that_does_not_exist_is_refused_conversation_not_found` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| Paged members of an unknown conversation are a 404 | documented | `test_paged_members_of_a_conversation_that_does_not_exist_is_refused_404` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| An activity past 100 KB counted as UTF-16 is 413 `MessageSizeTooBig`, nothing written | documented | `test_an_activity_over_the_documented_size_limit_is_refused_message_size_too_big`, `test_an_unknown_conversation_and_an_oversized_activity_are_refused` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| A message of 40,000 characters (80 KB as UTF-16) is inside the limit and delivered | documented | `test_a_message_of_forty_thousand_characters_is_inside_the_documented_limit_and_delivered` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| An activity well inside the limit is delivered whole | documented | `test_an_activity_well_inside_the_size_limit_is_delivered` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| Details of an unknown team are a 404 | observed | `test_details_of_a_team_that_does_not_exist_are_refused_404` | — |
| A send is stored and answered with its activity id | documented | `test_a_send_is_stored_and_answered_with_its_activity_id` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| An Adaptive Card attachment is kept as sent | documented | `test_an_adaptive_card_attachment_is_kept_as_sent` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| Reply to Activity in a channel threads under the activity it names | documented | `test_a_reply_to_an_activity_in_a_channel_threads_under_it` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| Reply to Activity where there are no nested replies (a personal chat) behaves like a send | documented | `test_a_reply_to_an_activity_in_a_personal_chat_is_delivered_like_a_send` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| Update Activity replaces the activity and answers its id | documented | `test_an_update_replaces_the_activitys_text` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| An update carrying text and a card together is 400 `BadSyntax` ("multiple skype activities"), activity unchanged | observed | `test_an_update_carrying_both_text_and_a_card_is_refused_bad_syntax` | — |
| The same update with the card alone is accepted | observed | `test_an_update_carrying_only_a_card_is_accepted` | — |
| Delete Activity removes the activity and answers no body | documented | `test_a_deleted_activity_is_gone_from_the_conversation` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| Reply, update or delete naming an activity the conversation does not hold is 404 `ActivityNotFoundInConversation` | documented | `test_an_activity_that_is_not_in_the_conversation_is_refused_activity_not_found_in_conversation` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| Updating or deleting a person's message is 403 `NotEnoughPermissions`; the message is unchanged | documented | `test_changing_a_persons_message_is_refused_not_enough_permissions` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| A proactive create to someone without the bot in personal scope is 403 `ForbiddenOperationException` | documented | `test_a_proactive_conversation_with_a_person_who_never_installed_the_bot_is_refused` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability and https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A proactive create with the user's id and the tenant answers the 1:1 conversation, an `a:` id | documented | `test_a_proactive_create_answers_the_persons_one_to_one_conversation` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A proactive create without the tenant is 400 | documented | `test_a_proactive_create_without_a_tenant_is_refused_400` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A bot cannot create a group chat (400) | documented | `test_a_proactive_create_of_a_group_chat_is_refused_400` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A proactive create naming another bot is 400 | observed | `test_a_proactive_create_naming_another_bot_is_refused_400` | — |
| Team details carry the Entra group id beside, and different from, the thread id | documented | `test_team_details_answer_the_groups_directory_id_beside_the_thread_id` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| The General channel is listed with `name` null and the team's id | documented | `test_the_general_channel_is_listed_with_no_name_and_the_teams_own_id` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| Paged members: 200 a page by default, a `continuationToken` to the rest, each member a `29:` id with the directory object id beside it | documented | `test_a_page_without_a_size_holds_two_hundred_members_and_a_token_for_the_rest` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| A `pageSize` under the documented minimum of 50 answers 50 (the page names a minimum and no refusal) | documented | `test_a_page_size_below_fifty_answers_fifty_members`, `test_paged_members_below_the_minimum_page_size_answer_fifty_and_this_roster_whole` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| A `pageSize` between 50 and 500 is the page's size | documented | `test_a_page_size_inside_the_bounds_is_honoured` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| A pushed channel activity names the team by `19:…@thread` id and by `aadGroupId` | documented | `test_a_channel_message_names_the_team_by_thread_id_and_by_group_id` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| A pushed activity's sender is a `29:` id with `aadObjectId` beside it | documented | `test_a_pushed_message_names_its_sender_by_mri_and_by_directory_id` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A reply inside a channel thread arrives with `;messageid=<root>` on its conversation id | observed | `test_a_reply_in_a_channel_thread_names_its_root_in_the_conversation_id` | — |

## Graph drive items (`graph.microsoft.com`)

| Claim | Class | Test | Source |
|---|---|---|---|
| A folder's children are listed in `value` | documented | `test_a_folders_children_are_listed_under_value` | https://learn.microsoft.com/en-us/graph/api/driveitem-list-children |
| An item read by id carries its `file` facet | documented | `test_a_file_is_read_by_id_with_its_file_facet` | https://learn.microsoft.com/en-us/graph/api/driveitem-get |
| An item that does not exist is 404 `itemNotFound` | documented | `test_an_item_that_does_not_exist_is_refused_404_item_not_found` | https://learn.microsoft.com/en-us/graph/errors |
| A folder is created under a parent, 201, with its `folder` facet | documented | `test_a_folder_is_created_under_a_parent_201_with_its_folder_facet` | https://learn.microsoft.com/en-us/graph/api/driveitem-post-children |
| PUT by path creates the file, 201, with its size in bytes | documented | `test_an_upload_by_path_creates_the_file_201_with_its_size` | https://learn.microsoft.com/en-us/graph/api/driveitem-put-content |
| GET `content` answers 302 to a pre-authenticated URL | documented | `test_downloading_content_redirects_302_to_a_preauthenticated_url` | https://learn.microsoft.com/en-us/graph/api/driveitem-get-content |
| PUT on an item id's `content` replaces it, 200 | documented | `test_putting_content_on_an_item_id_replaces_it_200` | https://learn.microsoft.com/en-us/graph/api/driveitem-put-content |
| PATCH `parentReference.id` moves the item, 200 | documented | `test_patching_parent_reference_moves_the_item_200` | https://learn.microsoft.com/en-us/graph/api/driveitem-move |
| DELETE answers 204 and the item is then not found | documented | `test_a_deleted_item_answers_204_and_is_then_not_found` | https://learn.microsoft.com/en-us/graph/api/driveitem-delete |
| `root/search(q=…)` answers matches in `value`, empty when none | documented | `test_search_from_the_root_finds_items_by_name_and_answers_empty_when_nothing_matches` | https://learn.microsoft.com/en-us/graph/api/driveitem-search |
| Search on a folder covers only what lies beneath it | documented | `test_search_from_a_folder_finds_only_what_lies_beneath_it` | https://learn.microsoft.com/en-us/graph/api/driveitem-search |
| `invite` answers one permission per recipient with the roles granted | documented | `test_an_invite_answers_the_permissions_it_granted` | https://learn.microsoft.com/en-us/graph/api/driveitem-invite |

## Not carried over

Contradicted by Microsoft's documentation (the old emulator was wrong; this provider follows the page):

- **A reply-to-activity in a personal or group chat refused 400 `BadArgument`.** The connector reference says every
  channel supports Reply to Activity and that without nested replies it behaves like a send.
- **The General channel listed with the name "General".** Teams sends its name as null.
- **An activity of 40,000 characters refused 413 at a 28 KB limit.** The Teams page puts the limit at 100 KB of the
  message counted as UTF-16 (80 KB to be safe); this provider measures it that way, and 40,000 characters are delivered.
- **Paged members one per page by default.** Teams documents a default of 200, a minimum of 50 and a maximum of 500.
- **`GET …/content` answered 200 with the bytes.** Graph answers 302 to a download URL.
- **A refresh token accepted whatever its value.** The platform refuses one it did not issue with `invalid_grant`.

Observed by the old emulator, not adopted (the documentation is silent, so this provider's behaviour stays):

- **A wrong client secret answered 400.** This provider answers 401 `invalid_client` (AADSTS7000215). The identity
  platform's token-endpoint pages show a 400 only on an `invalid_scope` example and list `invalid_client` ("client
  authentication failed") with no status of its own
  (https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow,
  https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow); RFC 6749 §5.2 allows 401 for a
  failed client authentication.
- **Delete Activity answered 204.** The connector reference says only that Delete Activity returns "an HTTP status
  code" with nothing in the body
  (https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference); this
  provider answers 200, and only "success, no body" is pinned.

Error codes this provider answers that are not on Teams' status-code table, kept as they are (every other connector
code it answers is on that table, https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability):

- **`BadArgument`.** The table spells the 400 code `Bad Argument`, with a space; every other code there is one word,
  and this provider keeps the one-word form clients compare against.
- **`BadSyntax`** on an update carrying both text and a card: observed by the old emulator (see the table above).

Observed, kept as facts, pending a fault kind the Microsoft seed is gaining (not yet declarable here):

- **A send acknowledged 202 with an empty body and no activity id**, and a reply acknowledged that way still
  threaded under its parent.

Emulator artefacts with no counterpart in Microsoft's service: its `/health` route, its admin routes (clear,
reset, provisioning a conversation, simulating a message or a card press, reading stored messages), its fixed
fixture ids and secrets, an `expires_in` of exactly 3600, and its seeded folder names.
