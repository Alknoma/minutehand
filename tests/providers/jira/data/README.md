# Answers recorded from the real service

`observed/` holds what Jira Cloud answered on 2026-10-08, each the whole HTTP answer as `curl -si --http1.1`
printed it, less `Set-Cookie`. The requests were made with no credentials: to a public
Jira Cloud site that lets anonymous callers read (`/rest/api/3/...`; its project key is written here as `PUB` and its host is left out), and to Atlassian's own
`auth.atlassian.com` and `api.atlassian.com`. A site checks a body before it refuses an anonymous write, so some
write refusals are recorded too; `comment_body_not_a_document.http` carries the anonymous caller's refusal beside
the body's.

These are answers to an anonymous caller. Where a signed-in caller may be answered otherwise (a query naming a
field the site has not got answered with no issues, not a 400), nothing here says so; the fake answers as recorded.

| File | Request |
|---|---|
| `unknown_issue.http` | `GET /rest/api/3/issue/NOPE-999999` |
| `unknown_project.http` | `GET /rest/api/3/project/NOPEZZQ` |
| `jql_field_unknown.http` | `GET /rest/api/3/search/jql?jql=colour%20%3D%20red` |
| `jql_value_unknown.http` | `GET /rest/api/3/search/jql?jql=status%20%3D%20%22Zzqq%22` |
| `jql_function_unknown.http` | `GET /rest/api/3/search/jql?jql=assignee%20%3D%20noSuchFunction()` |
| `jql_function_wrong_field.http` | `GET /rest/api/3/search/jql?jql=status%20%3D%20currentUser()` |
| `jql_operator_unsupported.http` | `GET /rest/api/3/search/jql?jql=text%20%3D%20%22export%22` |
| `jql_date_invalid.http` | `GET /rest/api/3/search/jql?jql=updated%20%3E%3D%20yesterday` |
| `jql_key_unknown.http` | `GET /rest/api/3/search/jql?jql=key%20%3D%20PUB-99999999` |
| `jql_project_unknown.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20NOPEZZQ` |
| `jql_order_field_unknown.http` | `GET /rest/api/3/search/jql?jql=issuetype%20%3D%20Bug%20ORDER%20BY%20zzfield&fields=id&maxResults=1` |
| `jql_was.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB%20AND%20status%20WAS%20Open` |
| `jql_expecting_field.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB%20AND` |
| `jql_quote_unclosed.http` | `GET /rest/api/3/search/jql?jql=summary%20~%20%22unclosed` |
| `jql_parenthesis_unclosed.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB%20AND%20(status%20%3D%20Open` |
| `jql_unbounded.http` | `GET /rest/api/3/search/jql?jql=order%20by%20key` |
| `search_max_results_zero.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB&maxResults=0` |
| `search_max_results_over.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB&maxResults=6000&fields=id` |
| `search_page_token_invalid.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB&nextPageToken=garbage` |
| `search_retired.http` | `GET /rest/api/3/search` |
| `mypermissions_no_keys.http` | `GET /rest/api/3/mypermissions` |
| `mypermissions_unknown_key.http` | `GET /rest/api/3/mypermissions?permissions=LAUNCH_ROCKETS` |
| `mypermissions_unknown_project.http` | `GET /rest/api/3/mypermissions?permissions=BROWSE_PROJECTS&projectKey=orb_it` |
| `comments_order_unknown.http` | `GET /rest/api/3/issue/PUB-1/comment?orderBy=author` |
| `comments_max_results_not_a_number.http` | `GET /rest/api/3/issue/PUB-1/comment?maxResults=x` |
| `user_search_no_query.http` | `GET /rest/api/3/user/search` |
| `user_search_query_and_account.http` | `GET /rest/api/3/user/search?query=a&accountId=x` |
| `assignable_no_project.http` | `GET /rest/api/3/user/assignable/search` |
| `issue_link_unknown.http` | `GET /rest/api/3/issueLink/99999999` |
| `project_search_order_unknown.http` | `GET /rest/api/3/project/search?orderBy=zz` |
| `approximate_count_get.http` | `GET /rest/api/3/search/approximate-count` |
| `transitions_unknown_id.http` | `GET /rest/api/3/issue/PUB-1/transitions?transitionId=999` |
| `issue_create_empty.http` | `POST /rest/api/3/issue` body `` |
| `issue_create_not_json.http` | `POST /rest/api/3/issue` body `not json` |
| `issue_create_not_object.http` | `POST /rest/api/3/issue` body `[1]` |
| `issue_create_no_project.http` | `POST /rest/api/3/issue` body `{"fields":{}}` |
| `issue_create_no_type.http` | `POST /rest/api/3/issue` body `{"fields":{"project":{"key":"PUB"}}}` |
| `issue_edit_not_on_screen.http` | `PUT /rest/api/3/issue/PUB-1` body `{"fields":{"summary":"x"}}` |
| `comment_body_not_a_document.http` | `POST /rest/api/3/issue/PUB-1/comment` body `{"body":"plain"}` |
| `transition_missing.http` | `POST /rest/api/3/issue/PUB-1/transitions` body `{}` |
| `link_type_unknown.http` | `POST /rest/api/3/issueLink` body `{"type":{"name":"Haunts"},"inwardIssue":{"key":"PUB-1"},"outwardIssue":{"key":"PUB-2"}}` |
| `search_body_unknown_property.http` | `POST /rest/api/3/search/jql` body `{"jql":"project = PUB","colour":1}` |
| `search_body_wrong_type.http` | `POST /rest/api/3/search/jql` body `{"jql":"project = PUB","maxResults":"x"}` |
| `count_unbounded.http` | `POST /rest/api/3/search/approximate-count` body `{"jql":"order by key"}` |
| `unknown_path.http` | `GET /rest/api/3/issue/PUB-1/no-such-thing` |
| `jql_value_unexpected.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB%20AND%20status%20%3D%20%3D%20Open` |
| `jql_and_or_expected.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB%20status` |
| `jql_value_missing.http` | `GET /rest/api/3/search/jql?jql=project%20%3D` |
| `jql_is_not_empty_value.http` | `GET /rest/api/3/search/jql?jql=status%20is%20Open` |
| `jql_text_no_word.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB%20AND%20text%20~%20%22%3F%3F%22` |
| `jql_period_invalid.http` | `GET /rest/api/3/search/jql?jql=project%20%3D%20PUB%20AND%20created%20%3E%3D%20startOfDay(zz)` |
| `token_unsupported_grant.http` | `POST https://auth.atlassian.com/oauth/token` (application/json) body `{"grant_type":"password"}` |
| `token_unreadable.http` | `POST https://auth.atlassian.com/oauth/token` (application/json) body `nope` |
| `gateway_unknown_cloud_id.http` | `GET https://api.atlassian.com/ex/jira/00000000-0000-0000-0000-000000000000/rest/api/3/myself` |
| `gateway_unknown_path.http` | `GET https://api.atlassian.com/nothing-here` |
| `site_unknown.http` | `GET https://zzqq-no-such-site-4711.atlassian.net/rest/api/3/myself` |
