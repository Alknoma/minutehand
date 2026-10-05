# Google Drive, Docs and Slides: where each behaviour comes from

Facts about the real services that an older, separately written stand-in for Drive was tested against, carried
here so the fake keeps them. **Documented** means Google's public reference says so (the page is linked; read it
there). **Observed** means no page says so and the fact rests on someone having seen the real service do it.

Tests named `vendor_claims` live in `tests/providers/google_drive/test_google_drive_vendor_claims.py` (Drive) and
`test_google_drive_vendor_claims_docs_and_slides.py` (Docs, Slides). A claim an earlier test already pinned names
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
| `files/root` answers My Drive's own id, never the alias `root` | observed | `test_drive_api.py::test_root_answers_with_my_drives_own_id` | |
| A listing answers one page and a `nextPageToken` for the rest | documented | `test_drive_api.py::test_a_listing_takes_pages_until_the_token_runs_out` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list |
| `pageSize` reaches 1000, and a larger folder still hands back a token | documented | `test_a_page_of_a_thousand_is_answered_whole_with_a_token_for_the_rest` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list |
| A field mask on a listing narrows each file to the fields named | documented | `test_a_listing_answers_only_the_file_fields_asked_for` | https://developers.google.com/workspace/drive/api/guides/fields-parameter |
| `mimeType = '...'` leaves folders out | documented | `test_a_mime_type_filter_leaves_folders_out` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| `trashed = true` lists only the trash, and nothing when nothing is trashed | documented | `test_trashed_true_lists_only_what_is_in_the_trash` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| An unescaped apostrophe inside a quoted value is a 400 `invalid` | documented | `test_drive_refusals.py::test_a_malformed_q_is_refused_400_invalid` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| An apostrophe escaped as `\'` finds the name that holds it | documented | `test_an_escaped_apostrophe_finds_the_name_that_holds_one` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| An unknown query term is a 400 `invalid` whose message names the term | observed | `test_an_unknown_query_term_is_refused_400_naming_the_term` | |
| `name contains` is prefix matching (documented); the start of a later word counts as a prefix (observed) | documented | `test_name_contains_matches_a_prefix_of_a_word_never_a_fragment_inside_one` | https://developers.google.com/workspace/drive/api/guides/ref-search-terms |
| Exporting a file that is not a Workspace document is a 403 `fileNotExportable` | documented | `test_drive_refusals.py::test_exporting_a_file_that_is_not_a_docs_file_is_refused_403_file_not_exportable` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export |
| An export over 10 MB is refused (documented) as a 403 `exportSizeLimitExceeded` (observed) | documented | `test_exporting_a_doc_over_ten_megabytes_is_refused_403_export_size_limit_exceeded` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export |
| `alt=media` on a Google Doc is a 403 `fileNotDownloadable`; a Doc is read by export | documented | `test_drive_refusals.py::test_downloading_a_google_doc_with_alt_media_is_refused_403_file_not_downloadable` | https://developers.google.com/workspace/drive/api/guides/manage-downloads |
| A permission role outside owner/organizer/fileOrganizer/writer/commenter/reader (`editor`) is a 400 | documented | `test_drive_refusals.py::test_a_permission_drive_would_not_grant_is_refused_400` | https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions |
| A `user` grant without `emailAddress` is a 400 `invalidSharingRequest` | documented | `test_drive_refusals.py::test_a_permission_drive_would_not_grant_is_refused_400` | https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions |
| An `anyone`/`reader` link share needs no address and is granted | documented | `test_a_link_share_to_anyone_as_reader_is_granted` | https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions |
| A deleted file leaves every listing, trash included, and is then a 404 | documented | `test_a_deleted_file_leaves_every_listing` | https://developers.google.com/workspace/drive/api/reference/rest/v3/files/delete |
| Text uploaded with the Docs type becomes a Doc that `documents.get` reads as paragraphs | documented | `test_drive_api.py::test_a_created_doc_reads_through_the_docs_api_as_paragraphs_of_text_runs` | https://developers.google.com/workspace/drive/api/guides/manage-uploads |
| A Doc's plain-text export and its Docs body hold the same characters | observed | `test_an_uploaded_doc_reads_the_same_through_export_and_the_docs_api` | |
| A plain-text export opens with a byte-order mark and ends lines CRLF | observed | `test_drive_api.py::test_a_created_doc_exports_as_text_with_a_bom_and_crlf` | |
| A Doc created with no text is a section break and one newline ending at 2, without its name in it | observed | `test_a_doc_created_with_no_text_is_its_final_newline_and_nothing_else` | |
| A CSV uploaded with the Sheets type exports its rows as text/csv | documented | `test_a_csv_uploaded_as_a_sheet_exports_its_rows_as_csv` | https://developers.google.com/workspace/drive/api/guides/manage-uploads |

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
| A new presentation holds one slide and its title | observed | `test_drive_through_proxy.py::test_a_deck_made_through_drive_is_built_by_slides_batch_update` | |
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
- **`fieldNotWritable` as a 400.** The older stand-in refused `parents` in an update body with a 400. This fake
  answers a 403 with the same reason, as an earlier test here already held; no public page states the status, so
  the 403 stands and the 400 is not carried.
- **Placeholder ids derived from the slide's id** (`<slide>_title`, `<slide>_body`, `<slide>_notes_body`). Slides
  mints placeholder ids unless the caller maps them; a client reads them from `presentations.get`.
- **File text in the JSON body of `files.create`** (`content`, `_media_content`). Drive takes a file's bytes through
  the upload endpoint only.
- **A health route, and admin routes that seed files or reset the store.** Drive has neither; a scenario seeds this
  fake.
