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
(F), `test_github_coverage.py` (C) and `test_github_world_without_users.py` (W).

Minutehand deliberately does not enforce credentials: every `Authorization`, or none, is accepted, and nothing refuses a call
for what a token was or was not issued for. The claims GitHub documents about refusing credentials are listed
under "Deliberately not enforced" below, with the tests that hold the provider to accepting them.

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

## Deliberately not enforced

GitHub documents these refusals; Minutehand accepts the call instead, because it does not enforce credentials.
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
169 operations, 12 served, 157 refused, pinned by C `test_the_subset_holds_the_operations_it_is_counted_to_hold`), and so
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
