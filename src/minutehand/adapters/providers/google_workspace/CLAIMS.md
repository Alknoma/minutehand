# Google Workspace (Drive, Docs, Slides, Gmail, Calendar): where each behaviour comes from

For Drive, Docs and Slides: facts about the real services that an older, separately written stand-in for Drive was
tested against, carried here so the fake keeps them. For Gmail and Calendar: what this fake was written from. **Documented** means Google's public reference says so (the page is linked; read it
there). **Observed** means no page says so and the fact rests on someone having seen the real service do it; the
row links the recorded evidence (a report holding the real answer, or a capture under `tests/data/`). A row may name
nothing else: a behaviour that is neither documented nor observed is not kept, and what is not served yet is refused
by name. `tests/providers/test_claims_are_sourced.py` holds every row to this, and lists the rows still unsourced.

Tests named `vendor_claims` live in `tests/providers/google_workspace/test_google_workspace_vendor_claims.py` (Drive) and
`test_google_workspace_vendor_claims_docs_and_slides.py` (Docs, Slides). A claim an earlier test already pinned names
that test instead.

## Drive v3

| Claim | Class | Test | Source |
|---|---|---|---|
| A shared-drive file is a 404 `notFound` unless the call sets `supportsAllDrives` | documented | `test_a_shared_drive_file_is_not_found_without_supports_all_drives_is_refused_404` | https://developers.google.com/workspace/drive/api/guides/enable-shareddrives |
| Creating under a shared-drive folder without `supportsAllDrives` is a 404 | documented | `test_creating_in_a_shared_drive_folder_without_supports_all_drives_is_refused_404` | https://developers.google.com/workspace/drive/api/guides/enable-shareddrives |
| A listing shows shared-drive files only with `supportsAllDrives` and `includeItemsFromAllDrives` | documented | `test_a_listing_shows_shared_drive_files_only_with_both_flags` | https://developers.google.com/workspace/drive/api/guides/enable-shareddrives |
| `driveId` is output-only: a create in a shared drive needs only the parent and the flag | documented | `test_a_create_in_a_shared_drive_needs_no_drive_id_in_its_body` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files |
| Creating under a parent that does not exist is a 404 naming the parent | documented | `test_drive_refusals.py::test_creating_under_an_unknown_parent_is_refused_404` | https://developers.google.com/workspace/drive/api/guides/handle-errors |
| `parents` in an update body is refused `fieldNotWritable`; moves go through `addParents`/`removeParents` | documented | `test_drive_refusals.py::test_parents_in_an_update_body_is_refused_403_field_not_writable` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files |
| `files/root` answers My Drive's own id, never the alias `root` | observed | `test_drive_api.py::test_root_answers_with_my_drives_own_id` | https://stackoverflow.com/a/56213902, https://stackoverflow.com/a/64497289 (`files().get(fileId='root').execute()['id']` read as the folder's id) |
| A listing answers one page and a `nextPageToken` for the rest | documented | `test_drive_api.py::test_a_listing_takes_pages_until_the_token_runs_out` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list |
| `pageSize` reaches 1000, and a larger folder still hands back a token | documented | `test_a_page_of_a_thousand_is_answered_whole_with_a_token_for_the_rest` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list |
| A field mask on a listing narrows each file to the fields named | documented | `test_a_listing_answers_only_the_file_fields_asked_for` | https://developers.google.com/workspace/drive/api/guides/fields-parameter |
| `mimeType = '...'` leaves folders out | documented | `test_a_mime_type_filter_leaves_folders_out` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| `trashed = true` lists only the trash, and nothing when nothing is trashed | documented | `test_trashed_true_lists_only_what_is_in_the_trash` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| An unescaped apostrophe inside a quoted value is a 400 `invalid` | documented | `test_drive_refusals.py::test_a_malformed_q_is_refused_400_invalid` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| An apostrophe escaped as `\'` finds the name that holds it | documented | `test_an_escaped_apostrophe_finds_the_name_that_holds_one` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| A `q` Drive cannot read (an unknown term) is a 400 `invalid` "Invalid Value" located at `q`, naming no term | observed | `test_an_unknown_query_term_is_refused_400_invalid_value_on_q` | https://stackoverflow.com/q/67608827, https://stackoverflow.com/q/69699515 |
| `name contains` is prefix matching (documented); the start of a later word counts as a prefix (observed) | documented | `test_name_contains_matches_a_prefix_of_a_word_never_a_fragment_inside_one` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms; https://stackoverflow.com/q/71011364 |
| Exporting a file that is not a Workspace document is a 403 `fileNotExportable` | documented | `test_drive_refusals.py::test_exporting_a_file_that_is_not_a_docs_file_is_refused_403_file_not_exportable` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export |
| An export over 10 MB is refused (documented) as a 403 `exportSizeLimitExceeded` (observed) | documented | `test_exporting_a_doc_over_ten_megabytes_is_refused_403_export_size_limit_exceeded` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export; https://github.com/googleapis/google-api-ruby-client/issues/906 |
| `alt=media` on a Google Doc is a 403 `fileNotDownloadable`; a Doc is read by export | documented | `test_drive_refusals.py::test_downloading_a_google_doc_with_alt_media_is_refused_403_file_not_downloadable` | https://developers.google.com/workspace/drive/api/guides/manage-downloads |
| A permission role outside owner/organizer/fileOrganizer/writer/commenter/reader (`editor`) is a 400 | documented | `test_drive_refusals.py::test_a_permission_drive_would_not_grant_is_refused_400` | https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions |
| A `user` grant without `emailAddress` is a 400 `invalidSharingRequest` | documented | `test_drive_refusals.py::test_a_permission_drive_would_not_grant_is_refused_400` | https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions |
| An `anyone`/`reader` link share needs no address and is granted | documented | `test_a_link_share_to_anyone_as_reader_is_granted` | https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions |
| A deleted file leaves every listing, trash included, and is then a 404 | documented | `test_a_deleted_file_leaves_every_listing` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/delete |
| Text uploaded with the Docs type becomes a Doc that `documents.get` reads as paragraphs | documented | `test_drive_api.py::test_a_created_doc_reads_through_the_docs_api_as_paragraphs_of_text_runs` | https://developers.google.com/workspace/drive/api/guides/manage-uploads |
| A Doc's plain-text export and its Docs body hold the same characters | unsourced | `test_an_uploaded_doc_reads_the_same_through_export_and_the_docs_api` | no page or recorded answer found |
| A plain-text export opens with a byte-order mark (observed); it ends lines CRLF (seen only indirectly) | observed (the mark) / unsourced (CRLF) | `test_drive_api.py::test_a_created_doc_exports_as_text_with_a_bom_and_crlf` | https://stackoverflow.com/a/38242332; CRLF only by https://stackoverflow.com/q/33622589 |
| A Doc created with no text is a section break and one newline ending at 2, without its name in it | observed (the section break first) / unsourced (the one newline) | `test_a_doc_created_with_no_text_is_its_final_newline_and_nothing_else` | https://stackoverflow.com/q/75662537 |
| A CSV uploaded with the Sheets type exports its rows as text/csv | documented | `test_a_csv_uploaded_as_a_sheet_exports_its_rows_as_csv` | https://developers.google.com/workspace/drive/api/guides/manage-uploads |
| A resumable upload may begin without `X-Upload-Content-Length`; the client library leaves it out when it cannot measure the stream | documented | `test_drive_google_client.py::test_a_resumable_upload_of_unknown_length_arrives_whole` | https://developers.google.com/workspace/drive/api/guides/manage-uploads#resumable |
| A chunk of an unknown total is `bytes a-b/*`, answered 308 with `Range: bytes=0-b`; the chunk naming the total completes the upload, 200 with the file | documented | `test_drive_resumable_upload.py::test_chunks_of_an_unknown_total_are_answered_308_until_the_last_names_it` | https://developers.google.com/workspace/drive/api/guides/manage-uploads#uploading |
| An empty `PUT` with `Content-Range: */*` (or `*/total`) asks how much arrived: 308 with no `Range` before any byte | documented | `test_drive_resumable_upload.py::test_a_status_query_before_any_byte_is_answered_308_with_no_range` | https://developers.google.com/workspace/drive/api/guides/manage-uploads#resume-upload |
| An interrupted upload resumes from the byte after the `Range` its status query answered; once complete, a status query answers 200 with the file | documented | `test_drive_resumable_upload.py::test_an_interrupted_upload_asks_where_it_stands_and_resumes_from_the_range` | https://developers.google.com/workspace/drive/api/guides/manage-uploads#resume-upload |
| A chunk that does not start where the received bytes end, or names a total other than the one declared, is a 400 `badContent` | unsourced | `test_drive_resumable_upload.py::test_a_chunk_that_skips_bytes_is_refused_400` | no page or recorded answer found |

## Docs v1

| Claim | Class | Test | Source |
|---|---|---|---|
| Indexes are UTF-16 code units: a style range after an emoji lands on its word | documented | `test_a_style_range_after_an_emoji_is_counted_in_utf16_and_lands_on_its_word` | https://developers.google.com/workspace/docs/api/concepts/structure |
| An insertion between the two halves of a surrogate pair is a 400 | documented | `test_docs_batch_update.py::test_indexes_count_utf16_code_units` | https://developers.google.com/workspace/docs/api/concepts/structure |
| Inserting at index 0 is a 400 `INVALID_ARGUMENT` naming `insertText` | documented | `test_inserting_at_index_zero_is_refused_400_invalid_argument` | https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#inserttextrequest |
| Inserting at the body's end index is a 400 naming the end index | documented | `test_inserting_at_the_bodys_end_index_is_refused_400` | https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#inserttextrequest |
| Inserting one short of the end index appends to the document | documented | `test_appending_just_before_the_final_newline_lands_at_the_end` | https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#inserttextrequest |
| `documents.create` makes a blank document; the title is not written into it | documented | `test_documents_create_makes_a_blank_document_not_one_holding_its_title` | https://developers.google.com/workspace/docs/api/how-tos/documents |

## Slides v1

| Claim | Class | Test | Source |
|---|---|---|---|
| A new presentation holds one slide and its title | unsourced | `test_drive_through_proxy.py::test_a_deck_made_through_drive_is_built_by_slides_batch_update` | the reference says only "Creates a blank presentation" |
| `deleteObject` on a slide removes it | documented | `test_drive_through_proxy.py::test_a_deck_made_through_drive_is_built_by_slides_batch_update` | https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#deleteobjectrequest |
| `createSlide` takes the caller's id, lands at its index and carries its layout's placeholders | documented | `test_a_slide_made_with_a_layout_lands_at_its_index_with_that_layouts_placeholders` | https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#createsliderequest |
| Text inserted into a placeholder's id reads back in that placeholder | documented | `test_text_inserted_into_a_slides_placeholders_reads_back_in_each` | https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#inserttextrequest |
| Speaker notes are written at the notes page's `speakerNotesObjectId` | documented | `test_speaker_notes_are_written_through_the_notes_pages_speaker_notes_object_id` | https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations.pages#notesproperties |
| A created shape holds its own text and fill, apart from the body placeholder | documented | `test_a_shape_keeps_its_own_text_and_fill_apart_from_the_body` | https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#createshaperequest |
| A created table's cells hold the text inserted at their `cellLocation`, apart from the body | documented | `test_a_tables_cells_hold_their_own_text_apart_from_the_body` | https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#createtablerequest |

## Not carried over

- **`alt=media` serving a Google Doc.** The older stand-in answered 200 to a media download of a Doc. Google's
  download guide says a Workspace document is read by `files.export`, and this fake refuses the download with a 403
  `fileNotDownloadable`. A client that downloaded Docs with `alt=media` was relying on a behaviour Drive does not
  have.
- **`fieldNotWritable` as a 400 — observed by the old emulator, not adopted.** The older stand-in refused `parents`
  in an update body with a 400. No public page states the status, so this fake keeps its 403 with the same reason
  (`test_parents_in_an_update_body_is_refused_403_field_not_writable`).
- **Placeholder ids derived from the slide's id** (`<slide>_title`, `<slide>_body`, `<slide>_notes_body`). Slides
  mints placeholder ids unless the caller maps them; a client reads them from `presentations.get`.
- **File text in the JSON body of `files.create`** (`content`, `_media_content`). Drive takes a file's bytes through
  the upload endpoint only.
- **A health route, and admin routes that seed files or reset the store.** Drive has neither; a scenario seeds this
  fake.

## Gmail v1

Tests are in `tests/providers/google_workspace/test_gmail_through_proxy.py`, each driving Google's own client in a
process of its own through the proxy, and `tests/e2e/test_mail_and_calendar_run.py`, a whole run.

| Claim | Class | Test | Source |
|---|---|---|---|
| `users.messages.send` takes the whole RFC 2822 message, base64url, in `raw`, and answers its `id`, `threadId` and `labelIds` (`SENT`) | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/guides/sending |
| A send joins a thread only when it names the `threadId` and its `Subject` matches the thread's; otherwise it starts a thread of its own | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/guides/threads |
| A message arriving in a mailbox joins the thread holding the message its `In-Reply-To` or `References` names, when the subjects match | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up`, `test_search_operators_find_the_seeded_mail_they_name` | https://developers.google.com/workspace/gmail/api/guides/threads (the rule for a message to join a thread) |
| A `From` that is not the account's own address is replaced (observed); a send without `Date` or `Message-ID` is given them (unsourced) | observed (From) / unsourced (Date, Message-ID) | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://stackoverflow.com/a/64099147 |
| A message sent to another account in the domain is a second message, with its own id, in that account's mailbox, labelled `INBOX` and `UNREAD` | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/guides/labels |
| A send with no recipient is a 400 `invalidArgument` "Recipient address required"; one with no `raw` a 400 naming `raw` | observed | `test_a_bad_id_another_mailbox_no_recipient_and_an_account_without_mail_are_refused` | https://github.com/Byron/google-apis-rs/issues/532, https://github.com/googleapis/google-api-nodejs-client/issues/979 |
| `users.messages.list` answers `{id, threadId}` a page at a time with `resultSizeEstimate`, newest first, leaving out spam and trash unless asked | documented | `test_search_operators_find_the_seeded_mail_they_name` | https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list |
| `q` takes Gmail's search operators: `from:`, `to:`, `subject:`, `is:unread`, `is:read`, `in:sent`, `newer_than:`, `OR`, `-`, words and quoted phrases; `from:me` is the account | documented | `test_search_operators_find_the_seeded_mail_they_name` | https://support.google.com/mail/answer/7190 |
| `format` is `full` (the MIME tree with each part's bytes base64url), `metadata` (headers, filtered by `metadataHeaders`), `minimal` or `raw` | documented | `test_search_operators_find_the_seeded_mail_they_name`, `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/reference/rest/v1/Format |
| `snippet` is the start of the text, HTML-escaped (`&#39;`) | observed | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://stackoverflow.com/q/67262471 |
| `users.threads.get` answers the thread's messages oldest first; `users.threads.list` a thread once however many of its messages match | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads |
| `users.history.list` answers what changed after `startHistoryId` (`messagesAdded`, `labelsAdded`, `labelsRemoved`, `messagesDeleted`), narrowed by `historyTypes`, and the mailbox's current `historyId` | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/guides/sync |
| A message is marked read by removing `UNREAD` with `users.messages.modify` | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/guides/labels |
| A label the mailbox does not have is a 400 `invalidArgument` "Invalid label: X" | observed | `test_an_unknown_label_on_a_held_message_is_refused` | https://stackoverflow.com/q/68714632 |
| An id that is not one is a 400 "Invalid id value"; one not in the mailbox a 404 "Requested entity was not found." | observed | `test_a_bad_id_another_mailbox_no_recipient_and_an_account_without_mail_are_refused` | https://stackoverflow.com/q/62656819, https://stackoverflow.com/q/72997848 |
| Another account's mailbox is a 403 `forbidden` "Delegation denied for X" | observed | `test_a_bad_id_another_mailbox_no_recipient_and_an_account_without_mail_are_refused` | https://stackoverflow.com/q/26135310 |
| An account with no mailbox (a service account impersonating nobody) is a 400 `failedPrecondition` "Precondition check failed." | observed | `test_a_bad_id_another_mailbox_no_recipient_and_an_account_without_mail_are_refused` | https://stackoverflow.com/q/66025277 |
| `users.getProfile` answers the address, the message and thread counts and the `historyId` to start a sync from | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile |
| `users.labels.list` names the system labels (`INBOX`, `SENT`, `UNREAD`, `STARRED`, `TRASH`, ...) | documented | `test_a_sent_question_is_answered_in_its_thread_found_by_history_marked_read_and_followed_up` | https://developers.google.com/workspace/gmail/api/guides/labels |

## Calendar v3

Tests are in `tests/providers/google_workspace/test_calendar_through_proxy.py` (T),
`test_calendar_sharing_and_conditions.py` (S) and `test_calendar_discovery.py` (D), each driving Google's own client
in a process of its own through the proxy, and `tests/e2e/test_mail_and_calendar_run.py`, a whole run.

The surface is Google's discovery document, committed as `tests/data/google_calendar_v3/discovery-retrieved-2026-10-08.json`
(revision 20261002): every method in it is served or answers 501 naming it (`calendars.REFUSED`), and every
parameter it gives a served method is served or answers 501 naming it (`calendars.SERVED`).

| Claim | Class | Test | Source |
|---|---|---|---|
| Every method and parameter of the discovery document is served or refused 501 by name | documented | D `test_every_documented_method_is_served_or_refused_by_name_at_its_documented_path`, D `test_a_served_method_serves_or_refuses_exactly_its_documented_parameters`, D `test_each_refused_method_and_parameter_answers_501_naming_it_to_googles_own_client` | https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest |
| A parameter the document does not name is refused 501 naming it: Google's answer to one is not recorded | documented | D `test_a_parameter_the_document_does_not_name_is_refused_by_name_and_answers_are_indented` | https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest (the parameters each method takes) |
| `prettyPrint`, true unless false, "Returns response with indentations and line breaks": two spaces a level | observed | D `test_a_parameter_the_document_does_not_name_is_refused_by_name_and_answers_are_indented` | `tests/data/google_calendar_v3/unauthenticated-calendar-list-2026-10-08.txt` |
| `calendarList.list` answers the account's primary calendar, whose id is its address, with its time zone | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://developers.google.com/workspace/calendar/api/v3/reference/calendarList |
| `calendars.get` answers `primary` or a calendar the caller holds a role on as `calendar#calendar` with `etag`, `id`, `summary`, `timeZone` | documented | S `test_calendars_get_answers_primary_and_a_calendar_the_caller_cannot_see_is_not_found` | https://developers.google.com/workspace/calendar/api/v3/reference/calendars/get |
| A calendar the caller cannot see is a 404 `notFound`, for `calendars.get`, events and watches alike | documented | S `test_calendars_get_answers_primary_and_a_calendar_the_caller_cannot_see_is_not_found`, S `test_a_secondary_calendar_shared_with_a_writer_is_theirs_to_add_and_write_and_hidden_from_others` | https://developers.google.com/workspace/calendar/api/guides/errors#404_not_found |
| `calendars.insert` makes a secondary calendar whose `dataOwner` is the caller, "added to the creator's calendar list" | documented | S `test_a_secondary_calendar_shared_with_a_writer_is_theirs_to_add_and_write_and_hidden_from_others` | https://developers.google.com/workspace/calendar/api/concepts/events-calendars; https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert |
| `acl.insert` grants a `user` scope a role; an owner may ("the additional ability to modify access levels of other users"); `acl.list` answers the rules as `calendar#acl` to a writer or owner ("can also see ACLs") | documented | S `test_a_secondary_calendar_shared_with_a_writer_is_theirs_to_add_and_write_and_hidden_from_others` | https://developers.google.com/workspace/calendar/api/v3/reference/acl; https://developers.google.com/workspace/calendar/api/concepts/sharing |
| "Sharing a calendar with a user no longer automatically inserts the calendar into their CalendarList": the grantee adds it with `calendarList.insert` | documented | S `test_a_secondary_calendar_shared_with_a_writer_is_theirs_to_add_and_write_and_hidden_from_others` | https://developers.google.com/workspace/calendar/api/concepts/sharing |
| A writer "can read and write events on the calendar"; an event written on a secondary calendar has that calendar as its organizer, `self` there | documented | S `test_a_secondary_calendar_shared_with_a_writer_is_theirs_to_add_and_write_and_hidden_from_others` | https://developers.google.com/workspace/calendar/api/concepts/sharing; https://developers.google.com/workspace/calendar/api/v3/reference/events ("Whether the organizer corresponds to the calendar on which this copy of the event appears") |
| `events.insert` takes guests in `attendees`, each `needsAction` until they answer; the organizer is `self` on their own calendar | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://developers.google.com/workspace/calendar/api/v3/reference/events |
| A `dateTime` without an offset is read in its `timeZone`, and is answered with its offset; one with an offset is answered as written | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://developers.google.com/workspace/calendar/api/v3/reference/events |
| An attendee's `responseStatus` is `needsAction`, `declined`, `tentative` or `accepted`; a guest may answer with a `comment` | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://developers.google.com/workspace/calendar/api/v3/reference/events |
| An invitation offers its guest Yes, Maybe and No | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://support.google.com/calendar/answer/37135 |
| `events.list` bounds an event's end by `timeMin` and its start by `timeMax`; `orderBy=startTime` needs `singleEvents=true` | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://developers.google.com/workspace/calendar/api/v3/reference/events/list |
| `showDeleted=true` includes deleted events as `cancelled`; on the organizer's calendar "cancelled events continue to expose event details"; elsewhere only `id` is guaranteed | documented | S `test_show_deleted_answers_a_deleted_event_in_the_window_as_cancelled_and_get_serves_it` | https://developers.google.com/workspace/calendar/api/v3/reference/events/list; https://developers.google.com/workspace/calendar/api/v3/reference/events (`status`) |
| With `updatedMin`, "entries deleted since this time will always be included regardless of showDeleted" | documented | S `test_show_deleted_answers_a_deleted_event_in_the_window_as_cancelled_and_get_serves_it` | https://developers.google.com/workspace/calendar/api/v3/reference/events/list |
| `events.get` on a deleted event answers it `cancelled`: "The get method always returns them" | documented | S `test_show_deleted_answers_a_deleted_event_in_the_window_as_cancelled_and_get_serves_it` | https://developers.google.com/workspace/calendar/api/v3/reference/events (`status`) |
| A deleted event deleted again is a 410 `deleted` "Resource has been deleted" | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event`, S `test_show_deleted_answers_a_deleted_event_in_the_window_as_cancelled_and_get_serves_it` | https://developers.google.com/workspace/calendar/api/guides/errors#410_gone |
| `events.patch`, `events.update` and `events.delete` with `If-Match` proceed only while the etag matches; otherwise a 412 `conditionNotMet` "Precondition Failed" at header `If-Match`, and nothing changes | documented | S `test_a_write_naming_a_stale_etag_is_refused_412_and_changes_nothing` | https://developers.google.com/workspace/calendar/api/guides/version-resources; https://developers.google.com/workspace/calendar/api/guides/errors |
| Every change gives the event a new etag ("the new version of the resource with the new etag"), a guest's answer landing included | documented | S `test_a_write_naming_a_stale_etag_is_refused_412_and_changes_nothing` | https://developers.google.com/workspace/calendar/api/guides/version-resources |
| `freeBusy.query` answers each calendar's busy ranges, a calendar it cannot find as an `errors` entry `notFound`, and a `transparent` event as free | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query |
| `events.patch` sets what the body names, a list as a whole; a guest keeps the answer they gave | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://developers.google.com/workspace/calendar/api/guides/performance#patch |
| A sync token answers every event changed since, a deleted one as `cancelled`, and cannot be combined with `timeMin`, `q` or `orderBy` | documented | T `test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event` | https://developers.google.com/workspace/calendar/api/guides/sync |
| An end at or before the start is a 400 `timeRangeEmpty` "The specified time range is empty." | documented | T `test_an_empty_range_another_calendar_a_guests_change_a_taken_id_and_recurrence_are_refused` | https://developers.google.com/workspace/calendar/api/guides/errors; https://github.com/ridafkih/keeper.sh/issues/614 (on insert) |
| A guest changing an event's shared properties is a 403 `forbiddenForNonOrganizer` | documented | T `test_an_empty_range_another_calendar_a_guests_change_a_taken_id_and_recurrence_are_refused` | https://developers.google.com/workspace/calendar/api/guides/errors#403_forbidden_for_non_organizer |
| A client-chosen event id already used is a 409 `duplicate` | documented | T `test_an_empty_range_another_calendar_a_guests_change_a_taken_id_and_recurrence_are_refused` | https://developers.google.com/workspace/calendar/api/guides/errors#409_the_requested_identifier_already_exists |

## Calendar v3 push notifications

Tests are in `tests/providers/google_workspace/test_calendar_push.py`, each driving Google's own client
(`events().watch`, `channels().stop`) in a process of its own through the proxy, told to an HTTPS receiver of the
test's whose certificate the run's CA signed. The machinery is Drive's `changes.watch` channel's, shared
(`channels.py`).

| Claim | Class | Test | Source |
|---|---|---|---|
| `POST /calendar/v3/calendars/{calendarId}/events/watch` takes `id`, `type` (`web_hook` or `webhook`), `address`, and optionally `token` and `params.ttl`, and answers a Channel: `kind` `api#channel`, `id`, `resourceId`, `resourceUri`, `token`, `expiration` in epoch milliseconds | documented | `test_a_watch_is_told_sync_then_exists_for_the_agents_own_change_and_a_guests_answer` | https://developers.google.com/workspace/calendar/api/v3/reference/events/watch |
| The address "must use HTTPS"; an `http://` one is a 400 "WebHook callback must be HTTPS: <address>", whose envelope names no reason since no recorded answer shows one | observed | `test_an_http_address_a_past_expiration_and_a_ttl_that_is_no_number_are_refused` | https://developers.google.com/workspace/calendar/api/guides/push; https://stackoverflow.com/q/43484709, https://github.com/janeczku/calibre-web/issues/502 |
| A push verifies the receiver's certificate ("a valid SSL certificate"); one the run does not trust is not reached | documented | `test_a_receiver_whose_certificate_the_run_does_not_trust_is_not_reached` | https://developers.google.com/workspace/calendar/api/guides/push |
| A watch may ask an `expiration`; where Google has a limit of its own "the more restrictive value is used", as it is between an `expiration` and a `params.ttl` | documented | `test_a_channel_past_its_expiry_is_told_nothing_more` | https://developers.google.com/workspace/calendar/api/guides/push |
| A channel lives `params.ttl` seconds, "Default is 604800 seconds" | documented | `test_a_watch_is_told_sync_then_exists_for_the_agents_own_change_and_a_guests_answer` | https://developers.google.com/workspace/calendar/api/v3/reference/events/watch |
| A longer `ttl` is cut to 30 days | observed | `test_a_channel_past_its_expiry_is_told_nothing_more` | https://stackoverflow.com/q/64986662, https://stackoverflow.com/a/65001852 |
| `calendarId` is `primary` or a calendar id the caller holds a role on, a secondary one included; any other is a 404 `notFound` | documented | `test_a_reused_channel_id_and_another_accounts_calendar_are_refused`, `test_a_watch_on_a_secondary_calendar_is_told_of_its_events_and_names_it_encoded` | https://developers.google.com/workspace/calendar/api/v3/reference/events/watch; https://developers.google.com/workspace/calendar/api/guides/errors#404_not_found |
| A channel id "uniquely identifies this new notification channel within your project": an id already taken, by a Calendar or a Drive channel, even stopped, is refused "Channel id X not unique" | observed | `test_a_reused_channel_id_and_another_accounts_calendar_are_refused` | https://developers.google.com/workspace/calendar/api/guides/push; https://github.com/googleapis/google-api-nodejs-client/issues/774 |
| That refusal's status and reason (400 `channelIdNotUnique`, kept from the Drive fake) | unsourced | `test_a_reused_channel_id_and_another_accounts_calendar_are_refused` | no recorded answer shows them |
| Right after a watch, Calendar sends a `sync` message, whose `X-Goog-Message-Number` is always 1 | documented | `test_a_watch_is_told_sync_then_exists_for_the_agents_own_change_and_a_guests_answer` | https://developers.google.com/workspace/calendar/api/guides/push |
| Then `exists` when "There was a change to a resource", by anyone: the agent's own change, another's, a guest's answer at its moment | documented | `test_a_watch_is_told_sync_then_exists_for_the_agents_own_change_and_a_guests_answer`, `test_message_numbers_rise_on_each_channel_and_the_token_goes_only_where_it_was_given` | https://developers.google.com/workspace/calendar/api/guides/push (the rule names no exception) |
| Each delivery is a POST with no body, carrying `X-Goog-Channel-ID`, `X-Goog-Message-Number`, `X-Goog-Resource-ID`, `X-Goog-Resource-State`, `X-Goog-Resource-URI`, and `X-Goog-Channel-Expiration` and `X-Goog-Channel-Token` "Only present if defined" | documented | `test_a_watch_is_told_sync_then_exists_for_the_agents_own_change_and_a_guests_answer` | https://developers.google.com/workspace/calendar/api/guides/push |
| Message numbers "increase for each subsequent message on the channel"; two channels on one calendar carry the same opaque `X-Goog-Resource-ID` | documented | `test_message_numbers_rise_on_each_channel_and_the_token_goes_only_where_it_was_given` | https://developers.google.com/workspace/calendar/api/guides/push |
| `resourceUri` reads `https://www.googleapis.com/calendar/v3/calendars/<calendarId as asked, percent-encoded>/events?alt=json` | observed | `test_a_watch_on_a_secondary_calendar_is_told_of_its_events_and_names_it_encoded` | https://stackoverflow.com/q/74562159 |
| `POST /calendar/v3/channels/stop` takes the channel's `id` and `resourceId` and answers an empty body | documented | `test_after_a_stop_nothing_more_is_told_and_each_api_stops_only_its_own_channels` | https://developers.google.com/workspace/calendar/api/v3/reference/channels/stop |
| Stopping an unknown channel, or one named with another `resourceId`, is a 404 `notFound` "Channel 'X' not found for project 'P'" | observed | `test_after_a_stop_nothing_more_is_told_and_each_api_stops_only_its_own_channels` | https://stackoverflow.com/q/34618420 |
| Stopping a channel already stopped is that 404 too; a Drive channel named to Calendar's `channels.stop`, or the reverse, is not found | unsourced | `test_after_a_stop_nothing_more_is_told_and_each_api_stops_only_its_own_channels` | a second stop was seen to succeed before the 404 came (https://github.com/googleapis/google-api-nodejs-client/issues/774); nothing is recorded across APIs |
| A stopped channel, or one past its expiration, is sent nothing more | documented | `test_after_a_stop_nothing_more_is_told_and_each_api_stops_only_its_own_channels`, `test_a_channel_past_its_expiry_is_told_nothing_more`, `test_an_expired_channel_makes_a_guests_answer_unheard` | https://developers.google.com/workspace/calendar/api/guides/push |
| A push answered 500, 502, 503 or 504 is retried "with exponential backoff"; this fake records it and does not retry, since no schedule is documented or recorded | unsourced | `test_an_unreachable_or_failing_address_is_recorded_and_the_run_goes_on` | https://developers.google.com/workspace/calendar/api/guides/push documents the retry, not when |

Google lets "only the same user from the same client" stop a channel a user made
(https://developers.google.com/workspace/calendar/api/guides/push). Minutehand does not enforce Google's caller and
credential rules, by a rule of the product, not a reading of Google's: any signed-in caller may stop a channel.

## Gmail and Calendar: refused by name

- **Gmail's `users.watch`** answers 501. It delivers through Cloud Pub/Sub, a topic the app subscribes to
  (https://developers.google.com/workspace/gmail/api/guides/push), not a webhook channel, and this fake has no
  Pub/Sub: an agent finds new mail by polling (`users.history.list`).
- **Drafts, user labels, filters, settings, `messages.insert`, `import`, `trash`, `delete`, `batchModify`,
  attachments by `attachmentId`, the `/upload/` media path, and `has:`, `filename:`, `size:`, `category:` or
  grouped search terms** answer 501.
- **Calendar methods** the discovery document has and this fake does not serve, each answered 501 naming it:
  `calendars.REFUSED`. **Parameters** of served methods not served yet, likewise: `calendars.SERVED`.
- **Event fields** `conferenceData`, `attachments`, `gadget`, `outOfOfficeProperties`, `focusTimeProperties`,
  `workingLocationProperties`, `birthdayProperties`, `eventLabelId`, `recurringEventId`, `originalStartTime`,
  `recurrence`, an `eventType` other than `default` and an `iCalUID` other than the event's answer 501 naming them.
  Every other writable field is kept as written and answered as kept; read-only ones in a body are ignored.
- **ACL roles** other than `writer` and `owner`, **scopes** other than `user`, and an ACL change by a caller who is
  not an owner answer 501: Google's answers to them are not recorded here.
- **A push channel's** past `expiration`, non-numeric `ttl` and `payload` answer 501.

## Gmail and Calendar: still unsourced

- `sendUpdates` and `acl.insert`'s `sendNotifications` are served without writing the emails Google sends to guests
  and grantees: those land in mailboxes the agent cannot read.
- A history id never expires. Gmail answers a `startHistoryId` outside the range it keeps with a 404 and documents
  no fixed range.
- A part's bytes are served inline in `body.data`, never as an attachment id.
