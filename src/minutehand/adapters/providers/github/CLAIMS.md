# GitHub provider: vendor claims

The behaviours below were each asserted by a test of an older, home-grown GitHub emulator. Each is a fact somebody
learned about api.github.com; this provider keeps it, and the named test fails if it stops. **Documented** means
GitHub's public reference says so (the page is given); **recorded** means the reference does not settle it and the
claim rests on an exchange with the real service kept in `tests/providers/github/observed/api.github.com.2026-10-08.json`
(no credential, the public `octocat/Hello-World`, 2026-10-08); **observed, not recorded** means the old emulator's
authors saw it and no recording is held: each of those needs a credential to record (code search and GraphQL refuse
a call without one) and is unproven. Tests are in `tests/providers/github/`, files
`test_github_vendor_claims_rest.py` (R), `test_github_vendor_claims_search.py` (S),
`test_github_vendor_claims_graphql.py` (G), `test_github_vendor_claims_budget.py` (B), `test_github_refusals.py`
(F), `test_github_coverage.py` (C), `test_github_world_without_users.py` (W), and, for what the agent writes,
`test_github_issues.py` (I), `test_github_comments_and_labels.py` (K), `test_github_tracker_seed.py` (E) and
`tests/transitions/test_github_people.py` (T). **Observed** means the same of a capture committed under `tests/data/`:
`tests/data/github_rest/observed-2026-10-09.json` holds read-only exchanges with public repositories, trimmed to what
a claim rests on and holding no login or free text of anyone.

Authentication is out of scope (`docs/design.md`, "Authentication is out of scope"). Every `Authorization`, or none, is accepted, and nothing refuses a call
for what a token was or was not issued for. The claims GitHub documents about refusing credentials are listed
under "What GitHub would refuse" below, with the tests that hold the provider to accepting them.

## Ported

| Claim | Class | Test | Source |
|---|---|---|---|
| In a world with no user to act as: public data is served, `/user` is 401 "Requires authentication" | documented | W `test_github_world_without_users.py::test_with_nobody_to_act_as_public_data_reads_and_what_needs_a_user_is_refused_401` | https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api |
| …its `documentation_url` is `https://docs.github.com/rest` | recorded | (same test) | `observed/api.github.com.2026-10-08.json` |
| `Authorization: Bearer <token>` acts as the token's user | documented | R `test_a_bearer_token_is_served_as_its_user` | https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api |
| An error body is `message`, `documentation_url` and `status` (a string), with `errors` when there are some | recorded | F, R (every `refusal`) | `observed/api.github.com.2026-10-08.json` |
| An unknown `X-GitHub-Api-Version` is 400 "Bad Request", the reason a sentence in `errors` | recorded | F `test_an_api_version_github_does_not_serve_is_refused` | `observed/api.github.com.2026-10-08.json` |
| A GET answers an `ETag`; sent back in `If-None-Match` it is a 304 with no body | documented | R `test_a_get_carries_an_etag_and_sent_back_in_if_none_match_is_a_304_that_spends_nothing` | https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate |
| …a 304 to an authorized call spends nothing; without a credential it spends | documented | R (same test), W `test_github_world_without_users.py::test_with_nobody_to_act_as_a_304_still_spends_the_addresss_budget` | (same page) |
| …the `ETag` is weak, `W/"` and 64 hex digits | recorded | R (first test) | `observed/api.github.com.2026-10-08.json` |
| The installation-token exchange is 201 with `token` and `expires_at`, `permissions` as asked | documented | R `test_an_installation_token_is_issued_for_any_app_jwt_and_works_as_any_credential` | https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app |
| …the token starts `ghs_` and expires an hour after it is made | documented | (same test) | https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app, https://github.blog/2021-04-05-behind-githubs-new-authentication-token-formats/ |
| Every answer of a served operation holds every field the description requires of it | documented | C `test_a_served_operation_answers_every_field_the_description_requires` | https://github.com/github/rest-api-description |
| A repository's URL templates, `git_url`, `ssh_url`, `clone_url`, `svn_url`, `mirror_url: null` | recorded | C (same test) | `observed/api.github.com.2026-10-08.json` |
| A new repository's features: `has_issues`, `has_projects`, `has_wiki` on, `has_discussions` off | documented | C (same test) | https://docs.github.com/en/rest/repos/repos#create-a-repository-for-the-authenticated-user |
| `has_pages` false, `has_downloads` true | documented (`has_downloads` was observed false on an old repository) | C (same test) | (same page) |
| A commit's committer defaults to its author, date included ("By default, `committer` will use the information set in `author`."); a seed may declare another committer and when it committed | documented | R `test_a_commit_s_committer_is_the_one_the_seed_declares_else_its_author` | https://docs.github.com/en/rest/git/commits#create-a-commit |
| A commit's author and committer names are the accounts' declared names (their own or their person's); an account with none refuses the seed, naming the commit. Emails are the declared ones, else GitHub's no-reply address | documented (no-reply) | R `test_a_commit_by_an_account_with_no_name_is_refused_naming_the_commit` | https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address |
| A repository listing's `Link` points at `/repositories/{id}/…`, which answers as `/repos/{owner}/{repo}/…` | recorded | R `test_a_link_header_points_under_repositories_by_id_and_that_address_answers` | `observed/api.github.com.2026-10-08.json` |
| No file outside a commit: a seed repository with files and no commit is refused, naming it, and so is a file no commit's `paths` names, in a seed or in a fragment added to an open world | documented (git's object model) | `test_github_world.py` `test_a_repository_with_files_and_no_commit_is_rejected_naming_it`, `test_github_further_seed.py` `test_a_fragment_adding_a_file_to_a_held_repository_without_a_commit_is_refused_naming_it` | https://git-scm.com/book/en/v2/Git-Internals-Git-Objects |
| A node id names one object: two licenses have two | documented | R `test_two_licenses_have_two_node_ids` | https://docs.github.com/en/graphql/guides/using-global-node-ids |
| Every answer carries `X-RateLimit-Limit/Remaining/Used/Reset/Resource`; core is 5,000 | documented | R `test_every_answer_carries_the_rate_limit_headers_for_its_budget` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| With no user to act as, the budget is 60 an hour | documented | W `test_github_world_without_users.py::test_with_nobody_to_act_as_public_data_reads_and_what_needs_a_user_is_refused_401` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| Code search spends its own budget, `code_search`, 10 a minute | documented | R `test_code_search_reports_its_own_ten_a_minute_budget` | https://docs.github.com/en/rest/search/search#search-code |
| Each call spends one from its budget; the reset moment stays put through the window; a 404 spends too | documented | B `test_each_call_spends_one_from_the_users_budget_and_the_reset_stays_put` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| The budget is the user's, shared by all their tokens, not the token's | documented | B `test_two_tokens_of_one_user_spend_one_budget_and_another_user_has_their_own` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| Core, code search and GraphQL are separate budgets | documented | B `test_each_resource_is_its_own_budget` | https://docs.github.com/en/rest/rate-limit/rate-limit |
| The call after the last is 403 "API rate limit exceeded", `Remaining: 0`, until `X-RateLimit-Reset`; the budget is whole after it | documented | B `test_the_call_after_the_last_is_refused_403_until_the_reset_on_the_runs_clock` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| With no user to act as, the address's budget is spent and refused the same way | documented | W `test_github_world_without_users.py::test_with_nobody_to_act_as_a_spent_address_budget_is_refused_403` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| A spent GraphQL budget is a 200 whose errors say RATE_LIMITED | documented | B `test_a_spent_graphql_budget_answers_200_with_rate_limited_errors` | https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api |
| `GET /rate_limit` reports every budget, `rate` as core, and does not count | documented | B `test_rate_limit_reports_every_budget_and_spends_none` | https://docs.github.com/en/rest/rate-limit/rate-limit |
| A secondary limit is 403 or 429 with `Retry-After`, then calls go through | documented | G `test_a_secondary_limit_is_a_403_or_429_with_retry_after_and_then_the_call_goes_through` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| An unknown or unreachable repository is 404 on every repository route | documented | R `test_an_unknown_repository_is_not_found_on_every_route` | https://docs.github.com/en/rest/repos/repos#get-a-repository |
| `/user/repos` lists only what the token reaches, with `permissions` | documented | R `test_user_repos_lists_only_what_the_token_can_see_with_its_permissions` | https://docs.github.com/en/rest/repos/repos#list-repositories-for-the-authenticated-user |
| `/languages` is bytes of code per language | documented | R `test_languages_counts_bytes_of_code_leaving_out_prose_and_vendored_files` | https://docs.github.com/en/rest/repos/repos#list-repository-languages |
| `/languages` leaves out prose and vendored paths | documented (linguist, GitHub's own) | (same test) | https://github.com/github-linguist/linguist/blob/main/docs/how-linguist-works.md |
| `/branches` lists name, commit, `protected` | documented | R `test_branches_list_each_branch_with_its_commit` | https://docs.github.com/en/rest/branches/branches#list-branches |
| Contents at a ref naming no commit is 404 "No commit found for the ref …", pointing at `https://docs.github.com/v3/repos/contents/` | recorded | R `test_contents_at_a_ref_that_names_no_commit_is_refused_404` | `observed/api.github.com.2026-10-08.json` |
| Contents `ref` takes a branch or a commit sha | documented | R `test_contents_takes_a_branch_or_a_commit_sha_as_its_ref` | https://docs.github.com/en/rest/repos/contents#get-repository-content |
| A file over 1 MiB answers `content: ""`, `encoding: none` | documented | R `test_a_file_over_a_mebibyte_answers_no_content_and_encoding_none` | https://docs.github.com/en/rest/repos/contents#get-repository-content |
| The blob endpoint carries those bytes, same size | documented | R `test_the_blob_endpoint_carries_the_bytes_the_contents_endpoint_left_out` | https://docs.github.com/en/rest/git/blobs#get-a-blob |
| A directory listing stops at 1,000 entries and says nothing | documented | R `test_a_directory_listing_stops_at_a_thousand_entries_and_says_nothing` | https://docs.github.com/en/rest/repos/contents#get-repository-content |
| Commits from a sha naming nothing are 404 "Not Found" (corrected: the old emulator said "No commit found for SHA: …") | recorded | R `test_commits_from_a_sha_that_names_no_commit_are_refused_404` | `observed/api.github.com.2026-10-08.json` |
| Commits filter by `path` | documented | R `test_commits_filter_by_path` | https://docs.github.com/en/rest/commits/commits#list-commits |
| Commits honour `per_page` | documented | R `test_commits_honour_per_page` | https://docs.github.com/en/rest/commits/commits#list-commits |
| A listed commit has author email and `html_url`, and no `files` | recorded | R `test_a_listed_commit_carries_its_author_and_link_and_no_file_list` | `observed/api.github.com.2026-10-08.json` |
| A blob sha that is not 40 hex digits is 422 "The sha parameter must be exactly 40 characters and contain only [0-9a-f]." with no `errors` (corrected: the old emulator said "Validation Failed") | recorded | F `test_a_blob_sha_that_is_not_a_sha_is_refused_and_an_unknown_one_not_found` | `observed/api.github.com.2026-10-08.json` |
| A tree without `recursive` is one level | documented | R `test_a_tree_without_recursive_is_one_level_deep` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| A recursive tree holds every object | documented | R `test_a_recursive_tree_holds_every_object` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| A tree at a ref naming nothing is 404 | documented | R `test_a_tree_at_a_ref_that_names_nothing_is_not_found` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| A tree past its limit is cut and `truncated: true` | documented | R `test_a_tree_past_its_entry_limit_is_answered_partial_and_marked_truncated` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| Search matches whole tokens, never a substring | observed, not recorded | S `test_a_substring_of_a_token_does_not_match` | |
| Every bare term must match | observed, not recorded | S `test_every_term_of_the_query_has_to_match` | |
| `repo:` scopes the search | documented | S `test_a_repo_qualifier_scopes_the_search_and_one_the_token_cannot_see_is_refused_422` | https://docs.github.com/en/search-github/searching-on-github/searching-code |
| An unreachable `repo:` is 422 "cannot be searched" | documented | (same test) | https://docs.github.com/en/rest/search/search#access-errors-or-missing-search-results |
| `language:` filters by language | documented | S `test_a_language_qualifier_keeps_only_files_in_that_language` | https://docs.github.com/en/search-github/searching-on-github/searching-code |
| Qualifiers with no term are 422 `invalid` | documented | S `test_a_query_of_qualifiers_alone_is_refused_422_invalid` | https://docs.github.com/en/rest/search/search#search-code |
| An empty `q` is 422 `missing` | observed, not recorded | S `test_an_empty_query_is_refused_422_missing` | |
| Results page by `per_page`/`page`, same `total_count` on each page | documented | S `test_results_are_paged_and_every_page_reports_the_same_total` | https://docs.github.com/en/rest/search/search#search-code |
| `text_matches` only under the text-match media type | documented | S `test_text_matches_come_only_when_the_text_match_media_type_is_asked_for` | https://docs.github.com/en/rest/search/search#text-match-metadata |
| Files of 384 KB or more are not searchable | documented | S `test_a_file_past_the_index_ceiling_is_not_searchable_though_it_reads_inline` | https://docs.github.com/en/rest/search/search#search-code |
| GraphQL `variables` name the repository | documented | G `test_variables_name_the_repository_a_query_reads` | https://docs.github.com/en/graphql/guides/forming-calls-with-graphql |
| An unresolvable repository is `null` with a NOT_FOUND error | observed, not recorded | G `test_a_repository_that_does_not_resolve_is_null_with_a_not_found_error` | |
| A binary blob's `text` is null | documented | G `test_a_binary_blob_has_null_text` | https://docs.github.com/en/graphql/reference/git#blob |
| `object(expression:)` naming no path is null | documented | G `test_an_expression_naming_no_path_is_null` | https://docs.github.com/en/graphql/reference/objects#repository |
| `viewer` is the token's user | documented | G `test_the_viewer_is_the_token_s_user` | https://docs.github.com/en/graphql/guides/forming-calls-with-graphql |
| A body with no string `query` is 400 | documented | G `test_a_body_without_a_query_is_refused_400` | https://docs.github.com/en/graphql/guides/forming-calls-with-graphql |
| …and the message says a query attribute must be specified | observed, not recorded | (same test) | |

## Issues, comments and labels

What an agent writes is kept and answered as sent (titles, bodies, label names, comments); what GitHub assigns is
made. Rows are written from the reference's pages and the committed description
(`tests/providers/github/openapi/api.github.com.subset.json`); a detail the reference leaves out is refused by name
(below) or taken from the capture above and said so.

| Claim | Class | Test | Source |
|---|---|---|---|
| A repository's issues list only the open ones by default, newest created first; a pull request is an issue and is marked by `pull_request` | documented | I `test_issues_list_the_open_ones_newest_first_and_mark_the_pull_request` | https://docs.github.com/en/rest/issues/issues#list-repository-issues |
| `state` is open, closed or all; `labels` (comma separated, all must be on the issue), `assignee` (a login, `none`, `*`) and `creator` filter | documented | I `test_issues_filter_by_state`, I `test_issues_filter_by_labels_assignee_and_creator` | https://docs.github.com/en/rest/issues/issues#list-repository-issues |
| `sort` is created, updated or comments and `direction` asc or desc (desc by default) | documented | I `test_issues_sort_by_created_updated_or_comments_in_either_direction` | https://docs.github.com/en/rest/issues/issues#list-repository-issues |
| `milestone` and `type` take `none` for issues with none and `*` for issues with one; no issue here has either | documented | I `test_the_milestone_and_type_filters_follow_from_a_world_with_neither` | https://docs.github.com/en/rest/issues/issues#list-repository-issues |
| `since` keeps issues updated at or after it (inclusive) | observed | I `test_issues_since_keeps_those_updated_at_or_after_it` | `tests/data/github_rest/observed-2026-10-09.json` |
| A comment on an issue moves its `updated_at` to the comment's moment | observed | K `test_commenting_moves_the_issue_update_time` | `tests/data/github_rest/observed-2026-10-09.json` |
| A pull request on the "Issues" routes has `html_url` under `/pull/` and `pull_request` with `url`, `html_url`, `diff_url`, `patch_url`, `merged_at` | observed | I `test_issues_list_the_open_ones_newest_first_and_mark_the_pull_request`, K `test_a_pull_requests_conversation_is_commented_on_through_the_issue_routes` | `tests/data/github_rest/observed-2026-10-09.json` |
| An issue holds `state_reason`, `closed_by`, `locked`, `active_lock_reason`, `assignees`, `milestone: null` and the `*_url` fields; the provider answers them from the world | observed | I `test_an_issue_reads_with_the_urls_and_counts_github_assigns` | `tests/data/github_rest/observed-2026-10-09.json` |
| An issue and a comment are 404 when they or their repository are not there or not visible | documented | I `test_an_issue_that_is_not_there_and_a_repository_the_caller_cannot_see_are_both_404` | https://docs.github.com/en/rest/issues/issues#get-an-issue |
| Creating an issue answers 201 with the issue; title and body come back as sent, `title` taking a string or an integer | documented | I `test_an_issue_the_agent_opens_is_kept_and_returned_as_sent`, I `test_a_whole_number_title_is_kept_as_its_text` | https://docs.github.com/en/rest/issues/issues#create-an-issue |
| "Only users with push access can set labels [and assignees] for new issues. [They are] silently dropped otherwise." | documented | I `test_labels_and_assignees_are_dropped_silently_without_push_access` | https://docs.github.com/en/rest/issues/issues#create-an-issue |
| `labels` takes names or objects with a `name`; `assignee` is accepted beside `assignees` | documented | I `test_labels_may_be_objects_with_a_name_and_an_assignee_may_stand_for_assignees` | https://docs.github.com/en/rest/issues/issues#create-an-issue |
| Omitting a required parameter, or giving one the wrong type, is 422 "Invalid request" | documented | I `test_a_request_without_a_title_or_with_the_wrong_type_is_422_invalid_request` | https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api#invalid-request |
| A body that is not JSON is 400 "Problems parsing JSON", and one that is not an object 400 "Body should be a JSON object" | documented | I `test_a_body_that_is_not_json_or_not_an_object_is_400` | https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api#problems-parsing-json |
| Updating an issue takes `state` (open, closed); closing sets `closed_at` and `closed_by`, reopening clears them; `state_reason` is "Ignored unless `state` is changed" | documented | I `test_an_issue_closed_and_reopened_records_who_when_and_why`, I `test_a_state_reason_is_ignored_unless_the_state_changes` | https://docs.github.com/en/rest/issues/issues#update-an-issue |
| Updating replaces `labels` and `assignees` with what is sent and `[]` clears them; without push access those changes are silently dropped | documented | I `test_labels_and_assignees_replace_the_set_and_an_empty_array_clears_it`, I `test_label_and_assignee_changes_without_push_access_are_dropped_silently` | https://docs.github.com/en/rest/issues/issues#update-an-issue |
| "Issue owners and users with push access or Triage role can edit an issue"; the reference documents 403 for the refusal and names it "Forbidden" (it gives no message, so the answer's message is that name) | documented | I `test_only_the_author_or_a_triager_may_edit_an_issue` | https://docs.github.com/en/rest/issues/issues#update-an-issue |
| Locking takes push access, answers 204, and takes a `lock_reason` of off-topic, too heated, resolved or spam; unlocking answers 204 | documented | I `test_a_conversation_is_locked_with_a_reason_and_unlocked`, I `test_locking_takes_push_access_and_a_reason_from_the_reference` | https://docs.github.com/en/rest/issues/issues#lock-an-issue |
| Only collaborators can comment on a locked conversation | documented | I `test_commenting_on_a_locked_conversation_without_push_access_is_refused_by_name` | https://docs.github.com/en/communities/moderating-comments-and-conversations/locking-conversations |
| One sequence numbers a repository's issues and pull requests | documented | E `test_a_seed_that_github_could_never_hold_is_refused` | https://docs.github.com/en/rest/issues/issues#list-repository-issues |
| An author is associated with the repository as its OWNER, a MEMBER of the owning organization, a COLLABORATOR, a CONTRIBUTOR (has committed) or NONE | documented | I `test_an_issue_reads_with_the_urls_and_counts_github_assigns`, K `test_a_comment_is_kept_and_returned_as_sent` | https://docs.github.com/en/graphql/reference/enums#commentauthorassociation |
| Creating an issue comment answers 201, listing orders by ascending id and takes `since`, updating answers 200, deleting 204 | documented | K `test_a_comment_is_kept_and_returned_as_sent`, K `test_comments_list_by_ascending_id_and_the_issue_counts_them`, K `test_a_comment_is_edited_and_deleted_and_the_edit_moves_its_update_time` | https://docs.github.com/en/rest/issues/comments#create-an-issue-comment |
| A repository's comments list by ascending id, or by `sort` (created, updated) and `direction` | documented | K `test_a_repositorys_comments_list_by_id_or_sorted_with_a_direction` | https://docs.github.com/en/rest/issues/comments#list-issue-comments-for-a-repository |
| An issue comment on a pull request has `html_url` under `/pull/` and `issue_url` the issue's | observed | K `test_a_pull_requests_conversation_is_commented_on_through_the_issue_routes` | `tests/data/github_rest/observed-2026-10-09.json` |
| A repository's labels list alphabetically by name, upper and lower case together, whatever their ids | observed | K `test_labels_list_alphabetically_by_name_case_aside` | `tests/data/github_rest/observed-2026-10-09.json` |
| A label's `url` encodes its name: a space is `%20`, a colon is left as it is | observed | K `test_a_label_is_created_kept_as_sent_and_found_by_name` | `tests/data/github_rest/observed-2026-10-09.json` |
| Creating a label answers 201; a name that is taken is 422 `already_exists` ("such as label names"); the color is six hexadecimal digits and the description 100 characters or fewer | documented | K `test_a_label_is_created_kept_as_sent_and_found_by_name`, K `test_a_label_name_that_is_taken_is_422_already_exists`, K `test_a_label_with_a_color_or_description_outside_the_reference_is_422_invalid` | https://docs.github.com/en/rest/issues/labels#create-a-label |
| A label is never archived: `archived_at` and `archived_by` are null ("or `null` if it has not been archived") | documented | C `test_a_served_operation_answers_every_field_the_description_requires` | https://docs.github.com/en/rest/issues/labels#get-a-label |
| Adding labels returns the issue's labels, setting replaces them, removing one returns what remains, removing all answers 204; an array of names, an array of `{name}` and an object with `labels` all stand | documented | K `test_labels_are_added_set_listed_and_removed_on_an_issue` | https://docs.github.com/en/rest/issues/labels#add-labels-to-an-issue |
| Removing a label the issue does not carry is 404 ("returns a `404 Not Found` status if the label does not exist") | documented | K `test_removing_a_label_the_issue_does_not_carry_is_404` | https://docs.github.com/en/rest/issues/labels#remove-a-label-from-an-issue |
| An open issue assigned to a person waits on them; the issue's author and those with triage access or more may close it, and reopen it once closed; each is one move of its state in the log | documented | T `test_an_open_issue_assigned_to_a_person_is_pending_and_a_pinned_close_lands_as_theirs`, T `test_nobody_is_offered_a_move_github_would_refuse_them`, I `test_an_issue_opened_closed_and_reopened_by_the_agent_is_three_moves_of_its_state` | https://docs.github.com/en/rest/issues/issues#update-an-issue |
| A person closes an issue as completed or not planned; `duplicate` names the issue it duplicates, which a person is not offered | documented | T `test_a_close_names_why_it_was_closed_and_only_as_github_takes` | https://docs.github.com/en/rest/issues/issues#update-an-issue |

### Refused by name

Each of these is the reference's, not served, and answered 501 naming the parameter: the `mentioned` and
`issue_field_values` filters of the issue list; `milestone` (when it names one), `type`, `parent_issue_id`,
`issue_field_values` and `duplicate_issue_id` when sent with a value; an assignee who is not a user with a role on the
repository ("available assignees" is all the reference says); a label name the repository has not defined (the
reference does not say what GitHub does with one); a label created without a `color` (the reference's text calls it
required and its schema optional); a label added as a suggestion; commenting on a locked conversation without push
access; a comment list sorted without a direction (no default is given); reopening a merged pull request; a `since` that
is not ISO 8601 with a time zone; a `state`, `sort` or `direction` outside the reference's lists.

## `HEAD` as a ref

Each place the fake accepts a ref or a SHA, and what GitHub's public reference says of `HEAD` there. Tests in
`test_github_vendor_claims_head.py` (H). The REST reference names a SHA, a branch or a tag for every ref parameter
and never mentions `HEAD`, so a client that relies on `HEAD` there relies on something undocumented.

| Place | What the reference says | The fake | Test | Source |
|---|---|---|---|---|
| `GET /repos/{owner}/{repo}/commits?sha=` | "SHA or branch to start listing commits from." Not stated by the documentation; recorded as accepted (`observed/api.github.com.2026-10-08.json`) | Lists from the head commit, as the default branch | H `test_list_commits_with_sha_head_starts_at_the_default_branch` | https://docs.github.com/en/rest/commits/commits#list-commits |
| `GET /repos/{owner}/{repo}/contents/{path}?ref=` | "The name of the commit/branch/tag." Not stated by the documentation; recorded as accepted (`observed/api.github.com.2026-10-08.json`) | The default branch's content | H `test_repository_content_with_ref_head_is_the_default_branchs` | https://docs.github.com/en/rest/repos/contents#get-repository-content |
| `GET /repos/{owner}/{repo}/git/trees/{tree_sha}` | "The SHA1 value or ref (branch or tag) name of the tree." Not stated by the documentation; kept as accepted | The default branch's root tree | H `test_get_a_tree_named_head_is_the_root_tree_of_the_default_branch` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| GraphQL `Repository.object(expression:)` | Documented: "A Git revision expression suitable for rev-parse", which reads `HEAD` | `HEAD` and `HEAD:path` resolve at the head commit | H `test_graphql_object_expression_head_names_the_head_commit` | https://docs.github.com/en/graphql/reference/repos#repository |
| `GET /repos/{owner}/{repo}/commits/{ref}`, compare (`BASE...HEAD`) | "a commit SHA, branch name (heads/BRANCH_NAME), or tag name (tags/TAG_NAME)"; compare's `HEAD` is the head BRANCH's name, not the ref `HEAD` | Not served: 501, refused by name | R `test_an_unserved_path_is_refused_by_name_not_answered_as_github_s_404` | https://docs.github.com/en/rest/commits/commits#get-a-commit |

## Pending a recording

Each needs a credential to record (code search and GraphQL refuse a call without one), so none is proven:

- Code search's `score`: the description requires it (a number) and the reference says nothing of how it is
  computed. It is **not answered**, rather than made up (S `test_a_hit_carries_no_score_made_up_by_the_provider`;
  the coverage test names it as its one pending field).
- Code search's order: the reference says results are by "best match" unless `sort` says otherwise, without saying
  what that is; hits come in repository and path order here.
- The five "observed, not recorded" claims above: whole-token matching, every bare term must match, an empty `q`
  is 422 `missing`, a GraphQL repository that does not resolve is `null` with NOT_FOUND, and the missing-query
  message.

## What GitHub would refuse

Authentication is out of scope (`docs/design.md`, "Authentication is out of scope"). GitHub documents these refusals; this stack does not test them and answers the call instead.
Each test fails if the refusal comes back.

| GitHub refuses | Source | Minutehand | Test |
|---|---|---|---|
| An unknown or revoked token, 401 "Bad credentials" | https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api | Acts as the seed's `unknown_credentials_act_as`, by default its first user | F `test_any_credential_the_world_does_not_hold_acts_as_its_first_user`, F `test_the_seed_names_who_an_unknown_credential_acts_as` |
| An app's JWT on a route that needs a user or an installation | https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app | As an unknown token | F (first test, `app-jwt`) |
| `Basic` with a password, and `Bearer` with nothing after it, 401 | https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api | `Basic` acts as the token in its password, else as an unknown token; a bare `Bearer` as an unknown token | F `test_basic_authentication_is_accepted`, F (first test, `bearer-alone`) |
| A classic token without the `repo` scope reaches no private repository | https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps | Reaches every repository its user may; its scopes are only echoed in `X-OAuth-Scopes` | F `test_a_classic_token_without_the_repo_scope_reads_every_private_repository_its_user_may` |
| A fine-grained token reaches only the repositories it selected | https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens | Reaches every repository its user may; the seed no longer takes a selection | F `test_a_fine_grained_token_reads_everything_its_user_may_not_only_what_it_selected` |
| An installation-token exchange for an app that is not installed, or with a bad JWT | https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app | Always 201 | R `test_an_installation_token_is_issued_for_any_app_jwt_and_works_as_any_credential` |

GitHub also refuses a call with no `Authorization` at all, or an empty one, on `/user`, `/user/repos`, code search and
GraphQL (401), and serves it public data only, on 60 an hour
(https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api). Minutehand acts as the seed's
`unknown_credentials_act_as` instead, as for an unknown token; only a world with no user reads a call as nobody's
(F `test_a_call_with_no_authorization_acts_as_the_first_user_on_rest_graphql_and_search`, F (first test, `empty`)).

Visibility stays world data: a private repository its user does not own, collaborate on or reach through an
organization is a 404 (F `test_a_private_repository_without_access_is_not_found`).

## Refused by name

Every operation of `tests/providers/github/openapi/api.github.com.subset.json` that is not served is answered 501,
"minutehand's github fake does not implement <METHOD> <path>" (C `test_every_operation_the_provider_does_not_serve_is_refused_by_name`;
169 operations, 32 served, 137 refused, pinned by C `test_the_subset_holds_the_operations_it_is_counted_to_hold`), and so
is any other path. `X-GitHub-Api-Version: 2026-03-10`, which GitHub answers (recorded), is refused by name the same
way (F `test_an_api_version_github_serves_and_this_provider_does_not_is_refused_by_name`).

## Contradicted by the documentation (the old emulator was wrong)

- A call with no `Authorization` was refused 401 "Bad credentials" on every route. GitHub serves public data
  without a credential and refuses `/user` with "Requires authentication"
  (https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api).
- `query { viewer { login } }` was answered as an error. `viewer` is a root field and that query is the guide's
  first example (https://docs.github.com/en/graphql/guides/forming-calls-with-graphql).

## Not carried over

- **One budget for every caller** (the old emulator kept one window per resource, whoever called, on the machine's
  clock): GitHub's documentation puts the limit on the user, shared by every token acting as them, or on the
  address without a credential, and so does this provider, on the run's clock.
- **GraphQL priced by the query** (documented, simplified): every query costs one point here.
- **Admin routes** (emulator artefact): arming rate limits, lowering the tree limit, seeding a wide directory and
  resetting were HTTP routes of the emulator; here they are the seed (`faults`, `tree_entry_limit`, files).
- **Its fixed world** (emulator artefact): one owner, one repository and one bot login, canned commit ids.
- **Its GraphQL dialect** (emulator artefact): it answered only the few query shapes one client sent and a
  "parse error" for anything else; here the query is parsed and validated against the schema in `graphql.py`.
