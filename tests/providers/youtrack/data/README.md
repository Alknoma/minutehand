# Answers recorded from the real service

`observed/` holds what JetBrains' public YouTrack instance, `https://youtrack.jetbrains.com`, answered on
2026-10-08 to requests made with no credentials (the instance lets a guest read), each the whole HTTP answer as
`curl -si --http1.1` printed it. Writes need credentials there (a guest's `POST /api/issues` is a 403), so only
reads are recorded.

| File | Request |
|---|---|
| `unknown_issue.http` | `GET /api/issues/NOPE-999999?fields=id` |
| `unknown_project_custom_fields.http` | `GET /api/admin/projects/0-99999/customFields?fields=id` |
| `unknown_project_team.http` | `GET /api/admin/projects/0-99999/team?fields=id` |
| `query_value_not_used.http` | `GET /api/issues?query=State: Zzqqxx&fields=id&$top=1` |
| `query_project_not_used.http` | `GET /api/issues?query=project: NOPEZZ&fields=id&$top=1` |
| `query_sort_field_unknown.http` | `GET /api/issues?query=project: YTD sort by: Zzwibble&fields=id&$top=1` |
| `query_attribute_unknown.http` | `GET /api/issues?query=Zzwibble: High&fields=id&$top=1` |
| `query_parentheses.http` | `GET /api/issues?query=(State: Open)&fields=id&$top=1` |
| `activities_no_categories.http` | `GET /api/activities?fields=id` |
| `activities_unknown_category.http` | `GET /api/activities?categories=ZzNope&fields=id` |
| `issue_activities_bad_start.http` | `GET /api/issues/{an issue the instance holds}/activities?categories=CommentsCategory&start=x&fields=id` |
| `fields_syntax_invalid.http` | `GET /api/issues?fields=id,summary(&$top=1` |
| `fields_attribute_unknown.http` | `GET /api/issues?fields=id,zzzattr&$top=1` |
| `skip_not_a_number.http` | `GET /api/issues?fields=id&$skip=x&$top=1` |
| `skip_negative.http` | `GET /api/issues?fields=id&$skip=-1&$top=1` |
| `top_negative.http` | `GET /api/issues?fields=id&$top=-1` |
| `top_not_a_number.http` | `GET /api/issues?fields=id&$top=x` |
