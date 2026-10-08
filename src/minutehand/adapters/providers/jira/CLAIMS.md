# Jira Cloud: where the fake's behaviour comes from

Every behaviour of this fake is either Jira Cloud's documented behaviour, cited below, or refused by name. Each row
names its source and the test that holds the fake to it (tests are under `tests/providers/jira/`).

## Sources

- **The reference.** Atlassian's OpenAPI descriptions, fetched 2026-10-08:
  https://developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json (platform v3, version
  `1001.0.0-SNAPSHOT-075425bb…`) and https://developer.atlassian.com/cloud/jira/software/swagger.v3.json (Agile
  1.0). The subset for the resources this fake claims is committed as `tests/data/vendor_surface/jira.json`. Each
  operation's page is `https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-<group>/`; rows name
  the operation by its `operationId`.
- **JQL.** https://support.atlassian.com/jira-software-cloud/docs/jql-fields/,
  https://support.atlassian.com/jira-software-cloud/docs/jql-functions/,
  https://support.atlassian.com/jira-software-cloud/docs/jql-operators/.
- **OAuth.** https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/ and
  https://support.atlassian.com/user-management/docs/create-oauth-2-0-credential-for-service-accounts/.
- **Recorded observations of the real service** are cited where used; there is one (the retired search).

### What "observed" meant before

The rows an earlier version of this file marked **observed** ("what callers of the real service reported") were
not observations of Jira Cloud. They were the claims of an older stand-in, a Flask emulator in another repository,
written by its author from Atlassian's reference and community posts; its own source marks the wording of these
refusals "WORDING UNVERIFIED against a real tenant", and no recorded traffic from a real site backs any of them. No
client library was involved. Each such row is now cited to the reference, or its behaviour is gone.

## Coverage

Of the 212 operations in the subset, 45 are served and 167 are refused by name: the app raises
`NotImplementedError`, answered 501 in Jira's error body naming the method and the reference's path template
(`surface.UNSERVED`). A path outside the claimed resources (another API, `/rest/api/2`, `/dashboard`) is refused
the same way, naming the path; a path inside them that the reference does not name is Jira's 404. For each served
operation, every query parameter and body property the reference documents is either acted on or refused by name
in a 501 (`Served.refuses`, `Request.UNSERVED`); a property a closed schema (`additionalProperties: false`) has not
got is a 400. `test_jira_surface.py` holds all of this to the subset.

## Credentials and permissions are not enforced

Minutehand deliberately does not enforce credentials or permissions. Any credential, or none, is let in, and a
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
| A query with no restriction is a 400; `maxResults` below 1 is refused by name, since the reference does not say what it answers | `jql`: "this parameter requires a bounded query" | `test_a_query_that_cannot_be_read_is_refused_with_400`, `test_search_with_no_room_on_the_page_is_refused_501` |
| `GET /issue`: every field by default; `-x` alone is every field less `x`; `expand=changelog` is most recent first (`GET /changelog` oldest first); an expansion not served is refused by name | `getIssue`, `getChangeLogs` | `test_an_issue_read_with_only_exclusions_answers_every_other_field`, `test_an_issues_changelog_expansion_is_most_recent_first`, `test_an_expansion_the_fake_does_not_serve_is_refused_501_naming_it` |
| `PUT /issue?returnIssue=true` answers 200 with the issue; an `update` operation other than `set` (and a label's `add`/`remove`) is refused by name | `editIssue` | `test_an_edit_asked_to_return_the_issue_answers_200_with_it`, `test_an_update_operation_the_fake_does_not_serve_is_refused_501_not_400` |
| `PUT /assignee`: `"-1"` gives the project's default assignee, `null` unassigns, no `accountId` is a 400 | `assignIssue` | `test_assigning_minus_one_gives_the_default_assignee_and_no_account_id_is_refused_400`, `test_assignee_endpoint_assigns_and_unassigns` |
| `GET /transitions?transitionId=` answers that transition alone | `getTransitions` | `test_transitions_read_for_one_transition_id_answer_that_one_alone` |
| A link whose comment is not an Atlassian document is a 400 and links nothing; an unknown link type is a 404 | `linkIssues` 400 ("the comment is not created… The issue link is also not created") and 404 ("the issue link type is not found") | `test_a_link_whose_comment_is_not_a_document_is_refused_400_and_links_nothing`, `test_a_link_of_a_type_the_site_has_not_got_is_refused_404` |
| `statuscategorychangedate` is when the status last changed category | Jira computes the field; the fake used to copy `updated` | `test_status_category_change_date_moves_only_when_the_category_does` |
| Boards filter by `name`, matching part of it | Agile `getAllBoards` | `test_boards_filter_by_name` |
| JQL: an increment without a unit is in the function's own period; `M` and `y` are calendar months and years; `endOfWeek()`, `endOfMonth()`, `startOfYear()`, `endOfYear()` and `futureSprints()` are served | JQL functions | `test_jql_month_increments_are_calendar_months_and_a_bare_increment_is_the_functions_own` |
| JQL: a documented field (`watcher`, `component`…), function (`membersOf()`…) or operator (`WAS`, `CHANGED`) not served is refused by name; an unknown function is a 400 | JQL fields, functions, operators | `test_jql_a_field_jira_documents_and_the_fake_does_not_serve_is_refused_501_naming_it`, `test_jql_a_function_jira_documents_and_the_fake_does_not_serve_is_refused_501_naming_it`, `test_jql_history_operators_are_refused_501_naming_them`, `test_a_query_that_cannot_be_read_is_refused_with_400` |

## Not carried over

- Refusal sentences word for word: this fake answers in its own words, in Jira's `errorMessages`/`errors` shape,
  keyed by the fields the reference names.
- A created project surviving a reload of the older stand-in's cache, its admin reset, and its one fixed principal
  holding every global permission: all three described the stand-in, not Jira.

## Values Jira assigns that this fake fills with its own

`serverInfo`'s `buildNumber` and `buildDate`, the permission ids and names `mypermissions` answers, avatar URLs and
a status's `iconUrl`. No test pins them.
