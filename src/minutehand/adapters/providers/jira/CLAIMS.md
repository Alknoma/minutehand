# Jira Cloud: where the fake's behaviour comes from

Every behaviour of this fake is either Jira Cloud's documented behaviour, cited below, or refused by name. Each row
names its source and the test that holds the fake to it (tests are under `tests/providers/jira/`).

## Sources

- **The reference.** Atlassian's OpenAPI descriptions, fetched 2026-10-08:
  https://developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json (platform v3, version
  `1001.0.0-SNAPSHOT-075425bb…`) and https://developer.atlassian.com/cloud/jira/software/swagger.v3.json (Agile
  1.0). The subset for the resources this fake claims is committed as `tests/data/vendor_surface/jira.json`, with
  the paths under `/component` and `/version` (project components and versions) added from the platform description
  fetched 2026-10-09 (its `added` entry). Each operation's page is `https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-<group>/`; rows name
  the operation by its `operationId`.
- **JQL.** https://support.atlassian.com/jira-software-cloud/docs/jql-fields/,
  https://support.atlassian.com/jira-software-cloud/docs/jql-functions/,
  https://support.atlassian.com/jira-software-cloud/docs/jql-operators/.
- **OAuth.** https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/ and
  https://support.atlassian.com/user-management/docs/create-oauth-2-0-credential-for-service-accounts/.
- **Recorded answers of the real service**: `tests/providers/jira/data/observed/` (see its `README.md`), what a
  public Jira Cloud site and Atlassian's token endpoint and gateway answered on 2026-10-08 to requests made with no
  credentials; `test_jira_recorded.py` holds the fake to each, byte for byte after this world's identifiers.

### What "observed" meant before

The rows an earlier version of this file marked **observed** ("what callers of the real service reported") were
not observations of Jira Cloud. They were the claims of an older stand-in, a Flask emulator in another repository,
written by its author from Atlassian's reference and community posts; its own source marks the wording of these
refusals "WORDING UNVERIFIED against a real tenant", and no recorded traffic from a real site backs any of them. No
client library was involved. Each such row is now cited to the reference, or its behaviour is gone.

## Coverage

Of the 231 operations in the subset, 76 are served and 155 are refused by name: the app raises
`NotServed`, answered 501 in Jira's error body naming the method and the reference's path template
(`surface.UNSERVED`). A path outside the claimed resources (another API, `/rest/api/2`, `/dashboard`) is refused
the same way, naming the path; a path inside them that the reference does not name is Jira's 404. For each served
operation, every query parameter and body property the reference documents is either acted on or refused by name
in a 501 (`Served.refuses`, `Request.UNSERVED`); a property a closed schema (`additionalProperties: false`) has not
got is a 400. `test_jira_surface.py` holds all of this to the subset.

## Credentials and permissions

Authentication is out of scope (`docs/design.md`, "Authentication is out of scope"). Any credential, or none, is let in, and a
credential only says who calls: a seeded API token or access token is its account's, a Basic username that is an
account's email is that account's, anything else is the agent's (`test_jira_any_credential.py`). What a caller can
see is the world's data and stays: an issue or project in which the caller holds no role is a 404, as the
reference says of an issue "not found or the user does not have permission to view it".

Removed, each once a refusal:

- 401 for no `Authorization`, an unknown API token, an API token sent with another account's email, an OAuth access
  token at the site host, an expired access token, and a deactivated account's token;
- 401 from `accessible-resources` for an unknown token; at `auth.atlassian.com/oauth/token`, 403 `invalid_grant` for
  an unknown or spent refresh token and for every authorization code, and 401 for a client id or secret that did
  not match (any of these now gets a pair: a seeded grant's refresh token rotates it, any other pair acts as the
  agent; `client_credentials` is taken too);
- 403 for an account without an editing role creating, editing, deleting, assigning, transitioning, commenting on,
  linking or scheduling an issue;
- 403 for creating a project without site administration (the seed's `agent_site_admin` and the stored `siteAdmin`
  are gone), and for adding role members without administering the project;
- the roles' `edits` and `administers` flags: a role now only says who belongs to a project, and any active
  Atlassian account in a project is assignable in it.

`mypermissions` answers what the fake does: every global permission held, and a project permission held where
the caller sees the project.

## Creating a project (`POST /rest/api/3/project`, `createProject`, `CreateProjectDetails`)

| Claim | Source | Test |
|---|---|---|
| A body missing `key`, `name` or the lead is a 400 naming each, keyed `projectKey`, `projectName`, `leadAccountId` | `key` and `name` required; "Either `lead` or `leadAccountId` must be set"; `ErrorCollection`'s example keys `projectKey` | `test_a_project_create_missing_a_required_field_is_refused_naming_it` |
| An empty body names every missing field in one 400 | `ErrorCollection.errors`: "the list of errors by parameter" | `test_an_empty_project_create_names_every_missing_field_at_once` |
| With neither a type nor a template, the 400 names `projectTypeKey` | `projectTypeKey`: "If you don't specify the project template you have to specify the project type" | `test_a_project_create_with_neither_type_nor_template_is_refused_naming_the_type` |
| A template with no type is accepted, and the project takes the template's type | same | `test_a_project_create_with_a_template_and_no_type_takes_the_templates_type`, `test_a_service_desk_template_the_enum_lists_builds_a_service_desk_project` |
| A template of another type is a 400 on `projectTemplateKey` | `projectTemplateKey`: "must match with the type of the `projectTypeKey`" | `test_a_project_create_whose_template_belongs_to_another_type_is_refused` |
| Every template in `projectTemplateKey`'s enum is accepted; any other is a 400 | the enum (the fake used to lack two software and four service-desk templates) | `test_every_software_template_jira_lists_is_accepted`, `test_a_template_key_jira_does_not_have_is_refused` |
| A key that is lowercase, starts with a digit, holds `_` or `-`, is one letter, or is longer than ten is a 400 on `projectKey` | `key`: "start with an uppercase letter followed by one or more uppercase alphanumeric characters. The maximum length is 10" | `test_a_project_key_breaking_the_key_rule_is_refused`, `test_a_project_key_of_eleven_characters_is_refused`, `test_a_project_key_of_exactly_ten_characters_is_accepted` |
| A key already held is a 400 on `projectKey` | `key`: "Project keys must be unique"; the message is this fake's wording | `test_a_project_key_another_project_holds_is_refused_naming_that_project` |
| A name already held (any case) is a 400 on `projectName` | https://support.atlassian.com/jira-software-cloud/docs/create-a-business-project/ (space names are unique); `GET /projectvalidate/validProjectName` exists to find a name not in use | `test_a_project_name_another_project_holds_is_refused` |
| An email address as `leadAccountId`, or any id naming no account, is a 400 on that field; the caller's own account is a valid lead | `leadAccountId`: "The account ID of the project lead" | `test_an_email_address_as_the_lead_is_refused`, `test_the_callers_own_account_is_a_valid_lead` |
| `assigneeType` is kept as sent and read back; a value outside `PROJECT_LEAD`/`UNASSIGNED` is a 400 | the enum | `test_a_created_projects_default_assignee_is_kept_as_sent` |
| Any other documented property (`categoryId`, the schemes, `avatarId`, `url`, `lead`) is refused by name and creates nothing | not served | `test_a_project_property_the_fake_does_not_keep_is_refused_501_and_creates_nothing` |

## Asking what the caller may do (`GET /rest/api/3/mypermissions`, `getMyPermissions`)

| Claim | Source | Test |
|---|---|---|
| Exactly the keys asked for come back, each with `havePermission` | the operation | `test_mypermissions_answers_exactly_the_keys_asked_for` |
| A permission the caller lacks is answered 200 with `havePermission: false` (here only a project permission where it sees no project) | same | `test_mypermissions_reports_a_permission_the_caller_lacks_as_false` |
| No `permissions`, an unknown key, a lowercase or spaced key: a 400 for the whole call | 400: "`permissions` is empty, contains an invalid key" | `test_mypermissions_without_the_permissions_parameter_is_refused`, `test_mypermissions_with_one_unknown_key_refuses_the_whole_call_naming_it`, `test_mypermissions_with_a_malformed_permission_key_is_refused_400` |

## Everything else served

| Claim | Source | Test |
|---|---|---|
| A project read, its statuses, or `mypermissions?projectKey=` naming a malformed or unseen key is a 404 | `getProject` 404: "not found or the user does not have permission to view it" | `test_a_read_naming_a_malformed_project_key_is_refused_404` |
| An issue create naming a malformed project key is a 400 on `project` | `createIssue` 400: "contains invalid field values" | `test_an_issue_create_naming_a_malformed_project_key_is_refused_400_on_project` |
| An edit refused for one field is a 400 and writes none of its fields | `editIssue` 400 | `test_an_edit_refused_for_one_field_writes_none_of_them` |
| `GET`/`POST /rest/api/3/search` answer 410 pointing at `/search/jql` and write nothing | the reference: "Endpoint is currently being removed" (https://developer.atlassian.com/changelog/#CHANGE-2046); the 410 and its message are recorded from the real service by its users, e.g. https://gitlab.com/gitlab-org/gitlab/-/issues/569792 | `test_the_retired_search_is_refused_410_and_writes_nothing` |
| Comments come 100 a page by default; `orderBy` other than `created` is a 400; `-created` is newest first | `getComments` | `test_comments_come_100_a_page_unless_max_results_says_otherwise`, `test_comments_ordered_by_anything_but_created_are_refused_400` |
| `user/search` with both `query` and `accountId` is a 400 | `findUsers` 400 | `test_a_user_search_with_both_query_and_account_id_is_refused_400` |
| `user/assignable/search` with neither `query` nor `accountId`, or both, is a 400; `accountId` alone finds that account | `findAssignableUsers` 400 | `test_an_assignable_search_with_neither_query_nor_account_id_is_refused_400` |
| `users/search` pages 50 by default, at most 1000 | `getAllUsers` | `test_users_search_lists_everyone_with_their_account_type` |
| `project/search` orders by key by default, takes `orderBy=key` or `name` (`-` reverses), `keys`, `id` and `typeKey`, at most 100 a page, links `nextPage`, and lists description, lead, issue types and keys only when expanded; `GET /project/{key}` always includes the first three | `searchProjects`; `getProject` `expand`: "the project description, issue types, and project lead are included in all responses by default" | `test_project_search_lists_without_description_or_lead_until_expanded`, `test_project_search_orders_filters_and_links_the_next_page`, `test_project_search_pages_with_is_last` |
| A role read takes `excludeInactiveUsers`; a role's description is the world's (empty unless seeded), not a sentence the fake writes; a group added to a role is refused by name | `getProjectRole`, `addActorUsers` | `test_a_role_read_with_exclude_inactive_users_leaves_them_out`, `test_a_role_describes_itself_with_the_worlds_words_not_the_fakes`, `test_a_group_added_to_a_role_is_refused_501_naming_it` |
| `search/jql`: ids only by default; `-x` alone is the navigable fields less `x`; `fields` may repeat; 50 a page by default, at most 5000; `nextPageToken` on all but the last page; `names` beside the issues | `searchAndReconsileIssuesUsingJql`, `SearchAndReconcileResults` | `test_search_jql_with_no_fields_answers_ids_only_and_pages_by_token`, `test_search_fields_of_exclusions_only_start_from_the_navigable_fields`, `test_search_fields_given_more_than_once_are_all_answered`, `test_search_pages_past_100_issues_when_asked_for_more`, `test_search_names_are_the_results_not_each_issues` |
| A query with no restriction is a 400; `maxResults` outside 1 to 5000 is a 400 | `jql`: "this parameter requires a bounded query"; both sentences recorded (`jql_unbounded.http`, `search_max_results_zero.http`, `search_max_results_over.http`) | `test_a_query_that_cannot_be_read_is_refused_with_400`, `test_search_with_a_page_size_outside_1_to_5000_is_refused_400` |
| A query naming a field, value, function, issue or project the site has not got, an operator its field does not take, a date it cannot read, or `IS` with a value, matches no issue (200); an unknown `ORDER BY` field is passed over | recorded (`jql_field_unknown.http`, `jql_value_unknown.http`, `jql_function_unknown.http`, `jql_function_wrong_field.http`, `jql_operator_unsupported.http`, `jql_date_invalid.http`, `jql_key_unknown.http`, `jql_project_unknown.http`, `jql_is_not_empty_value.http`, `jql_text_no_word.http`, `jql_period_invalid.http`, `jql_order_field_unknown.http`), as an anonymous caller; the fake used to refuse each with a 400 in its own words | `test_a_query_naming_what_the_site_has_not_got_matches_nothing`, `test_an_order_by_field_the_site_has_not_got_is_passed_over` |
| `GET /issue`: every field by default; `-x` alone is every field less `x`; `expand=changelog` is most recent first (`GET /changelog` oldest first); an expansion not served is refused by name | `getIssue`, `getChangeLogs` | `test_an_issue_read_with_only_exclusions_answers_every_other_field`, `test_an_issues_changelog_expansion_is_most_recent_first`, `test_an_expansion_the_fake_does_not_serve_is_refused_501_naming_it` |
| `PUT /issue?returnIssue=true` answers 200 with the issue; an `update` operation other than `set` (and a label's `add`/`remove`) is refused by name | `editIssue` | `test_an_edit_asked_to_return_the_issue_answers_200_with_it`, `test_an_update_operation_the_fake_does_not_serve_is_refused_501_not_400` |
| `PUT /assignee`: `"-1"` gives the project's default assignee, `null` unassigns, no `accountId` is a 400 | `assignIssue` | `test_assigning_minus_one_gives_the_default_assignee_and_no_account_id_is_refused_400`, `test_assignee_endpoint_assigns_and_unassigns` |
| `GET /transitions?transitionId=` answers that transition alone | `getTransitions` | `test_transitions_read_for_one_transition_id_answer_that_one_alone` |
| A link whose comment is not an Atlassian document is a 400 and links nothing; an unknown link type is a 404 | `linkIssues` 400 ("the comment is not created… The issue link is also not created") and 404 ("the issue link type is not found") | `test_a_link_whose_comment_is_not_a_document_is_refused_400_and_links_nothing`, `test_a_link_of_a_type_the_site_has_not_got_is_refused_404` |
| `statuscategorychangedate` is when the status last changed category | Jira computes the field; the fake used to copy `updated` | `test_status_category_change_date_moves_only_when_the_category_does` |
| Boards filter by `name`, matching part of it | Agile `getAllBoards` | `test_boards_filter_by_name` |
| JQL: an increment without a unit is in the function's own period; `M` and `y` are calendar months and years; `endOfWeek()`, `endOfMonth()`, `startOfYear()`, `endOfYear()` and `futureSprints()` are served | JQL functions | `test_jql_month_increments_are_calendar_months_and_a_bare_increment_is_the_functions_own` |
| JQL: a documented field (`watcher`, `component`…), function (`membersOf()`…) or operator (`WAS`, `CHANGED`) not served is refused by name (the public site answers `WAS` with no issues for an anonymous caller, `jql_was.http`; the fake does not search history) | JQL fields, functions, operators | `test_jql_a_field_jira_documents_and_the_fake_does_not_serve_is_refused_501_naming_it`, `test_jql_a_function_jira_documents_and_the_fake_does_not_serve_is_refused_501_naming_it`, `test_jql_history_operators_are_refused_501_naming_them`, `test_a_query_that_cannot_be_read_is_refused_with_400` |

## Worklogs, attachments, comments, watchers, remote links, components, versions and webhooks

Rows here are all documented, each with the page it comes from. Page anchors are
https://developer.atlassian.com/cloud/jira/platform/rest/v3/ groups; the webhook page is
https://developer.atlassian.com/cloud/jira/platform/webhooks/.

### Worklogs (`addWorklog`, `getIssueWorklog`, `getWorklog`, `updateWorklog`, `deleteWorklog`, `bulkDeleteWorklogs`)

| Claim | Class | Source | Test |
|---|---|---|---|
| A worklog is kept as sent (its ADF `comment`, `started`) and read back by id and in the issue's list; `author` and `updateAuthor` are the caller, `created` and `updated` the run's clock | documented | `Worklog`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-post | `test_a_worklog_is_kept_as_sent_and_read_back_by_id_and_in_the_issues_list`, `test_a_worklog_logged_by_another_account_names_it_as_author` |
| `started` is required, and one, only one, of `timeSpent` and `timeSpentSeconds`; a missing or doubled one is a 400 (Jira's invalid-payload body, as recorded for a body of the wrong shape) | documented | `started`: "Required when creating a worklog"; `timeSpent`: "Cannot be provided if `timeSpentSecond` is provided": https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-post | `test_a_worklog_without_started_or_without_a_time_or_with_both_times_is_refused_400` |
| `timeSpent` is days (`#d`), hours (`#h`) or minutes (`#m` or `#`); weeks and any other form are refused by name (501) | documented | `timeSpent`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-post | `test_a_time_in_weeks_is_refused_501_because_the_reference_names_only_days_hours_and_minutes`, `test_time_spent_in_seconds_is_taken_as_it_is` |
| A day is eight hours and a week five days when `timeSpent` is read and written | observed-pending | Atlassian's time tracking pages (https://support.atlassian.com/jira-cloud-administration/docs/configure-time-tracking/ and the Server page) make hours per day and days per week site settings and state no default; 8 and 5 are what this fake has always assumed, recorded from no source | `test_a_worklog_adds_to_the_issues_time_spent_and_auto_reduces_the_remaining_estimate` |
| The issue's `timeSpentSeconds` is the sum of its worklogs (and the seeded time spent); a worklog adds to it, an update moves it by the difference, a delete takes it off | documented | `TimeTrackingDetails.timeSpent`: "Time worked on this issue": https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get | `test_a_worklog_adds_to_the_issues_time_spent_and_auto_reduces_the_remaining_estimate`, `test_a_worklog_update_changes_what_is_sent_and_auto_moves_the_estimate_by_the_difference` |
| `adjustEstimate` is `auto` unless given; on add, `auto` reduces the remaining estimate by the worklog's time, `leave` keeps it, `new` sets it to `newEstimate`, `manual` reduces it by `reduceBy`; it never goes below nothing | documented | `adjustEstimate` (default `auto`): https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-post | `test_a_worklog_adds_to_the_issues_time_spent_and_auto_reduces_the_remaining_estimate`, `test_adjust_estimate_leave_keeps_the_remaining_estimate`, `test_adjust_estimate_new_sets_the_remaining_estimate`, `test_adjust_estimate_manual_reduces_the_remaining_estimate_by_the_amount`, `test_the_remaining_estimate_never_goes_below_nothing` |
| `new` without `newEstimate` (or an unreadable one) and `manual` without `reduceBy` are 400s, in the reference's words | documented | 400: "`adjustEstimate` is set to `new` but `newEstimate` is not provided or is invalid": https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-post | `test_adjust_estimate_new_without_a_new_estimate_is_refused_400`, `test_adjust_estimate_manual_without_reduce_by_is_refused_400` |
| An update changes only what is sent; `auto` updates the estimate "by the difference between the original and updated value of `timeSpent`"; `leave` and `new` as on add; `manual` is not listed for an update and is refused by name | documented | `updateWorklog`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-id-put | `test_a_worklog_update_with_less_time_gives_estimate_back_under_auto`, `test_a_worklog_update_with_leave_or_new_sets_the_estimate_as_asked`, `test_a_worklog_update_with_manual_is_refused_501_because_its_reference_does_not_document_it` |
| A delete takes the worklog's time off the issue; `leave` keeps the estimate, `new` sets it, `manual` increases it by `increaseBy`; a worklog that is not the issue's is a 404 | documented | `deleteWorklog`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-id-delete | `test_deleting_a_worklog_takes_its_time_off_and_leave_keeps_the_estimate`, `test_deleting_with_new_and_manual_sets_or_raises_the_remaining_estimate`, `test_a_worklog_of_another_issue_or_none_is_404` |
| Bulk delete takes up to 5000 ids, all of them the issue's (else 404), with `adjustEstimate` `leave` or `auto` | documented | `bulkDeleteWorklogs`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-delete | `test_bulk_deleting_worklogs_removes_them_all_or_names_the_stranger_404`, `test_bulk_deleting_with_an_adjustment_the_reference_does_not_list_is_refused_501` |
| The list is oldest created first, `total` is the number of results on the page, and takes `startedAfter` (on or after) and `startedBefore`, in milliseconds | documented | `getIssueWorklog`: "The number of results on the page" (`PageOfWorklogs.total`) and "starting from the oldest worklog or from the worklog started on or after a date and time": https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-get | `test_worklogs_list_oldest_created_first_and_filter_on_when_they_started`, `test_started_after_includes_a_worklog_started_at_that_moment_and_started_before_leaves_it_out` |
| A worklog's `comment` must be an Atlassian document, else 400 in the words recorded for a comment | documented | `comment`: "in Atlassian Document Format": https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-post | `test_a_worklog_comment_that_is_not_a_document_is_refused_400` |
| Deleting an issue deletes its worklogs | documented | `deleteIssue` (the issue and what hangs from it): https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-delete | `test_deleting_an_issue_takes_its_worklogs_with_it` |

Refused by name because the reference does not say: `visibility`, `properties`, `expand` and `overrideEditableFlag`
(worklog properties and restrictions are not kept); listing without `maxResults` (its default is not given); a
delete with `adjustEstimate=auto` (the default) from an issue that has an estimate, because the reference says
`auto` "reduces" the estimate on a delete while its sibling `manual` "increases" it (an issue with no estimate has
none to move, so `auto` there is served); `manual` on an update; any `timeSpent` that is not days, hours and
minutes. The changelog entries (`timespent`, `timeestimate`, `WorklogId`) Jira may write are not documented and are
not written. `notifyUsers` changes nothing: no mail is sent. `GET /worklog/list`, `/worklog/updated`,
`/worklog/deleted` and `/worklog/move` stay refused.

### Attachments (`addAttachment`, `getAttachment`, `getAttachmentContent`, `removeAttachment`)

| Claim | Class | Source | Test |
|---|---|---|---|
| An upload is `multipart/form-data` whose parameter is `file`, with `X-Atlassian-Token: no-check`; the answer lists an attachment for each file, in the order sent, with `filename`, `size`, `mimeType`, `author`, `created`, `self` and `content` | documented | `addAttachment`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/#api-rest-api-3-issue-issueidorkey-attachments-post | `test_an_upload_answers_each_file_as_attachment_metadata`, `test_several_files_in_one_upload_are_each_an_attachment_in_the_order_sent` |
| More than 60 files in one upload is a 413 | documented | 413: "more than 60 files are requested to be uploaded": https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/#api-rest-api-3-issue-issueidorkey-attachments-post | `test_more_than_sixty_files_in_one_upload_are_refused_413` |
| The metadata reads back by id, the bytes exactly (`redirect=false`), and the issue lists its attachments in `fields.attachment` | documented | `getAttachment`, `getAttachmentContent`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/#api-rest-api-3-attachment-id-get ; the field: https://community.developer.atlassian.com/t/how-to-get-attachment-details-in-all-issues-by-rest-api/13334 | `test_the_metadata_reads_back_by_id_and_the_issue_lists_its_attachments`, `test_the_bytes_come_back_exactly_with_redirect_false` |
| `redirect` is true unless given and answers 303 with the download in `Location`; `Range` answers 206 with the part, 416 when it cannot be satisfied, 400 when malformed | documented | `getAttachmentContent`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/#api-rest-api-3-attachment-content-id-get | `test_the_default_is_a_redirect_to_the_download`, `test_a_range_header_answers_206_with_the_part_and_where_it_sits`, `test_an_unsatisfiable_range_is_416_and_a_malformed_one_is_400` |
| A delete removes the attachment, and an unknown one is a 404 | documented | `removeAttachment`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/#api-rest-api-3-attachment-id-delete | `test_deleting_an_attachment_removes_its_metadata_and_its_bytes`, `test_an_attachment_of_a_project_the_caller_holds_no_role_in_is_not_found_to_them` |

Refused by name because the reference does not say: an upload without `no-check` ("blocked", and not how), a part not
named `file` or with no file name, an upload that is not multipart, several ranges in one `Range`, the thumbnail
(`/attachment/thumbnail/{id}`, and `thumbnail` is left out of the metadata), and a size limit (a site setting; the
reference gives no number). A part with no `Content-Type` takes the type its file name suggests, else
`application/octet-stream`.

### Comments (`getComment`, `updateComment`, `deleteComment`), watchers, remote links, edit metadata

| Claim | Class | Source | Test |
|---|---|---|---|
| A comment update replaces the body (an ADF document) and sets `updated` and `updateAuthor`; `author` and `created` stay | documented | `updateComment`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/#api-rest-api-3-issue-issueidorkey-comment-id-put | `test_a_comment_update_replaces_the_body_and_stamps_who_and_when`, `test_a_comment_update_with_a_body_that_is_not_a_document_is_refused_400_and_changes_nothing` |
| A comment is read by id, deleted (204), and a comment that is not the issue's is a 404 in the reference's words | documented | `getComment`, `deleteComment`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/#api-rest-api-3-issue-issueidorkey-comment-id-get | `test_a_comment_is_read_by_id`, `test_deleting_a_comment_removes_it_and_a_second_delete_is_404`, `test_a_comment_of_another_issue_is_404_under_this_one` |
| Watchers: the body of an add is the account id as a JSON string, none adds the caller; a delete takes `accountId` (400 without it); the list has `watchCount`, `isWatching` and `watchers`, and the issue's `watches` field the first two | documented | `addWatcher`, `removeWatcher`, `getIssueWatchers`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-watchers/#api-rest-api-3-issue-issueidorkey-watchers-post | `test_adding_a_watcher_by_account_id_lists_them_and_counts_them`, `test_adding_a_watcher_with_no_body_adds_the_caller`, `test_removing_a_watcher_by_account_id`, `test_removing_a_watcher_without_an_account_id_is_refused_400`, `test_watching_as_an_account_that_is_not_there_is_404_and_a_body_that_is_not_a_string_is_400` |
| A remote link is created from `object` (`title` and `url` required) and read back as sent, by id, in the list, or by `globalId`; posting a `globalId` a link has replaces it (what is left out is null); a PUT replaces; a delete is by id or `globalId` | documented | `createOrUpdateRemoteIssueLink`, `getRemoteIssueLinks`, `updateRemoteIssueLink`, `deleteRemoteIssueLinkById`, `deleteRemoteIssueLinkByGlobalId`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/#api-rest-api-3-issue-issueidorkey-remotelink-post | `test_a_remote_link_is_created_and_read_back_exactly_as_sent`, `test_a_remote_link_is_found_by_its_global_id`, `test_posting_a_global_id_a_link_has_updates_it_and_nulls_what_is_left_out`, `test_a_remote_link_put_replaces_and_204`, `test_remote_links_are_deleted_by_id_and_by_global_id` |
| A link that does not belong to the issue, or whose id is invalid, is a 400; one that is not there is a 404; a global id over 255 characters or an object without `title` and `url` is a 400 | documented | 400 and 404 of `getRemoteIssueLinkById`, `RemoteIssueLinkRequest.globalId` ("The maximum length is 255 characters"), `RemoteObject.required`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/#api-rest-api-3-issue-issueidorkey-remotelink-linkid-get | `test_a_remote_link_of_another_issue_is_400_and_one_that_is_not_there_is_404`, `test_a_remote_link_without_an_object_title_or_url_is_refused_400`, `test_deleting_by_global_id_without_one_is_refused_400` |
| Edit metadata lists the fields of the issue's screen an edit can set (not the project or the issue type), in the shape `createmeta` gives them | documented | `getEditIssueMeta`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-editmeta-get | `test_edit_metadata_lists_the_screens_fields_an_edit_can_set` |

Refused by name because the reference does not say: a watcher added twice or removed who does not watch, a comment
update with no body, `visibility`, `expand` and `overrideEditableFlag`, both overrides of edit metadata, and
`issue/picker` (what its "History Search" holds and orders is not documented). Watchers are listed whole: the
permission to see them is not enforced.

### Project components and versions (`createComponent`, `getComponent`, `getProjectComponents(Paginated)`, `createVersion`, `getVersion`, `getProjectVersions(Paginated)`)

| Claim | Class | Source | Test |
|---|---|---|---|
| A component is created from `project` (the key) and `name` (255 characters at most), with `description`, `leadAccountId` and an `assigneeType` of `PROJECT_DEFAULT` (unless given), `COMPONENT_LEAD`, `PROJECT_LEAD` or `UNASSIGNED`; each failure is a 400 in the reference's words; a project not seen is a 404 | documented | `createComponent`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-components/#api-rest-api-3-component-post | `test_a_component_is_created_with_the_assignee_its_type_names`, `test_a_component_defaults_to_the_project_default_assignee`, `test_a_component_create_missing_what_is_required_or_wrong_is_refused_400`, `test_a_component_in_a_project_that_is_not_there_is_404_and_one_by_id_that_is_not_there_is_404` |
| `assignee` is the user `assigneeType` names, `realAssigneeType` falls back to `PROJECT_DEFAULT` when a `COMPONENT_LEAD` has no lead, and `isAssigneeTypeValid` is false then | documented | `ProjectComponent.assignee`, `realAssignee`, `realAssigneeType`, `isAssigneeTypeValid`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-components/#api-rest-api-3-component-id-get | `test_a_component_led_by_nobody_falls_back_and_says_its_type_is_not_valid`, `test_a_component_set_to_the_project_lead_is_assigned_to_them` |
| Project components list whole, or 50 a page ordered by `description`, `issueCount`, `lead` or `name` (`-` reverses) and filtered by `query` (name or description, any case); they appear on the project | documented | `getProjectComponents`, `getProjectComponentsPaginated`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-components/#api-rest-api-3-project-projectidorkey-component-get | `test_components_list_whole_and_paged_and_appear_on_the_project`, `test_component_order_by_other_than_the_four_and_compass_are_refused` |
| A version is created from `name` (255 characters at most) and `projectId`, with `description`, `archived`, `startDate` and `releaseDate` (`yyyy-mm-dd`) and read back as sent; `released` is "not applicable when creating" | documented | `createVersion`, `getVersion`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-versions/#api-rest-api-3-version-post | `test_a_version_is_created_and_read_back_as_sent`, `test_a_version_create_that_is_not_valid_or_names_no_project_is_refused`, `test_a_version_that_is_not_there_or_read_with_an_expansion_is_404_or_501` |
| Project versions list whole, or 50 a page ordered by `description`, `name`, `releaseDate` or `startDate` (those with no date last) or `sequence` (the order made), filtered by `query` and by `status` (`released`, `unreleased`, `archived`, by the flags the version holds) | documented | `getProjectVersions`, `getProjectVersionsPaginated`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-versions/#api-rest-api-3-project-projectidorkey-version-get | `test_versions_list_whole_and_paged_ordered_and_filtered`, `test_version_listing_refuses_an_unknown_status_and_order_and_an_expansion` |

Refused by name because the reference does not say: a name another component or version of the project holds
(the reference calls the name unique and gives no answer), `leadUserName`, `driver`, `moveUnfixedIssuesTo`, the
version `expand` and its deprecated `project`, a version created already released, and `componentSource=compass`.
`overdue`, `userStartDate` and `userReleaseDate` are left out of a version (the instance's date format is not
given). Components and versions are not yet a field an issue can hold: update and delete of either stay refused.

### Webhooks (`JiraSeed.webhooks`; `webhooks.py`)

| Claim | Class | Source | Test |
|---|---|---|---|
| A webhook set up on the site (as in Jira administration) is POSTed each event it names, as `jira:issue_created`, `jira:issue_updated` or `comment_created`: for the agent's own writes once the call is answered, for a person's transition when it is applied | documented | "Registration methods" (administration page), event names: https://developer.atlassian.com/cloud/jira/platform/webhooks/ | `test_an_issue_the_agent_creates_is_sent_as_issue_created_signed`, `test_a_comment_is_sent_as_comment_created_with_the_comment`, `test_a_persons_transition_is_sent_to_the_webhook_and_the_agent_hears_of_it` |
| The body holds `timestamp` (milliseconds), `webhookEvent`, `user` (without `emailAddress` and `locale`), `issue` (as the REST API answers it without `expand`), and for an update `changelog` (`id`, `items`) and `issue_event_type_name` `issue_generic`; for a comment event, `comment` | documented | "JSON payload": https://developer.atlassian.com/cloud/jira/platform/webhooks/ | `test_an_edit_is_sent_as_issue_updated_with_the_changelog_of_what_changed`, `test_a_transition_the_agent_makes_is_issue_updated_and_its_comment_is_comment_created` |
| `X-Atlassian-Webhook-Identifier` is unique to each delivery; `X-Hub-Signature` is `sha256=` and the HMAC-SHA256 of the body keyed with the webhook's secret, sent when it has one | documented | "HTTP headers" and "Secure admin webhooks": https://developer.atlassian.com/cloud/jira/platform/webhooks/ | `test_each_delivery_has_an_identifier_of_its_own`, `test_a_webhook_with_no_secret_is_not_signed` |
| A webhook's JQL filter takes `issueKey`, `project`, `issuetype`, `status`, `priority`, `assignee` and `reporter` with `=`, `!=`, `IN` and `NOT IN`, and is evaluated for the issue of each event | documented | "JQL filtering": https://developer.atlassian.com/cloud/jira/platform/webhooks/ | `test_a_webhook_with_a_jql_filter_is_sent_only_the_issues_it_matches`, `test_a_webhook_event_or_filter_the_reference_does_not_allow_is_refused_at_seeding` |

Refused by name (at seeding): every other event the reference lists (`jira:issue_deleted`, `comment_updated`, the
worklog, attachment, version, project and sprint events ...) and any clause, operator or `ORDER BY` the filter
section does not allow. Not done, because the reference gives no values for a first delivery or the fake sends once:
the retries (five, on 408, 409, 425, 429, 5xx, a refused connection or a timeout), and the headers
`X-Atlassian-Webhook-Retry` and `X-Atlassian-Webhook-Flow`; the registration by `POST /rest/api/3/webhook` and
`POST /rest/webhooks/1.0/webhook` (outside the claimed resources, 501). A comment sent as `comment_created` is not
also sent as `jira:issue_updated`: the reference does not say it is. Everything a person or the scenario does to an issue
goes through the people engine's `apply` (a `TicketHappening` among them), and each is sent as the agent's own call
would be.

## People's transitions (`docs/design-transitions.md`)

| Claim | Class | Source | Test |
|---|---|---|---|
| A person's move offers the transitions open from the issue's status and is taken through the same path as the agent's transition, its comment as `update.comment`; a transition not open from the status is refused | documented | `getTransitions`, `doTransition`: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-transitions-get | `test_an_offer_no_longer_legal_is_refused_as_jira_refuses_it`, `test_a_pinned_take_moves_the_issue_with_its_words_as_the_person` |
| A workflow begins with its initial transition, named Create, into its first status | documented | https://support.atlassian.com/jira-cloud-administration/docs/work-with-issue-workflows/ | `test_the_agents_own_create_and_transition_are_recorded_as_the_agents` |

## The words of a refusal

No refusal is in this fake's own words. Each is one of:

- **recorded**: Jira's sentence as `data/observed/` holds it: an unknown issue, project, link and link type; the
  JQL syntax errors (a field name or a value expected at the end, a value expected but an operator found, `AND`
  or `OR` expected, a `)` or a quote left open, with Jira's `(line 1, character N)`); an unbounded query; a page
  size outside 1 to 5000; a spent page token; the retired search; `mypermissions` with no keys, an unknown key
  (`errors: {KEY: "Unrecognized permission"}`) or an unknown project; comments ordered by anything but `created`;
  user search with neither or both of `query` and `accountId`; an assignable search naming no project or issue;
  `project/search` ordered by an unknown field; an issue create with no body, no project or no issue type; a body
  that is not JSON, not an object, of the wrong types or with a property a closed schema lacks (`errorMessages`
  alone, no `errors`); a field not on the screen; a comment body that is not a document; a transition not named;
  a query parameter that is not a number, a path no operation has and a method an operation has not got (Jira's
  `application/problem+json` body); the gateway's 404 for an unknown cloud id or path; the token endpoint's
  unknown grant and unreadable body; a host under atlassian.net holding no site (Jira's "Page unavailable" page,
  cut to its title and heading).
- **the reference's words**: where the reference documents the refusal and nothing records Jira's sentence, the
  message is the reference's own description of it, verbatim ("Returned if the request contains invalid field
  values.", "Returned if the request is missing required fields.", "Returned if the issue has subtasks and
  `deleteSubtasks` is not set to *true*.", "Returned if the user is not found.", "Returned if the request is not
  valid and the project could not be created." and the like); a malformed project key takes `ErrorCollection`'s
  example sentence; a declared rate limit takes the user-search reference's 429 sentence. Adding a role member who
  is no account is the reference's 404 ("Returned if the user or group is not found."), not the 400 the fake gave.
- **refused by name (501)**: where neither documents the answer: an empty body other than an issue create or
  edit, `GET /user` without `accountId`, an `update` operation that is not a list of single verbs, a negative
  `startAt` or `maxResults`.

## Not carried over

- A created project surviving a reload of the older stand-in's cache, its admin reset, and its one fixed principal
  holding every global permission: all three described the stand-in, not Jira.

## Values Jira assigns that this fake fills with its own

`serverInfo`'s `buildNumber` and `buildDate`, the permission ids and names `mypermissions` answers, avatar URLs and
a status's `iconUrl`. No test pins them.
