# Microsoft provider — where its behaviour comes from

Every behaviour this provider keeps is Microsoft's: **documented** means the cited page says it; **observed** would
mean a recorded answer of the real service committed under `tests/data/`, and no row here is observed today. Each
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
| The subset holds 2,103 operations (method and path); 127 are served (`surface.SERVED`), every one of them in the subset | documented | `test_what_is_served_is_on_graphs_published_surface` | `tests/data/microsoft_graph_v1/openapi-subset-retrieved-2026-10-08.json` |
| The other 1,976 are refused by name, 501 `not_implemented`, before any surface reads them; a served read answers below 400 when called as its page documents | documented | `test_every_published_operation_is_served_or_refused_by_name` | `tests/data/microsoft_graph_v1/openapi-subset-retrieved-2026-10-08.json` |
| Two served reads the description leaves out are on their reference page: `GET /me/mailboxSettings/automaticRepliesSetting` and its `/users/{id}` form | documented | `test_a_person_away_shows_out_of_office_while_it_lasts_and_available_after` | https://learn.microsoft.com/en-us/graph/api/user-get-mailboxsettings |
| A path is matched in the forms Graph reads alike: `name('key')` key segments, a drive through `/me/drive`, `/users/{id}/drive` or `/sites/{id}/drive`, a site by `{host}:/{path}:`, an item by path, `delta` with or without `()` | documented | `test_every_published_operation_is_served_or_refused_by_name` | https://learn.microsoft.com/en-us/graph/onedrive-addressing-driveitems |
| A Graph error is `{"error": {"code", "message", "innerError"}}` | documented | `test_a_team_unknown_is_refused_in_graphs_shape` | https://learn.microsoft.com/en-us/graph/errors |

## Credentials: deliberately not enforced

Minutehand does not enforce credentials. Any token, credential or client secret works, every sign-in succeeds, and
no scope, role or permission grant refuses a call. These checks, each of which the identity platform, Graph or the
connector does make, were removed:

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

What still refuses is the world itself: a user an administrator disabled (AADSTS50057) or removed (AADSTS50034)
cannot sign in; a user's token reaches only that user's own mailbox, calendar and OneDrive; a bot reaches only the
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
| Delete Activity removes the activity and answers no body | documented | `test_a_deleted_activity_is_gone_from_the_conversation` | https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference |
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

Exchange's response codes are documented on one page, https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/responsecode
("RESPONSE_CODES" below); its HTTP status for each is the meaning Graph's error table gives
(https://learn.microsoft.com/en-us/graph/errors: 400 malformed or incorrect, 403 access denied, 404 not found).

| Claim | Class | Test | Source |
|---|---|---|---|
| `sendMail` answers 202 Accepted with no body, and saves the message in Sent Items | documented | `test_a_sent_email_is_one_copy_per_mailbox_and_the_senders_copy_asks_each_recipient` | https://learn.microsoft.com/en-us/graph/api/user-sendmail |
| A reply goes to the message's `replyTo` when it names any, else to its sender; so a reply from Sent Items goes to the mailbox itself | documented | `test_a_reply_goes_to_the_messages_reply_to_and_else_to_its_sender` | https://learn.microsoft.com/en-us/graph/api/message-reply |
| Reply-all goes to the sender (or `replyTo`) and every recipient of the message | documented | `test_a_reply_to_all_goes_to_the_sender_and_every_recipient` | https://learn.microsoft.com/en-us/graph/api/message-replyall |
| A comment and the message's body together are 400, nothing sent (the code is not documented and not pinned) | documented | `test_a_comment_and_a_body_together_are_refused_400` | https://learn.microsoft.com/en-us/graph/api/message-reply |
| `reply` answers 202 and the reply is in the message's conversation | documented | `test_a_persons_reply_lands_in_the_senders_inbox_in_the_conversation_and_is_notified` | https://learn.microsoft.com/en-us/graph/api/message-reply |
| A body is stored as sent and read as HTML unless `Prefer: outlook.body-content-type="text"` asks for text, which `Preference-Applied` confirms | documented | `test_a_reply_body_is_kept_as_sent`, `test_an_events_body_and_attendees_are_kept_as_sent`, `test_filter_orderby_top_and_paging_read_the_mailbox_as_clients_ask` | https://learn.microsoft.com/en-us/graph/api/user-list-calendarview |
| A message or event property the provider would not keep (`attachments`, `categories`, `isOnlineMeeting`, …) is refused by name and nothing is sent or made | documented | `test_message_properties_that_would_be_dropped_are_refused_by_name_and_nothing_is_sent`, `test_event_properties_that_would_be_dropped_are_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/resources/message |
| A message and an event carry `changeKey` and `@odata.etag` W/"changeKey", both renewed with every change | documented | `test_outlook_items_carry_their_change_key_as_a_weak_etag_and_both_change_with_them` | https://learn.microsoft.com/en-us/graph/api/message-get |
| Messages page 10 by default, `$top` 1 to 1000, the next page by `@odata.nextLink` | documented | `test_filter_orderby_top_and_paging_read_the_mailbox_as_clients_ask` | https://learn.microsoft.com/en-us/graph/api/user-list-messages |
| With `$filter` and `$orderby` together, the `$orderby` properties must open the `$filter` in order, else `InefficientFilter` | documented | `test_an_orderby_that_does_not_lead_the_filter_is_refused_inefficient_filter` | https://learn.microsoft.com/en-us/graph/api/user-list-messages |
| A folder's message `delta` pages to a `@odata.deltaLink`, and from it lists what changed, a removal as `@removed` | documented | `test_delta_lists_the_folder_then_only_what_arrived_and_what_left` | https://learn.microsoft.com/en-us/graph/api/message-delta |
| A well-known folder this provider does not hold (`junkemail`, …) is refused by name; an unknown folder is 404 `ErrorFolderNotFound` | documented | `test_a_well_known_folder_not_held_is_refused_by_name_and_an_unknown_one_is_not_found` | https://learn.microsoft.com/en-us/graph/api/resources/mailfolder and RESPONSE_CODES |
| A send with no recipient, or a recipient whose address is none, is 400 `ErrorInvalidRecipients` | documented | `test_a_send_with_no_recipient_or_a_bad_address_is_refused_invalid_recipients` | https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/responsecode |
| An event whose end is before its start is 400 `ErrorCalendarEndDateIsEarlierThanStartDate`; the organizer answering their own meeting is `ErrorCalendarIsOrganizerForAccept`, `…ForTentative`, `…ForDecline` | documented | `test_an_end_before_the_start_and_an_organizer_answering_are_refused_with_exchanges_codes` | https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/responsecode |
| A user's token reaching another user's mailbox or calendar is 403 `ErrorAccessDenied` (whose mailbox it is is the world's) | documented | `test_a_users_token_reaching_another_mailbox_is_refused_access_denied`, `test_an_invitation_asks_each_attendee_and_their_accept_lands_as_their_change` | https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/responsecode |
| `Prefer: outlook.timezone` other than UTC, and `IdType="ImmutableId"`, are refused by name; without them times are UTC | documented | `test_a_preference_that_would_change_the_answer_unserved_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/user-list-calendarview |
| An Outlook message subscription lasts at most 10,080 minutes | documented | `test_a_mail_subscription_past_seven_days_or_on_a_folder_that_is_none_is_refused` | https://learn.microsoft.com/en-us/graph/api/resources/subscription |
| An event made with attendees sends them an invitation carrying the event's body; an attendee's response is in `attendees[].status` | documented | `test_an_invitation_asks_each_attendee_and_their_accept_lands_as_their_change` | https://learn.microsoft.com/en-us/graph/api/user-post-events |
| An event's attendees are kept as sent, address and name | documented | `test_an_events_body_and_attendees_are_kept_as_sent` | https://learn.microsoft.com/en-us/graph/api/resources/attendee |
| `calendarView` without `startDateTime` and `endDateTime` (both required, no answer documented) is refused by name | documented | `test_a_calendar_view_without_a_window_is_refused_by_name` | https://learn.microsoft.com/en-us/graph/api/user-list-calendarview |
| A POST repeated with the same `transactionId` makes the event once | documented | `test_an_event_retried_with_its_transaction_id_is_made_once` | https://learn.microsoft.com/en-us/graph/api/resources/event |
| `getSchedule`'s `availabilityView` is one digit an interval: 0 free, 1 tentative, 2 busy, 3 out of office; an address that is no mailbox answers `ErrorMailRecipientNotFound` in its schedule | documented | `test_the_calendar_view_and_free_busy_read_every_calendar_and_its_answers` | https://learn.microsoft.com/en-us/graph/api/calendar-getschedule and RESPONSE_CODES |
| A person's automatic reply is the reason the scenario gives, as written | documented | `test_a_person_away_shows_out_of_office_while_it_lasts_and_available_after` | https://learn.microsoft.com/en-us/graph/api/resources/automaticrepliessetting |

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
  cannot be read; `getSchedule` with its end not after its start or an interval outside 5 to 1440; `sendMail`
  without a message; the content of a folder; listing sites without `?search=`; delta on a folder that is not the
  drive's root.

## Answered, not yet sourced

These are still answered as they are; no page says them and no recording shows them. Each is to be replaced by a
recorded answer of the real service or refused by name:

- the connector's Delete Activity answers 200 (its page says only "an HTTP status code" with no body), and its
  400 refusals spell their code `BadArgument` (Teams' table spells it `Bad Argument`);
- a reply's body is the comment alone, where Outlook also quotes the original; a response to an invitation is
  subjected "Accepted:", "Tentative:" or "Declined:" with the event's subject;
- messages and events list newest-received and soonest-starting first when no `$orderby` is sent;
- a mailbox's well-known folders' display names, an Outlook item's `internetMessageId`, and the subscriptions
  surface's refusal codes (`InvalidRequest`, `ExtensionError`, `ResourceNotFound`);
- an unreadable request body is 400 `invalidRequest`.

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
