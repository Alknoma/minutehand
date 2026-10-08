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
