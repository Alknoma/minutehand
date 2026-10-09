# Microsoft provider — where its behaviour comes from

Every behaviour this provider keeps is Microsoft's: **documented** means the cited page (or Microsoft's published
API description or example answer) says it; **observed** means a recorded answer of the real service, cited where
it is public (a Microsoft-published sample recorded from Exchange Online, or an issue thread quoting the service's
own answer). Each
row is pinned by the test it names, in `tests/providers/microsoft/`. What Microsoft does not document, and no
recording shows, is not answered as fact: it is refused by name (501 `not_implemented`, naming the method and path
and why), listed under "Refused by name" below.

## Graph's surface

Microsoft publishes Graph `v1.0` as an OpenAPI description. The subset for the resources this provider claims is
kept as test data, with its source, commit and date:
`tests/data/microsoft_graph_v1/openapi-subset-retrieved-2026-10-08.json` (from
https://github.com/microsoftgraph/msgraph-metadata, `openapi/v1.0/openapi.yaml` at commit
`15780e98e1082517bc7a39a6b75b356a085326d9`, retrieved 2026-10-08; the file states the filter that made the subset).

| Claim | Class | Test | Source |
|---|---|---|---|
| The subset holds 2,103 operations (method and path); 200 are served (`surface.SERVED`), every one of them in the subset | documented | `test_what_is_served_is_on_graphs_published_surface` | `tests/data/microsoft_graph_v1/openapi-subset-retrieved-2026-10-08.json` |
| The other 1,903 are refused by name, 501 `not_implemented`, before any surface reads them; a served read answers below 400 when called as its page documents | documented | `test_every_published_operation_is_served_or_refused_by_name` | `tests/data/microsoft_graph_v1/openapi-subset-retrieved-2026-10-08.json` |
| Two served reads the description leaves out are on their reference page: `GET /me/mailboxSettings/automaticRepliesSetting` and its `/users/{id}` form | documented | `test_a_person_away_shows_out_of_office_while_it_lasts_and_available_after` | https://learn.microsoft.com/en-us/graph/api/user-get-mailboxsettings |
| A path is matched in the forms Graph reads alike: `name('key')` key segments, a drive through `/me/drive`, `/users/{id}/drive` or `/sites/{id}/drive`, a site by `{host}:/{path}:`, an item by path, `delta` with or without `()` | documented | `test_every_published_operation_is_served_or_refused_by_name` | https://learn.microsoft.com/en-us/graph/onedrive-addressing-driveitems |
| A Graph error is `{"error": {"code", "message", "innerError"}}` | documented | `test_a_team_unknown_is_refused_in_graphs_shape` | https://learn.microsoft.com/en-us/graph/errors |

## Credentials

Authentication is out of scope (`docs/design.md`, "Authentication is out of scope"). Any token, credential or client secret works, every sign-in succeeds, and
no scope, role or permission grant refuses a call. These checks, each of which the identity platform, Graph or the
connector does make, this stack does not test:

- the token endpoint's wrong client secret (AADSTS7000215), unknown client id (AADSTS700016), app registered in
  another tenant (AADSTS700016), unknown tenant in the path (AADSTS90002) and unknown resource in a scope
  (AADSTS500011): each is now answered with a token, its `appid` the client id as sent, its tenant the world's;
- an authorization code's and a refresh token's signature, lifetime, app and redirect URI (AADSTS9002313, 50148,
  70000): a code or refresh token is read only for the user it names;
- authorize's unknown client id (AADSTS700016) and user of another tenant (AADSTS50020);
- Graph's 401 `InvalidAuthenticationToken` for a missing, unreadable, forged or expired token or one for another
  audience: a call presenting no readable token is the tenant's own application (app-only);
- the connector's 401 "Authorization has been denied for this request." for the same: the bot calling is the app
  the token names, else the world's bot;
- the pre-authenticated SharePoint URL's `tempauth` check (401 `unauthenticated`);
- the `roles` claim on application tokens (`Sites.ReadWrite.All`, `Files.ReadWrite.All`, `User.Read.All`,
  `ChannelMessage.Read.All`, `Chat.Read.All`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`): no token
  carries roles and nothing reads them.

What still refuses is the world itself, and stays: a user an administrator disabled (AADSTS50057) or removed
(AADSTS50034) cannot sign in, a refusal of world data, not of a credential; a user's token reaches only that user's own mailbox, calendar and OneDrive; a bot reaches only the
conversations it is installed in.

| Claim | Class | Test | Source |
|---|---|---|---|
| Client credentials answer `access_token`, `token_type` Bearer and `expires_in`, for the Bot Framework and for Graph | documented | `test_a_registered_app_gets_a_bearer_token_for_the_bot_framework_and_for_graph` | https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow |
| A client-credentials scope without `/.default` is `invalid_scope`, AADSTS1002012; an unknown grant type is `unsupported_grant_type`, AADSTS70003 (the protocol's refusals, which no credential decides) | documented | `test_a_bad_scope_and_an_unknown_grant_are_refused` | https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes |
| A refresh token the platform issued answers a new access token | documented | `test_a_refresh_token_the_platform_issued_answers_a_fresh_access_token` | https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow |
| A disabled user cannot sign in, AADSTS50057; a removed one, AADSTS50034 | documented | `test_a_disabled_user_cannot_sign_in_or_be_messaged_until_enabled_and_a_removed_one_is_gone` | https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes |
| Any secret, any client id and any tenant get a token | documented | `test_any_secret_any_client_and_any_tenant_get_a_token`, `test_a_wrong_client_secret_and_an_unknown_app_still_get_a_token` | https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow (the grant answered; the checks it documents are deliberately not made, above) |
| Any token, or none, sends to the connector as the world's bot, and reads Graph as the tenant's application | documented | `test_any_token_or_none_is_the_worlds_bot_sending`, `test_a_team_unknown_is_refused_in_graphs_shape` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference (the call answered; its authentication deliberately not checked, above) |

## Bot Framework connector (`smba.trafficmanager.net`)

| Claim | Class | Test | Source |
|---|---|---|---|
| A send to a conversation that does not exist is 404 `ConversationNotFound`, nothing delivered | documented | `test_a_send_to_a_conversation_that_does_not_exist_is_refused_conversation_not_found` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| Paged members of an unknown conversation are a 404 | documented | `test_paged_members_of_a_conversation_that_does_not_exist_is_refused_404` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| An activity past 100 KB counted as UTF-16 is 413 `MessageSizeTooBig`, nothing written | documented | `test_an_activity_over_the_documented_size_limit_is_refused_message_size_too_big`, `test_an_unknown_conversation_and_an_oversized_activity_are_refused` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| A message of 40,000 characters (80 KB as UTF-16) is inside the limit and delivered | documented | `test_a_message_of_forty_thousand_characters_is_inside_the_documented_limit_and_delivered` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| An activity well inside the limit is delivered whole | documented | `test_an_activity_well_inside_the_size_limit_is_delivered` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| A send is stored and answered with its activity id | documented | `test_a_send_is_stored_and_answered_with_its_activity_id` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| An Adaptive Card attachment is kept as sent | documented | `test_an_adaptive_card_attachment_is_kept_as_sent` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| A bot's `entities` (its mentions among them) are kept as sent, and Graph's chatMessage lists the mentions with the user's directory id | documented | `test_a_bots_mentions_are_kept_and_graph_reads_them` | https://learn.microsoft.com/en-us/previous-versions/microsoftteams/platform/resources/bot-v3/bot-conversations/bots-conv-channel |
| Reply to Activity in a channel threads under the activity it names | documented | `test_a_reply_to_an_activity_in_a_channel_threads_under_it` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| Reply to Activity where there are no nested replies (a personal chat) behaves like a send | documented | `test_a_reply_to_an_activity_in_a_personal_chat_is_delivered_like_a_send` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
| Update Activity replaces the activity and answers its id, a card alone included | documented | `test_an_update_replaces_the_activitys_text`, `test_an_update_carrying_only_a_card_is_accepted` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| Delete Activity removes the activity and answers 200 with no body ("The operation succeeded, there is no response.") | documented | `test_a_deleted_activity_is_gone_from_the_conversation` | https://github.com/microsoft/botbuilder-dotnet/blob/8efdbd723f0ceaa9ece353ca359ae4a44576794b/libraries/Swagger/ConnectorAPI.json |
| A payload the connector cannot use (no text or attachments, an unreadable body, a create without its tenant or for a group) is 400 with the code spelled `Bad Argument`, as Teams' table spells it | documented | `test_a_payload_the_connector_cannot_use_is_bad_argument_as_teams_spells_it` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| A member the conversation does not hold, or a proactive create naming someone who is no user of the tenant, has no code in Teams' table: refused by name | documented | `test_a_member_the_conversation_does_not_hold_is_refused_by_name` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| Reply, update or delete naming an activity the conversation does not hold is 404 `ActivityNotFoundInConversation` | documented | `test_an_activity_that_is_not_in_the_conversation_is_refused_activity_not_found_in_conversation` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| Updating or deleting a person's message is 403 `NotEnoughPermissions`; the message is unchanged | documented | `test_changing_a_persons_message_is_refused_not_enough_permissions` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability |
| A proactive create to someone without the bot in personal scope is 403 `ForbiddenOperationException` | documented | `test_a_proactive_conversation_with_a_person_who_never_installed_the_bot_is_refused` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A proactive create with the user's id and the tenant answers the 1:1 conversation, an `a:` id | documented | `test_a_proactive_create_answers_the_persons_one_to_one_conversation` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A proactive create without the tenant is 400 | documented | `test_a_proactive_create_without_a_tenant_is_refused_400` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A bot cannot create a group chat (400) | documented | `test_a_proactive_create_of_a_group_chat_is_refused_400` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| Team details carry the Entra group id beside, and different from, the thread id | documented | `test_team_details_answer_the_groups_directory_id_beside_the_thread_id` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| The General channel is listed with `name` null and the team's id | documented | `test_the_general_channel_is_listed_with_no_name_and_the_teams_own_id` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| Paged members: 200 a page by default, a `continuationToken` to the rest, each member a `29:` id with the directory object id beside it | documented | `test_a_page_without_a_size_holds_two_hundred_members_and_a_token_for_the_rest` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| A `pageSize` under the documented minimum of 50 answers 50; one between 50 and 500 is the page's size | documented | `test_a_page_size_below_fifty_answers_fifty_members`, `test_a_page_size_inside_the_bounds_is_honoured` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| A pushed channel activity names the team by `19:…@thread` id and by `aadGroupId` | documented | `test_a_channel_message_names_the_team_by_thread_id_and_by_group_id` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context |
| A pushed activity's sender is a `29:` id with `aadObjectId` beside it | documented | `test_a_pushed_message_names_its_sender_by_mri_and_by_directory_id` | https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages |
| A reply inside a channel thread arrives with `;messageid=<root>` on its conversation id: the channel plus the top-level message id | documented | `test_a_reply_in_a_channel_thread_names_its_root_in_the_conversation_id` | https://learn.microsoft.com/en-us/previous-versions/microsoftteams/platform/resources/bot-v3/bot-conversations/bots-conv-channel |
| Connector operations not served (Get Conversations, Send Conversation History, Upload Attachment, an activity's members) are refused by name | documented | `test_connector_operations_not_served_are_refused_by_name` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |

## Graph drive items (`graph.microsoft.com`)

| Claim | Class | Test | Source |
|---|---|---|---|
| A folder's children are listed in `value` | documented | `test_a_folders_children_are_listed_under_value` | https://learn.microsoft.com/en-us/graph/api/driveitem-list-children |
| An item read by id carries its `file` facet | documented | `test_a_file_is_read_by_id_with_its_file_facet` | https://learn.microsoft.com/en-us/graph/api/driveitem-get |
| An item that does not exist is 404 `itemNotFound` | documented | `test_an_item_that_does_not_exist_is_refused_404_item_not_found` | https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/errors |
| A folder is created under a parent, 201, with its `folder` facet | documented | `test_a_folder_is_created_under_a_parent_201_with_its_folder_facet` | https://learn.microsoft.com/en-us/graph/api/driveitem-post-children |
| PUT by path creates the file, 201, with its size in bytes | documented | `test_an_upload_by_path_creates_the_file_201_with_its_size` | https://learn.microsoft.com/en-us/graph/api/driveitem-put-content |
| GET `content` answers 302 to a pre-authenticated URL | documented | `test_downloading_content_redirects_302_to_a_preauthenticated_url` | https://learn.microsoft.com/en-us/graph/api/driveitem-get-content |
| PUT on an item id's `content` replaces it, 200 | documented | `test_putting_content_on_an_item_id_replaces_it_200` | https://learn.microsoft.com/en-us/graph/api/driveitem-put-content |
| PATCH `parentReference.id` moves the item, 200 | documented | `test_patching_parent_reference_moves_the_item_200` | https://learn.microsoft.com/en-us/graph/api/driveitem-move |
| DELETE answers 204 and the item is then not found | documented | `test_a_deleted_item_answers_204_and_is_then_not_found` | https://learn.microsoft.com/en-us/graph/api/driveitem-delete |
| `if-match` naming neither the item's eTag nor its cTag is 412 on PATCH and DELETE, code `resourceModified`, the item unchanged | documented | `test_a_stale_if_match_is_refused_412_and_the_item_is_unchanged` | https://learn.microsoft.com/en-us/graph/api/driveitem-update and https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/errors |
| A driveItem property the provider would not keep (`description`, …) is refused by name, never dropped | documented | `test_drive_item_properties_that_would_be_dropped_are_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/resources/driveitem |
| A delta token does not expire with age (Graph publishes no lifetime); `resyncRequired` comes only from a declared fault | documented | `test_an_old_delta_token_still_lists_what_changed_since` | https://learn.microsoft.com/en-us/graph/api/driveitem-delta |
| `root/search(q=…)` answers matches in `value`, empty when none; on a folder it covers only what lies beneath it | documented | `test_search_from_the_root_finds_items_by_name_and_answers_empty_when_nothing_matches`, `test_search_from_a_folder_finds_only_what_lies_beneath_it` | https://learn.microsoft.com/en-us/graph/api/driveitem-search |
| `invite` answers one permission per recipient with the roles granted | documented | `test_an_invite_answers_the_permissions_it_granted` | https://learn.microsoft.com/en-us/graph/api/driveitem-invite |

## Graph mail and calendars (`graph.microsoft.com`)

Each Exchange error code answered here is answered with the status and message the real service was recorded
answering it with; a code with no Graph recording (Exchange's `ErrorFolderNotFound`, `ErrorCalendar…`,
`ErrorMailRecipientNotFound`) is not answered, and its condition is refused by name instead.

| Claim | Class | Test | Source |
|---|---|---|---|
| `sendMail` answers 202 Accepted with no body, and saves the message in Sent Items | documented | `test_a_sent_email_is_one_copy_per_mailbox_and_the_senders_copy_asks_each_recipient` | https://learn.microsoft.com/en-us/graph/api/user-sendmail |
| A reply goes to the message's `replyTo` when it names any, else to its sender; so a reply from Sent Items goes to the mailbox itself | documented | `test_a_reply_goes_to_the_messages_reply_to_and_else_to_its_sender` | https://learn.microsoft.com/en-us/graph/api/message-reply |
| Reply-all goes to the sender (or `replyTo`) and every recipient of the message | documented | `test_a_reply_to_all_goes_to_the_sender_and_every_recipient` | https://learn.microsoft.com/en-us/graph/api/message-replyall |
| A comment and the message's body together are 400, nothing sent; the page gives no code, so the error carries none | documented | `test_a_comment_and_a_body_together_are_refused_400` | https://learn.microsoft.com/en-us/graph/api/message-reply |
| `reply` answers 202 and the reply is in the message's conversation | documented | `test_a_persons_reply_lands_in_the_senders_inbox_in_the_conversation_and_is_notified` | https://learn.microsoft.com/en-us/graph/api/message-reply |
| A body is stored as sent and read as HTML unless `Prefer: outlook.body-content-type="text"` asks for text, which `Preference-Applied` confirms | documented | `test_an_events_body_and_attendees_are_kept_as_sent`, `test_filter_orderby_top_and_paging_read_the_mailbox_as_clients_ask` | https://learn.microsoft.com/en-us/graph/api/user-list-calendarview |
| A reply's `body` and `bodyPreview` are left out: Graph composes them from what is written and the quoted original ("the reply message in HTML that this API creates"), and no page or recording shows how | documented | `test_a_replys_body_graph_composes_is_left_out_and_the_run_records_what_was_written` | https://learn.microsoft.com/en-us/graph/api/message-reply |
| A message or event property the provider would not keep (`categories`, `isOnlineMeeting`, …) is refused by name and nothing is sent or made | documented | `test_message_properties_that_would_be_dropped_are_refused_by_name_and_nothing_is_sent`, `test_event_properties_that_would_be_dropped_are_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/resources/message |
| A message and an event carry `changeKey` and `@odata.etag` W/"changeKey", both renewed with every change | documented | `test_outlook_items_carry_their_change_key_as_a_weak_etag_and_both_change_with_them` | https://learn.microsoft.com/en-us/graph/api/message-get |
| Messages page 10 by default, `$top` 1 to 1000, the next page by `@odata.nextLink` | documented | `test_filter_orderby_top_and_paging_read_the_mailbox_as_clients_ask` | https://learn.microsoft.com/en-us/graph/api/user-list-messages |
| With `$filter` and `$orderby` together, the `$orderby` properties must open the `$filter` in order, else `InefficientFilter` "The restriction or sort order is too complex for this operation." | documented | `test_an_orderby_that_does_not_lead_the_filter_is_refused_inefficient_filter` | https://learn.microsoft.com/en-us/graph/api/user-list-messages |
| `InefficientFilter` is a 400 | observed | `test_an_orderby_that_does_not_lead_the_filter_is_refused_inefficient_filter` | https://github.com/mpalermiti/outlook-mcp/issues/31 |
| A list of messages or events without `$orderby` that would hold more than one is refused by name: neither page documents an order, no Microsoft page or public recording states one (searched: List messages, List events, List calendarView, the query-parameters page, the Outlook REST v2 reference, Microsoft's own email tutorials, which send `$orderby` themselves; only `$search` results are documented sorted, by receivedDateTime descending). Client authors: send `$orderby` | documented | `test_filter_orderby_top_and_paging_read_the_mailbox_as_clients_ask`, `test_an_event_list_without_orderby_holding_more_than_one_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/user-list-messages and https://learn.microsoft.com/en-us/graph/api/user-list-events |
| A reply's subject is `RE: ` before the original's; an acceptance's is `Accepted: ` before the event's; a tentative or declining answer's is left out (no source shows it) | observed | `test_a_reply_goes_to_the_messages_reply_to_and_else_to_its_sender`, `test_only_an_acceptance_carries_a_subject_a_recording_shows` | https://github.com/microsoftgraph/dataconnect-solutions/blob/e6b679831b424c6a6a0a68d8246e0b3be38140d9/Datasets/data-connect-dataset-sentitems.md |
| A mailbox's folders are named "Deleted Items", "Drafts", "Inbox", "Sent Items" and listed in that order | documented | `test_mail_folders_list_by_display_name_as_graphs_example_answer_does` | https://learn.microsoft.com/en-us/graph/api/user-list-mailfolders |
| `internetMessageId` is left out (Exchange assigns it from its own hosts) and a `$filter` on it is refused by name | documented | `test_internet_message_id_is_left_out_and_filtering_on_it_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/resources/message |
| A message or event that is not in the mailbox is 404 `ErrorItemNotFound` "The specified object was not found in the store." | observed | `test_an_invitation_asks_each_attendee_and_their_accept_lands_as_their_change` | https://github.com/microsoftgraph/msgraph-sdk-java/issues/1171 |
| A folder's message `delta` pages to a `@odata.deltaLink`, and from it lists what changed, a removal as `@removed` | documented | `test_delta_lists_the_folder_then_only_what_arrived_and_what_left` | https://learn.microsoft.com/en-us/graph/api/message-delta |
| A well-known folder this provider does not hold (`junkemail`, …), or any folder the mailbox does not hold, is refused by name | documented | `test_a_folder_the_mailbox_does_not_hold_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/resources/mailfolder |
| A calendar view's `delta` needs `startDateTime` and `endDateTime`; the first round pages the events in the window by `@odata.nextLink` (`$skiptoken`, `Prefer: odata.maxpagesize`) to an `@odata.deltaLink` (`$deltatoken`), whose tokens carry the window; a later round lists the events added or updated in the window in full, and an event deleted, or added, deleted or updated outside the window, as `@removed` with the reason `deleted`; `$select`, `$filter`, `$orderby`, `$search` and `$expand` are unsupported and refused by name | documented | `test_a_calendar_view_delta_pages_to_a_delta_link_then_lists_what_changed`, `test_a_calendar_view_delta_without_its_window_or_with_an_unsupported_option_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/event-delta and https://learn.microsoft.com/en-us/graph/delta-query-events |
| `delta` on `events` of a calendar not bound to a window is documented for the beta version only, so it is refused by name | documented | `test_a_calendar_view_delta_without_its_window_or_with_an_unsupported_option_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/delta-query-events |
| A folder's message `delta` lists a message that was deleted for good or moved to another folder (which removes it from this one) as `@removed` | documented | `test_a_folder_delta_reports_a_message_moved_away_and_one_deleted_for_good` | https://learn.microsoft.com/en-us/graph/api/message-delta |
| A send with no recipient, or a recipient or attendee whose address is none, is 400 `ErrorInvalidRecipients` "At least one recipient isn't valid." | observed | `test_a_send_with_no_recipient_or_a_bad_address_is_refused_invalid_recipients` | https://github.com/microsoftgraph/php-connect-sample/issues/13 and https://github.com/microsoftgraph/msgraph-sdk-php/issues/280 |
| An event whose end is before its start, and the organizer answering their own meeting, are refused by name (Exchange's EWS codes exist; no Graph answer is recorded) | documented | `test_an_end_before_the_start_and_an_organizer_answering_are_refused_by_name` | https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/responsecode |
| A user's token reaching another user's mailbox or calendar is 403 `ErrorAccessDenied` "Access is denied. Check credentials and try again." (whose mailbox it is is the world's) | observed | `test_a_users_token_reaching_another_mailbox_is_refused_access_denied`, `test_an_invitation_asks_each_attendee_and_their_accept_lands_as_their_change` | https://github.com/microsoftgraph/microsoft-graph-explorer-v4/issues/2620 and https://github.com/hashicorp/terraform-provider-azuread/issues/1929 |
| An unreadable request body to mail, calendars or subscriptions is refused by name (Graph's table gives 400 for a malformed request, no code); to files it is still 400 `invalidRequest`, tracked as unsourced below | documented | `test_a_body_that_cannot_be_read_is_refused_by_name_where_graph_records_no_answer` | https://learn.microsoft.com/en-us/graph/errors and https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/errors |
| `Prefer: outlook.timezone` other than UTC, and `IdType="ImmutableId"`, are refused by name; without them times are UTC | documented | `test_a_preference_that_would_change_the_answer_unserved_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/user-list-calendarview |
| An Outlook message subscription lasts at most 10,080 minutes; asking for longer, whose answer is not documented, is refused by name | documented | `test_a_mail_subscription_past_seven_days_or_on_a_folder_that_is_none_is_refused` | https://learn.microsoft.com/en-us/graph/api/resources/subscription |
| A subscription whose notification URL fails validation is 400, with no code (the page gives none) | documented | `test_a_subscription_unvalidated_or_repeated_is_refused_and_one_too_long_or_unwatchable_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions |
| A second subscription with the same `changeType` and `resource` is 409 "Subscription Id <id> already exists for the requested combination", with no code | documented | `test_a_subscription_unvalidated_or_repeated_is_refused_and_one_too_long_or_unwatchable_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions |
| Renewing a subscription that expired or was deleted is 404, with no code; reading or deleting one, and every other subscription refusal, is refused by name | documented | `test_a_subscription_expires_on_the_runs_clock_and_notifies_nothing_after` | https://learn.microsoft.com/en-us/graph/api/subscription-update |
| An event made with attendees sends them an invitation carrying the event's body; an attendee's response is in `attendees[].status` | documented | `test_an_invitation_asks_each_attendee_and_their_accept_lands_as_their_change` | https://learn.microsoft.com/en-us/graph/api/user-post-events |
| An event's attendees are kept as sent, address and name | documented | `test_an_events_body_and_attendees_are_kept_as_sent` | https://learn.microsoft.com/en-us/graph/api/resources/attendee |
| `calendarView` without `startDateTime` and `endDateTime` (both required, no answer documented) is refused by name | documented | `test_a_calendar_view_without_a_window_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/user-list-calendarview |
| A POST repeated with the same `transactionId` makes the event once | documented | `test_an_event_retried_with_its_transaction_id_is_made_once` | https://learn.microsoft.com/en-us/graph/api/resources/event |
| `getSchedule`'s `availabilityView` is one digit an interval: 0 free, 1 tentative, 2 busy, 3 out of office; an address that is no mailbox of the tenant is refused by name (no answer for it is documented or recorded) | documented | `test_the_calendar_view_and_free_busy_read_every_calendar_and_its_answers` | https://learn.microsoft.com/en-us/graph/api/calendar-getschedule |
| A person's automatic reply is the reason the scenario gives, as written | documented | `test_a_person_away_shows_out_of_office_while_it_lasts_and_available_after` | https://learn.microsoft.com/en-us/graph/api/resources/automaticrepliessetting |
| An event's `recurrence` is a daily or weekly `pattern` (`interval`, and for weekly `daysOfWeek` and `firstDayOfWeek`) over a `range` of `endDate` (inclusive), `noEnd` or `numbered`, whose `startDate` is the date the event starts on; a weekly pattern repeats on `daysOfWeek` in every `interval`-th week, and the first occurrence may be the start day or later; the answer names the days in lower case, with `firstDayOfWeek` sunday, `index` first, `month` and `dayOfMonth` 0 and `numberOfOccurrences` 0 where not sent, as the page's example answer does; the event is a `seriesMaster` | documented | `test_a_weekly_series_is_kept_as_sent_with_the_defaults_the_page_documents_and_lists_its_instances`, `test_every_other_week_from_a_first_day_and_a_numbered_range_and_a_daily_series_without_end` | https://learn.microsoft.com/en-us/graph/api/resources/recurrencepattern, https://learn.microsoft.com/en-us/graph/api/resources/recurrencerange and https://learn.microsoft.com/en-us/graph/api/user-post-events |
| `GET …/events/{id}/instances?startDateTime&endDateTime` of a series master lists its occurrences in the window, each `type` `occurrence` with its `seriesMasterId`; a calendar view and free/busy hold the occurrences that fall in their window | documented | `test_a_weekly_series_is_kept_as_sent_with_the_defaults_the_page_documents_and_lists_its_instances`, `test_a_calendar_view_and_free_busy_hold_the_occurrences_not_the_series` | https://learn.microsoft.com/en-us/graph/api/event-list-instances and https://learn.microsoft.com/en-us/graph/delta-query-events |
| `cancel` is the organizer's: a cancellation with the comment goes to the attendees (`meetingMessageType` `meetingCancelled`) and the event leaves the calendar, 202 with no body; an attendee is 400 "Your request can't be completed. You need to be an organizer to cancel a meeting.", with no code | documented | `test_cancelling_a_meeting_tells_the_attendees_with_the_comment_and_only_its_organizer_may` | https://learn.microsoft.com/en-us/graph/api/event-cancel and https://learn.microsoft.com/en-us/graph/api/resources/eventmessage |
| `decline` takes `comment` (text included in the response) and `sendResponse` (default true), 202 with no body; `proposedNewTime` is refused by name | documented | `test_declining_with_a_comment_sends_the_organizer_the_comment` | https://learn.microsoft.com/en-us/graph/api/event-decline |
| A file is attached to an event by `POST …/events/{id}/attachments`, 201 with the attachment, under 3 MB, and read by `GET …/attachments` and `…/attachments/{id}`; the event's `hasAttachments` is then true | documented | `test_a_file_attached_to_an_event_is_kept_as_sent_and_read_by_its_attendees` | https://learn.microsoft.com/en-us/graph/api/event-post-attachments and https://learn.microsoft.com/en-us/graph/api/event-list-attachments |
| `POST /me/messages` makes a draft in Drafts from a JSON message, 201 with the message, `isDraft` true, no `sender` or `from` (the page's example answer has none); a MIME draft is refused by name | documented | `test_a_draft_is_made_changed_and_sent_to_its_recipients`, `test_a_draft_with_no_recipient_cannot_be_sent_and_a_message_that_is_no_draft_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/user-post-messages |
| A draft's `subject`, `body`, recipients and `importance` are updatable only if `isDraft` is true; a PATCH answers 200 with the message; on a message that is no draft, whose answer is not documented, it is refused by name | documented | `test_a_draft_is_made_changed_and_sent_to_its_recipients`, `test_a_draft_with_no_recipient_cannot_be_sent_and_a_message_that_is_no_draft_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/message-update |
| `send` of a draft answers 202 with no body and saves the message in Sent Items; the draft is no longer in Drafts, and its id is not the sent message's (`id` changes when the item moves from one folder to another) | documented | `test_a_draft_is_made_changed_and_sent_to_its_recipients` | https://learn.microsoft.com/en-us/graph/api/message-send and https://learn.microsoft.com/en-us/graph/api/resources/message |
| A draft with no recipient cannot be sent: 400 `ErrorInvalidRecipients` | observed | `test_a_draft_with_no_recipient_cannot_be_sent_and_a_message_that_is_no_draft_is_refused_by_name` | https://github.com/microsoftgraph/php-connect-sample/issues/13 |
| `createReply` and `createReplyAll` make a draft addressed as `reply` and `replyAll` address, 201; a comment and the message's body together are 400, no code; the draft's body, which Graph composes, is left out | documented | `test_a_reply_draft_answers_as_reply_does_and_takes_the_body_written_later` | https://learn.microsoft.com/en-us/graph/api/message-createreply and https://learn.microsoft.com/en-us/graph/api/message-createreplyall |
| `createForward` makes a draft, 201, and `forward` sends at once, 202 with no body and saved in Sent Items; either a comment or the message's body, and the recipients either as `toRecipients` or in the message, never both and never neither: 400, no code; the subject Outlook composes and the conversation it assigns are not sourced, so the draft has no subject unless sent and starts a conversation of its own; a message with attachments is not forwarded (no page says whether they travel) | documented | `test_a_forward_names_its_recipients_once_and_a_forward_draft_is_sent_later` | https://learn.microsoft.com/en-us/graph/api/message-forward and https://learn.microsoft.com/en-us/graph/api/message-createforward |
| `move` makes a new copy of the message in the destination folder (an id or a well-known name) and removes the original, 201 with the copy, the copy's `createdDateTime` the original's (the page's example answer); `copy` does the same and keeps the original, 201 | documented | `test_move_makes_a_new_copy_in_the_folder_and_removes_the_original_and_copy_keeps_it` | https://learn.microsoft.com/en-us/graph/api/message-move and https://learn.microsoft.com/en-us/graph/api/message-copy |
| A subscription on a folder's messages is notified `created` when a message is added to the folder (a draft made in Drafts, a message moved or copied in, a message sent) and `updated` when any of its properties change | documented | `test_a_draft_is_made_changed_and_sent_to_its_recipients`, `test_move_makes_a_new_copy_in_the_folder_and_removes_the_original_and_copy_keeps_it` | https://learn.microsoft.com/en-us/graph/outlook-change-notifications-overview |
| A file is attached to a draft by `POST …/attachments` with `name` and base64 `contentBytes`, 201 with the attachment: `size` its bytes, `isInline` false unless sent, `contentBytes` as sent; under 3 MB | documented | `test_attachments_are_kept_as_sent_listed_read_and_carried_to_each_recipient` | https://learn.microsoft.com/en-us/graph/api/message-post-attachments and https://learn.microsoft.com/en-us/graph/api/resources/fileattachment |
| `GET …/attachments` lists a message's attachments and `GET …/attachments/{id}` reads one; neither page documents an order, so a list of more than one without `$orderby` is refused by name | documented | `test_attachments_are_kept_as_sent_listed_read_and_carried_to_each_recipient` | https://learn.microsoft.com/en-us/graph/api/message-list-attachments |
| `hasAttachments` does not count inline attachments; the attachments of a sent message are in each copy | documented | `test_send_mail_carries_its_attachments_and_what_is_not_a_small_file_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/resources/message |

## Graph Teams messages

| Claim | Class | Test | Source |
|---|---|---|---|
| A channel's messages list newest first by the last change to their whole reply chain | documented | `test_channel_messages_list_by_their_reply_chains_last_change` | https://learn.microsoft.com/en-us/graph/api/channel-list-messages |
| A chat's messages list by `lastModifiedDateTime`, newest first, the documented default | documented | `test_channel_messages_list_by_their_reply_chains_last_change` | https://learn.microsoft.com/en-us/graph/api/chat-list-messages |
| A message's replies list newest first, as the page's example answer lists them | documented | `test_channel_messages_list_by_their_reply_chains_last_change` | https://learn.microsoft.com/en-us/graph/api/chatmessage-list-replies |
| `messages/delta` of a chat or a channel (the operations the OpenAPI description links to `chatmessage-delta`) lists the messages by round: the first pages all of them by `@odata.nextLink` (`$skiptoken`, `$top` to 50) to an `@odata.deltaLink` (`$deltatoken`), and a round from a delta link lists the messages posted or changed after it; a channel's replies are left to the replies operations; `$filter`, `$skip` and any other option are refused by name (the page's request line is `GET /users/{id}/chats/getAllMessages/delta`, which is not served, and its `$filter` is for that form) | documented | `test_a_chat_messages_delta_lists_all_then_those_posted_or_changed_since`, `test_a_channel_messages_delta_leaves_the_replies_out` | https://learn.microsoft.com/en-us/graph/api/chatmessage-delta |
| A signed-in user posts a message to a chat or a channel with `POST …/messages`; only `body` is required; the answer is 201 with the new `chatMessage`, its `from` the user, its `body` and `importance` (`normal` unless sent) as sent, a body with no `contentType` text | documented | `test_a_message_posted_to_a_chat_is_kept_as_sent_read_back_and_notified`, `test_a_text_body_defaults_to_text_and_pages_with_others` | https://learn.microsoft.com/en-us/graph/api/chatmessage-post |
| A message's `subject`, `importance`, `mentions` and `attachments` (a card's `content` the string it was sent as, its `id` the one the body names) come back as sent | documented | `test_a_message_posted_to_a_chat_is_kept_as_sent_read_back_and_notified` | https://learn.microsoft.com/en-us/graph/api/chatmessage-post |
| A reply is `POST /teams/{id}/channels/{id}/messages/{id}/replies`, 201 with the new message, its `replyToId` the message replied to | documented | `test_a_channel_post_and_a_reply_round_trip_and_notify` | https://learn.microsoft.com/en-us/graph/api/chatmessage-post-replies |
| Application permissions are supported for posting only to import (migration); an application posting with no user is refused by name | documented | `test_what_graph_documents_no_answer_to_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/chatmessage-post |
| A post notifies a subscription on the chat's or the channel's messages with `changeType` `created` and the resource `chats('{id}')/messages('{id}')`, in a channel `teams('{id}')/channels('{id}')/messages('{id}')` (a reply by its own id: the page shows no other form) | documented | `test_a_message_posted_to_a_chat_is_kept_as_sent_read_back_and_notified`, `test_a_channel_post_and_a_reply_round_trip_and_notify` | https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage |
| `POST /chats` makes a `oneOnOne` or `group` chat from `members` (each `roles` `owner`, bound by `user@odata.bind`, the caller among them), 201 with the chat; a one-on-one chat that exists is returned, not made again; `topic` only on a group | documented | `test_a_one_on_one_chat_between_two_people_is_made_once_and_a_group_with_its_topic` | https://learn.microsoft.com/en-us/graph/api/chat-post |
| A chat's members are listed by `GET /chats/{id}/members` | documented | `test_a_one_on_one_chat_between_two_people_is_made_once_and_a_group_with_its_topic` | https://learn.microsoft.com/en-us/graph/api/chat-list-members |
| `joinedTeams` lists the teams the user is a direct member of, populating `id`, `displayName`, `description`, `isArchived` and `tenantId`; it supports no OData query parameter | documented | `test_joined_teams_and_the_primary_channel` | https://learn.microsoft.com/en-us/graph/api/user-list-joinedteams |
| `GET /teams/{id}/primaryChannel` is the team's General channel | documented | `test_joined_teams_and_the_primary_channel` | https://learn.microsoft.com/en-us/graph/api/team-get-primarychannel |

## Answered without a source (tracked, to be sourced or refused)

Each row is a behaviour still answered that no Microsoft page or public recording backs. They are kept because
refusing them would stop agents listing files, users and channels at all; each is to be replaced by a recorded
answer of the real service or refused by name, and the list may only shrink
(`tests/providers/test_claims_are_sourced.py`, `OPEN`).

| Claim | Class | Test | Source |
|---|---|---|---|
| A folder's children without `$orderby` list in the order the items were made | unsourced | `test_a_folders_children_are_listed_under_value` | none: driveitem-list-children documents `$orderby` and no default |
| Users without `$orderby` list in the directory's own order | unsourced | `test_users_by_id_principal_name_and_filter_with_select` | none: user-list documents no default order |
| A team's channels list General first, then in the order they were made | unsourced | `test_channels_by_name_and_their_messages_paged_with_replies` | none: channel-list documents no order |
| A user's chats, and `/chats`, list in the order the chats were made | unsourced | `test_chats_their_messages_and_members` | none: chat-list documents only `$orderby` on lastMessagePreview |
| `/sites?search=` lists matching sites in the order they were made | unsourced | `test_every_published_operation_is_served_or_refused_by_name` | none: site-search documents no order |
| A team, channel or chat the world does not hold is 404 `NotFound` | unsourced | `test_a_team_unknown_is_refused_in_graphs_shape` | none recorded |
| A user the directory does not hold is 404 `Request_ResourceNotFound` | unsourced | `test_a_disabled_user_cannot_sign_in_or_be_messaged_until_enabled_and_a_removed_one_is_gone` | none recorded |
| A user's token reaching another user's OneDrive, or renaming, moving or deleting a drive's root, is 403 `accessDenied` | unsourced | `test_another_users_onedrive_is_refused_to_a_user` | none recorded: OneDrive's error page lists the code, not the status it comes with |
| A name already taken in a folder is 409 `nameAlreadyExists` | unsourced | `test_put_by_path_makes_the_folders_and_honours_conflict_behaviour` | none recorded: OneDrive's error page lists the code, not the status it comes with |
| A drive, site, upload session, copy monitor or permission the world does not hold is 404 `itemNotFound` | unsourced | `test_a_missing_item_and_an_unserved_segment_are_refused` | none recorded beyond the item itself |
| An upload fragment out of order or mismatched is 416 or 400 `invalidRange`; simple upload content over 250 MB is 413 `requestTooLarge` | unsourced | `test_put_by_path_makes_the_folders_and_honours_conflict_behaviour` | none recorded |
| A drive or Teams `$skiptoken` or delta token Minutehand never gave, a folder used as a file, a missing name or role, an unknown link type or scope, and an unreadable files body are 400 `invalidRequest` | unsourced | `test_folders_move_copy_share_and_link` | none recorded: OneDrive's error page lists the code, not which requests earn it |

## Refused by name

Each of these is answered 501 `not_implemented`, naming the call and why, because Microsoft documents no answer to
it and no recording of the real service shows one:

- the connector: an update carrying both text and a card (the old emulator's 400 `BadSyntax`, kept only on its
  authors' word), a create naming another bot than the caller (its 400), details of a team the world does not hold
  (its 404);
- sign-in: authorize without `login_hint` (no browser asks who signs in), a code or refresh token naming no user;
- Graph: `/me` with no signed-in user; `GET /chats` with no signed-in user; a mailbox of someone who is no user of
  the tenant; `$top` past a page's documented bounds; a `$filter`, `$orderby`, `$expand`, `$search` or `$count`
  not served; a body content type other than text or html; an event without start and end; a `dateTime` that
  cannot be read; `getSchedule` with its end not after its start, an interval outside 5 to 1440, or an address
  with no mailbox in the tenant; `sendMail` without a message; the content of a folder; listing sites without
  `?search=`; delta on a folder that is not the drive's root; a list of messages or events without `$orderby` that
  would hold more than one; a folder the mailbox does not hold; an event ending before it starts; an organizer
  answering their own meeting; a request body that cannot be read (outside files); and every subscription refusal
  but the three documented above;
- Graph mail drafts: a MIME draft or a draft made in a folder, a change to a property of a message that is no draft,
  `send` of a message that is no draft, a forward of a message with attachments, a move or copy to a folder the
  mailbox does not hold, an attachment that is an item or a link, is 3 MB or more or is not base64,
  `createUploadSession` (its upload host and answers are not on the page), an attachment added to a message that is
  no draft, and a list of attachments without `$orderby`;
- Graph events: a recurrence of any type but daily and weekly, one in a time zone other than UTC, with a
  `startDate` other than the day the event starts on, or with a property its type does not take; a change to an
  event's recurrence; the instances of an event that is no series master; a change, deletion, cancellation, response
  or attachment on one occurrence of a series (an occurrence is read only); `proposedNewTime` on `decline`; an
  attachment added to an event by someone but its organizer, or an attachment that is no small file; and
  `delta` on `events` (documented for the beta version only).

Also refused by name (Teams posting): a message property this provider would not keep (`hostedContents`, `from`,
`createdDateTime`, `messageType`, `replyToId`, …), a body content type other than text or html, an importance
other than normal, high or urgent, a mention of a channel or team, a post by someone who is no member of the chat or
channel, a reply to a reply, a chat of another type than `group` or `oneOnOne`, with a member of another type or a
role other than `owner`, a group chat of fewer than three people, a one-on-one chat that is not between two people
or has a topic, a chat that leaves out its caller, and any query option on `joinedTeams` — Learn gives no answer to
them.

## Not carried over

Contradicted by Microsoft's documentation (the old emulator was wrong; this provider follows the page):

- **A reply-to-activity in a personal or group chat refused 400 `BadArgument`.** The connector reference says every
  channel supports Reply to Activity and that without nested replies it behaves like a send.
- **The General channel listed with the name "General".** Teams sends its name as null.
- **An activity of 40,000 characters refused 413 at a 28 KB limit.** The Teams page puts the limit at 100 KB of the
  message counted as UTF-16 (80 KB to be safe); this provider measures it that way.
- **Paged members one per page by default.** Teams documents a default of 200, a minimum of 50 and a maximum of 500.
- **`GET …/content` answered 200 with the bytes.** Graph answers 302 to a download URL.

Emulator artefacts with no counterpart in Microsoft's service: its `/health` route, its admin routes (clear,
reset, provisioning a conversation, simulating a message or a card press, reading stored messages), its fixed
fixture ids and secrets, an `expires_in` of exactly 3600, and its seeded folder names.
