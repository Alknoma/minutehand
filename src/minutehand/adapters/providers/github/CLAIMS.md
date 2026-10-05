# GitHub provider: vendor claims

The behaviours below were each asserted by a test of an older, home-grown GitHub emulator. Each is a fact somebody
learned about api.github.com; this provider keeps it, and the named test fails if it stops. **Documented** means
GitHub's public reference says so (the page is given); **observed** means the reference does not settle it and the
claim rests on what was seen from the real service. Tests are in `tests/providers/github/`, files
`test_github_vendor_claims_rest.py` (R), `test_github_vendor_claims_search.py` (S),
`test_github_vendor_claims_graphql.py` (G), `test_github_vendor_claims_budget.py` (B) and
`test_github_people_and_ids.py` (P).

## Ported

| Claim | Class | Test | Source |
|---|---|---|---|
| A user's `id` is a positive integer; a person's declared id is answered as theirs | documented | P `test_a_persons_account_entry_is_a_github_user_with_its_declared_login_and_id` | https://docs.github.com/en/rest/users/users#get-the-authenticated-user |
| A login is letters, digits and single inner hyphens, at most 39 characters; a dotted login is refused at load | documented | P `test_a_login_github_does_not_allow_is_refused` | https://docs.github.com/en/enterprise-cloud@latest/admin/managing-iam/iam-configuration-reference/username-considerations-for-external-authentication |
| `email` is null when the user keeps their email private (or has none public) | documented | P `test_a_private_email_reads_null`, `test_a_person_with_no_email_reads_null_and_shows_the_entrys_name` | https://docs.github.com/en/rest/users/users#get-the-authenticated-user |
| No credential: public data is served, `/user` is 401 "Requires authentication" | documented | R `test_without_any_credential_the_user_is_refused_requires_authentication` | https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api |
| Basic authentication with a password is 401 | documented | R `test_basic_authentication_with_a_password_is_refused_401` | https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api |
| `Bearer` with no token is 401 "Bad credentials" | observed | R `test_a_bearer_scheme_with_no_token_after_it_is_refused_401` | |
| `Authorization: Bearer <token>` acts as the token's user | documented | R `test_a_bearer_token_is_served_as_its_user` | https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api |
| Every answer carries `X-RateLimit-Limit/Remaining/Used/Reset/Resource`; core is 5,000 | documented | R `test_every_answer_carries_the_rate_limit_headers_for_its_budget` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| Without a credential the budget is 60 an hour | documented | R `test_an_unauthenticated_answer_carries_the_sixty_an_hour_budget` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| Code search spends its own budget, `code_search`, 10 a minute | documented | R `test_code_search_reports_its_own_ten_a_minute_budget` | https://docs.github.com/en/rest/search/search#search-code |
| Each call spends one from its budget; the reset moment stays put through the window; a 404 spends too | documented | B `test_each_call_spends_one_from_the_users_budget_and_the_reset_stays_put` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| The budget is the user's, shared by all their tokens, not the token's | documented | B `test_two_tokens_of_one_user_spend_one_budget_and_another_user_has_their_own` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| Core, code search and GraphQL are separate budgets | documented | B `test_each_resource_is_its_own_budget` | https://docs.github.com/en/rest/rate-limit/rate-limit |
| The call after the last is 403 "API rate limit exceeded", `Remaining: 0`, until `X-RateLimit-Reset`; the budget is whole after it | documented | B `test_the_call_after_the_last_is_refused_403_until_the_reset_on_the_runs_clock` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| Without a credential the address's budget is spent and refused the same way | documented | B `test_without_a_credential_the_addresss_budget_is_spent_and_refused_403` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| A spent GraphQL budget is a 200 whose errors say RATE_LIMITED | documented | B `test_a_spent_graphql_budget_answers_200_with_rate_limited_errors` | https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api |
| `GET /rate_limit` reports every budget, `rate` as core, and does not count | documented | B `test_rate_limit_reports_every_budget_and_spends_none` | https://docs.github.com/en/rest/rate-limit/rate-limit |
| A secondary limit is 403 or 429 with `Retry-After`, then calls go through | documented | G `test_a_secondary_limit_is_a_403_or_429_with_retry_after_and_then_the_call_goes_through` | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| An unknown or unreachable repository is 404 on every repository route | documented | R `test_an_unknown_repository_is_not_found_on_every_route` | https://docs.github.com/en/rest/repos/repos#get-a-repository |
| `/user/repos` lists only what the token reaches, with `permissions` | documented | R `test_user_repos_lists_only_what_the_token_can_see_with_its_permissions` | https://docs.github.com/en/rest/repos/repos#list-repositories-for-the-authenticated-user |
| `/languages` is bytes of code per language | documented | R `test_languages_counts_bytes_of_code_leaving_out_prose_and_vendored_files` | https://docs.github.com/en/rest/repos/repos#list-repository-languages |
| `/languages` leaves out prose and vendored paths | observed | (same test) | |
| `/branches` lists name, commit, `protected` | documented | R `test_branches_list_each_branch_with_its_commit` | https://docs.github.com/en/rest/branches/branches#list-branches |
| Contents at a ref naming no commit is 404 "No commit found for the ref …" | observed | R `test_contents_at_a_ref_that_names_no_commit_is_refused_404` | |
| Contents `ref` takes a branch or a commit sha | documented | R `test_contents_takes_a_branch_or_a_commit_sha_as_its_ref` | https://docs.github.com/en/rest/repos/contents#get-repository-content |
| A file over 1 MiB answers `content: ""`, `encoding: none` | documented | R `test_a_file_over_a_mebibyte_answers_no_content_and_encoding_none` | https://docs.github.com/en/rest/repos/contents#get-repository-content |
| The blob endpoint carries those bytes, same size | documented | R `test_the_blob_endpoint_carries_the_bytes_the_contents_endpoint_left_out` | https://docs.github.com/en/rest/git/blobs#get-a-blob |
| A directory listing stops at 1,000 entries and says nothing | documented | R `test_a_directory_listing_stops_at_a_thousand_entries_and_says_nothing` | https://docs.github.com/en/rest/repos/contents#get-repository-content |
| Commits from a sha naming nothing are 404 "No commit found for SHA: …" | observed | R `test_commits_from_a_sha_that_names_no_commit_are_refused_404` | |
| Commits filter by `path` | documented | R `test_commits_filter_by_path` | https://docs.github.com/en/rest/commits/commits#list-commits |
| Commits honour `per_page` | documented | R `test_commits_honour_per_page` | https://docs.github.com/en/rest/commits/commits#list-commits |
| A listed commit has author email and `html_url`, and no `files` | observed | R `test_a_listed_commit_carries_its_author_and_link_and_no_file_list` | |
| A tree without `recursive` is one level | documented | R `test_a_tree_without_recursive_is_one_level_deep` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| A recursive tree holds every object | documented | R `test_a_recursive_tree_holds_every_object` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| A tree at a ref naming nothing is 404 | documented | R `test_a_tree_at_a_ref_that_names_nothing_is_not_found` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| A tree past its limit is cut and `truncated: true` | documented | R `test_a_tree_past_its_entry_limit_is_answered_partial_and_marked_truncated` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| Search matches whole tokens, never a substring | observed | S `test_a_substring_of_a_token_does_not_match` | |
| Every bare term must match | observed | S `test_every_term_of_the_query_has_to_match` | |
| `repo:` scopes the search | documented | S `test_a_repo_qualifier_scopes_the_search_and_one_the_token_cannot_see_is_refused_422` | https://docs.github.com/en/search-github/searching-on-github/searching-code |
| An unreachable `repo:` is 422 "cannot be searched" | observed | (same test) | |
| `language:` filters by language | documented | S `test_a_language_qualifier_keeps_only_files_in_that_language` | https://docs.github.com/en/search-github/searching-on-github/searching-code |
| Qualifiers with no term are 422 `invalid` | documented | S `test_a_query_of_qualifiers_alone_is_refused_422_invalid` | https://docs.github.com/en/rest/search/search#search-code |
| An empty `q` is 422 `missing` | observed | S `test_an_empty_query_is_refused_422_missing` | |
| Results page by `per_page`/`page`, same `total_count` on each page | documented | S `test_results_are_paged_and_every_page_reports_the_same_total` | https://docs.github.com/en/rest/search/search#search-code |
| `text_matches` only under the text-match media type | documented | S `test_text_matches_come_only_when_the_text_match_media_type_is_asked_for` | https://docs.github.com/en/rest/search/search#text-match-metadata |
| Files of 384 KB or more are not searchable | documented | S `test_a_file_past_the_index_ceiling_is_not_searchable_though_it_reads_inline` | https://docs.github.com/en/rest/search/search#search-code |
| GraphQL `variables` name the repository | documented | G `test_variables_name_the_repository_a_query_reads` | https://docs.github.com/en/graphql/guides/forming-calls-with-graphql |
| An unresolvable repository is `null` with a NOT_FOUND error | observed | G `test_a_repository_that_does_not_resolve_is_null_with_a_not_found_error` | |
| A binary blob's `text` is null | documented | G `test_a_binary_blob_has_null_text` | https://docs.github.com/en/graphql/reference/git#blob |
| `object(expression:)` naming no path is null | documented | G `test_an_expression_naming_no_path_is_null` | https://docs.github.com/en/graphql/reference/objects#repository |
| `viewer` is the token's user | documented | G `test_the_viewer_is_the_token_s_user` | https://docs.github.com/en/graphql/guides/forming-calls-with-graphql |
| A body with no string `query` is 400 | documented | G `test_a_body_without_a_query_is_refused_400` | https://docs.github.com/en/graphql/guides/forming-calls-with-graphql |
| …and the message says a query attribute must be specified | observed | (same test) | |

## `HEAD` as a ref

Each place the fake accepts a ref or a SHA, and what GitHub's public reference says of `HEAD` there. Tests in
`test_github_vendor_claims_head.py` (H). The REST reference names a SHA, a branch or a tag for every ref parameter
and never mentions `HEAD`, so a client that relies on `HEAD` there relies on something undocumented.

| Place | What the reference says | The fake | Test | Source |
|---|---|---|---|---|
| `GET /repos/{owner}/{repo}/commits?sha=` | "SHA or branch to start listing commits from." Not stated by the documentation; kept as accepted | Lists from the head commit, as the default branch | H `test_list_commits_with_sha_head_starts_at_the_default_branch` | https://docs.github.com/en/rest/commits/commits#list-commits |
| `GET /repos/{owner}/{repo}/contents/{path}?ref=` | "The name of the commit/branch/tag." Not stated by the documentation; kept as accepted | The default branch's content | H `test_repository_content_with_ref_head_is_the_default_branchs` | https://docs.github.com/en/rest/repos/contents#get-repository-content |
| `GET /repos/{owner}/{repo}/git/trees/{tree_sha}` | "The SHA1 value or ref (branch or tag) name of the tree." Not stated by the documentation; kept as accepted | The default branch's root tree | H `test_get_a_tree_named_head_is_the_root_tree_of_the_default_branch` | https://docs.github.com/en/rest/git/trees#get-a-tree |
| GraphQL `Repository.object(expression:)` | Documented: "A Git revision expression suitable for rev-parse", which reads `HEAD` | `HEAD` and `HEAD:path` resolve at the head commit | H `test_graphql_object_expression_head_names_the_head_commit` | https://docs.github.com/en/graphql/reference/repos#repository |
| `GET /repos/{owner}/{repo}/commits/{ref}`, compare (`BASE...HEAD`) | "a commit SHA, branch name (heads/BRANCH_NAME), or tag name (tags/TAG_NAME)"; compare's `HEAD` is the head BRANCH's name, not the ref `HEAD` | Not served: 404 as an unknown route | | https://docs.github.com/en/rest/commits/commits#get-a-commit |

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
