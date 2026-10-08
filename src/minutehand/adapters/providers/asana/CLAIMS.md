# Asana: where each behaviour comes from

Each row is a fact about how Asana answers that this provider keeps, the test that pins it, and its source:
**documented** (Asana's reference or its OpenAPI document says so; the page is linked), **observed** (a recording of
the real service under `tests/data/asana_rest_1_0/`, or a public report quoting its answer; linked), or
**unsourced** (kept from an earlier stand-in, and neither the reference nor any recording or report found on
2026-10-08 states it; each is a candidate to be confirmed against the real service or turned into a refusal).
Paths are under `tests/providers/asana/`; `claims` is `test_asana_vendor_claims.py`, `fidelity` is
`test_asana_fidelity.py`.

The surface is Asana's OpenAPI document (https://github.com/Asana/openapi, `defs/asana_oas.yaml` at `4cf6c7c`),
whose subset for the resources this provider claims is `tests/data/asana_rest_1_0/openapi-subset-2026-10-08.json`:
122 operations, 51 served and 71 answered 501 naming the method, path and operationId (`app.UNSERVED`,
`test_asana_surface.py`).

## Credentials

Minutehand deliberately does not enforce credentials. Asana answers a call with no token, or a token that is not
one, 401 "Not Authorized" (`tests/data/asana_rest_1_0/real-service-without-a-token-2026-10-08.txt`); this provider
answers it. A token the scenario seeded, or `/-/oauth_token` minted, acts as its user; any other token, or none, acts
as the agent. Removed and never refused: the 401 for a missing or unknown token (and `AsanaSeed.tokens` making the
workspace strict), token expiry (`SeedToken.expires_after`), the 401 for a removed user's token, and the refresh's
`invalid_grant` for a refresh token nobody seeded (it now buys a token for the agent). What the world holds still
decides what a caller sees: a private project is 403 to a non-member.

## Kept

| Claim | Class | Pinned by | Source |
|---|---|---|---|
| A call with no token, or any token, is answered, as the agent unless the token is a seeded or minted one | Minutehand's own: credentials are not enforced | `test_asana_refusals.py::test_a_call_with_no_token_or_any_token_acts_as_the_agent`, `test_asana_parity_refusals.py::test_a_token_nobody_seeded_acts_as_the_agent`, `claims::test_a_write_with_no_token_is_answered_as_the_agent` | `tests/data/asana_rest_1_0/real-service-without-a-token-2026-10-08.txt` (what Asana answers instead) |
| A path with no route, and a method a path does not take, are 404 "No matching route for request" | observed | `test_asana_refusals.py::test_an_unknown_route_and_a_method_a_path_does_not_take_are_refused_no_matching_route` | `tests/data/asana_rest_1_0/real-service-without-a-token-2026-10-08.txt` |
| Every answer is `application/json; charset=UTF-8`; an error is `{"errors": [{"message", "help"}]}` | observed | `asana_workspace.error` (every refusal test) | `tests/data/asana_rest_1_0/real-service-without-a-token-2026-10-08.txt`, https://developers.asana.com/docs/errors |
| A body field outside `data` is 400 "Unrecognized request field X . The only allowed keys at the top level are: data, options. ..." | observed | `claims::test_a_write_not_wrapped_in_data_is_refused_naming_the_stray_field` | https://forum.asana.com/t/238695 |
| A body with no `data` object at all is 400 "Missing input: data" | unsourced | `test_asana_refusals.py::test_a_write_without_its_data_wrapper_is_refused` | |
| `opt_fields` answers exactly the fields it names, and `gid` | documented | `fidelity::test_opt_fields_answers_exactly_what_it_names_and_the_gid` | https://developers.asana.com/docs/inputoutput-options |
| An `opt_fields` path may start with `this.` and group terms `(a\|b)` | documented | `fidelity::test_opt_fields_paths_may_start_with_this_and_group_terms` | https://developers.asana.com/docs/inputoutput-options |
| A field `opt_fields` names that this provider never answers (one Asana has and is not served, one Asana's document does not give the resource, a path into a value) is 501 naming it | Minutehand's own: refused by name, never dropped | `fidelity::test_a_field_this_fake_never_answers_is_refused_501_naming_it` | Asana's behaviour for an unknown field is not documented |
| A full record leaves out what Asana's OpenAPI document marks [Opt In] (`num_subtasks`; a team's `description`; a project membership's `parent` and `project`) until `opt_fields` names it | documented | `fidelity::test_a_full_task_leaves_out_what_is_opt_in_until_it_is_asked_for`, `fidelity::test_a_project_membership_answers_neither_an_invented_access_level_nor_opt_in_fields` | `TaskBase`, `TeamResponse`, `ProjectMembershipCompact` in the OpenAPI subset |
| A full task holds its custom fields full; a listed custom field is compact, without `resource_subtype` or `precision` | documented | `fidelity::test_a_full_task_holds_its_custom_fields_full_and_a_listed_one_compact` | `TaskResponse.custom_fields`, `CustomFieldCompact` in the OpenAPI subset |
| A task's name, notes and `due_at`, and a custom field's number and `date_time`, come back as they were sent | Minutehand's own: data as sent (Asana's reference shows `due_at` only in its own `...000Z` form, and no report says whether it rewrites one) | `fidelity::test_a_task_comes_back_with_its_words_as_they_were_sent`, `test_asana_api.py::test_update_changes_only_what_was_sent` | https://developers.asana.com/reference/gettask |
| `due_on` of a task given `due_at`, and `date` of a date field given `date_time`, is that moment's date in UTC | documented, with the world's people in UTC | `test_asana_api.py::test_update_changes_only_what_was_sent` | `TaskBase.due_on` ("the localized date") in the OpenAPI subset |
| `html_notes` is refused by name, written or read | documented that the two are one text; how either is derived from the other is not | `test_asana_parity_api.py::test_html_notes_are_refused_by_name_written_or_read` | https://developers.asana.com/docs/rich-text |
| Sending both `due_on` and `due_at` is 400 "You may only provide one of due_on or due_at!" | observed | `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it` | https://forum.asana.com/t/808508 |
| An unknown object named in a path is 404 "<resource>: Unknown object: <gid>" | observed | `test_asana_parity_api.py` (a deleted task) | https://forum.asana.com/t/72142, https://forum.asana.com/t/91831 |
| An assignee nobody in the organization is is 400 "assignee: Not a user in Organization: <as sent>" | observed | `test_asana_refusals.py::test_an_unknown_assignee_is_refused` | https://forum.asana.com/t/60069 |
| A listing too large to answer without `limit` is 400 "The result is too large. You should use pagination (may require specifying a workspace)!"; past 1,000 items | observed | `claims::test_an_unpaginated_listing_too_large_to_answer_is_refused_and_a_paged_one_is_not` | https://forum.asana.com/t/21374, https://forum.asana.com/t/29153; "truncated at about 1,000 objects", https://developers.asana.com/docs/pagination |
| A paged listing carries `next_page` `{offset, path, uri}`, null on the last page | documented | `test_asana_api.py::test_an_unpaginated_list_has_a_null_next_page`, the conformance listing property | https://developers.asana.com/docs/pagination |
| Every call in a throttled stretch is 429 with `Retry-After` and "You've made too many requests and hit a rate limit. Please retry after the given amount of time." | documented | `test_asana_parity_refusals.py::test_every_call_is_answered_429_with_retry_after_while_throttled` | https://developers.asana.com/docs/rate-limits |
| A listed user is compact (no email); `opt_fields=email` brings the field | documented | `test_asana_api.py::test_users_carry_no_email_until_it_is_asked_for` | https://developers.asana.com/reference/getusersforworkspace |
| A tag is named by its gid; a name in its place is "tag: Not a Recognized ID" | documented (the gid), unsourced (the words) | `test_asana_parity_refusals.py::test_a_tag_or_parent_that_names_nothing_is_refused` | https://developers.asana.com/reference/addtagfortask |
| `completed` must be a JSON boolean, on create and on update | documented | `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it`, `claims::test_an_update_whose_completed_is_not_a_json_boolean_is_refused` | https://developers.asana.com/reference/updatetask |
| Search is GET only, `limit` is 1 to 100, an unknown parameter or a non-id assignee is a 400; a parameter Asana has and this provider does not is 501 naming it | documented | `test_asana_refusals.py::test_search_rejects_what_it_does_not_know_and_its_limit` | https://developers.asana.com/reference/searchtasksforworkspace |
| Search and custom fields on a workspace that is not premium are 402 | documented | `test_asana_parity_refusals.py::test_a_free_workspace_answers_402_to_search_and_custom_fields` | https://developers.asana.com/docs/errors, https://developers.asana.com/reference/searchtasksforworkspace |
| A project listing without `archived` includes archived projects; `archived` filters both ways | documented | `claims::test_a_project_listing_without_archived_includes_archived_projects_and_the_filter_narrows_it` | https://developers.asana.com/reference/getprojectsforworkspace |
| A project's custom field settings are compact (gid, resource_type) until `opt_fields` asks for the field | documented | `claims::test_custom_field_settings_are_compact_until_the_field_is_asked_for` | https://developers.asana.com/reference/getcustomfieldsettingsforproject |
| `projects` on a create is an array of gids | documented | `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it` | https://developers.asana.com/reference/createtask |
| A due date sent on create is stored | documented | `test_asana_api.py::test_update_changes_only_what_was_sent` | https://developers.asana.com/reference/createtask |
| A task in two projects has a membership in each | documented | `claims::test_a_task_created_in_two_projects_is_a_member_of_both` | https://developers.asana.com/reference/gettask |
| A subtask never added to a project has no membership | documented | `claims::test_a_subtask_never_added_to_a_project_has_no_membership` | `TaskBase.projects` ("directly associated") in the OpenAPI subset; https://forum.asana.com/t/26339 |
| `addTask` within the task's own project moves it, with no second membership | documented | `test_asana_parity_api.py::test_add_task_moves_it_to_the_section_and_status_is_read_from_the_field` | https://developers.asana.com/reference/addtaskforsection |
| A project create naming no workspace (and no team to find one) is a 400 | documented | `claims::test_a_project_create_naming_no_workspace_is_refused_missing_input` | https://developers.asana.com/reference/createproject |
| `addCustomFieldSetting` needs a field's gid that the workspace defines; a name, an unknown gid or none is a 400 | documented | `test_asana_parity_refusals.py::test_a_field_already_on_a_project_or_unknown_is_refused_400`, `claims::test_a_custom_field_setting_naming_a_field_by_its_name_is_refused_not_a_recognized_id` | https://developers.asana.com/reference/addcustomfieldsettingforproject |
| `addCustomFieldSetting` on a project that is not there is a 404 | documented | `claims::test_a_custom_field_setting_on_a_project_that_is_not_there_is_refused_404` | https://developers.asana.com/docs/errors |
| In an organization, a project must name a team | documented | `test_asana_parity_refusals.py::test_an_organization_refuses_a_project_with_no_team` | https://developers.asana.com/reference/createproject |
| A created project answers its team, and reads back with it; a bare single read is the full record, team included | documented | `claims::test_a_created_project_reads_back_with_its_team_and_a_full_read_carries_it_unasked` | https://developers.asana.com/reference/getproject |
| A listed workspace is compact; `is_organization` is answered when asked, and on a bare single read | documented | `claims::test_a_full_workspace_read_says_whether_it_is_an_organization_unasked` | https://developers.asana.com/reference/getworkspace |
| `/users/me/teams` requires `organization`; missing or unknown is a 400; it lists the caller's teams | documented | `test_asana_parity_refusals.py::test_my_teams_need_an_organization`, `test_asana_parity_api.py::test_my_teams_are_the_teams_i_am_in_of_that_organization` | https://developers.asana.com/reference/getteamsforuser |
| In a workspace that is not an organization, a project needs no team and answers `team: null` | documented | `claims::test_a_project_in_a_plain_workspace_needs_no_team_and_has_none` | https://developers.asana.com/reference/createproject |
| A project's name missing or blank is "name: Missing input" | observed (the message's form, on another create) | `claims::test_a_blank_project_name_is_refused_missing_input` | https://forum.asana.com/t/99023, and "workspace: Missing input" on https://developers.asana.com/docs/errors |
| A new project has one "Untitled section" and no custom fields | observed (the section) | `test_asana_parity_api.py::test_a_created_project_has_one_untitled_section_no_fields_and_its_creator_as_member` | https://forum.asana.com/t/1062521 |
| Completing a task moves it to no section and rewrites no custom field | unsourced | `claims::test_completing_a_task_leaves_its_section_and_its_status_field_where_they_were` | |
| An assignee that is not a gid, an email or `me` is "assignee: Not a Recognized ID" | unsourced | `test_asana_refusals.py::test_an_unknown_assignee_is_refused` | |
| `setParent` refuses the task itself or its own subtask as the parent | unsourced | `test_asana_parity_refusals.py::test_a_tag_or_parent_that_names_nothing_is_refused` | |
| `setParent` naming a task that does not exist is a 400 | unsourced (the "<field>: Unknown object" form is reported for `projects`: https://forum.asana.com/t/580616) | `claims::test_set_parent_to_a_task_that_does_not_exist_is_refused_400` | |
| A comment with no text, or only spaces, is a 400 "Missing input: text" | unsourced | `test_asana_refusals.py::test_a_comment_with_nothing_to_say_is_refused` | |
| `addTask` on another project's section adds that project and keeps the first | unsourced | `test_asana_parity_api.py::test_a_task_in_a_section_of_another_project_joins_that_project` | |
| A project in a workspace or team that is not there is a 400 naming the gid | unsourced | `claims::test_a_project_in_a_workspace_or_team_that_is_not_there_is_refused_unknown_object` | |
| A field already on the project is refused 400 | unsourced | `test_asana_parity_refusals.py::test_a_field_already_on_a_project_or_unknown_is_refused_400` | |
| In a workspace that is not an organization, asking for the caller's teams is a 400 "organization: Not an organization" | unsourced | `claims::test_a_plain_workspace_refuses_to_list_my_teams_not_an_organization` | |
| A gid that is not digits is 400 "<resource>: Not a Recognized ID" | unsourced (a report shows "workspace: Not a Long: we", status not shown: https://forum.asana.com/t/19570) | `test_asana_refusals.py` (path parameters) | |

## Not carried over

- **Credential enforcement** (see Credentials): every 401, expiry, strict tokens and the refresh's `invalid_grant`.
- **`html_notes` read as the text it shows, and synthesised from `notes`; a story's `html_text` synthesised from
  `text`.** Asana documents neither conversion (https://developers.asana.com/docs/rich-text); both are refused by
  name instead.
- **A task's `subtasks` field.** Not in Asana's `TaskResponse`; subtasks are read at `/tasks/{gid}/subtasks`, and
  `opt_fields=subtasks` is refused by name.
- **A project membership's `access_level: "editor"` and `write_access: "full_write"`, and a custom field's
  `has_notifications_enabled: false`.** Constants nothing in the world holds; refused by name when asked for.
- **`due_at` and a date field's `date_time` rewritten to `...000Z`.** Kept as sent.
- **405 "Method not allowed"** for a method a path does not take: Asana answers 404 (recorded).
- **The earlier wording "Missing input: data" for a body with stray fields, "Cannot specify both due_on and due_at",
  "assignee: Unknown object" in an organization, and the 429 "Please, be chill."**: replaced by Asana's documented or
  reported words above.
- **A bare `GET /projects/{gid}` answered compact, without `team`**, and **a bare `GET /workspaces/{gid}` without
  `is_organization`**: contradicted by the reference (a single resource is its full record).
- **Admin routes that reset the world or rewrite the workspace's limits.** Asana has no such routes; here those are
  the scenario's seed (`AsanaSeed.workspace`).
- **Fixed gids and a fixture service account with no email.** Data of the predecessor's fixture files, not
  behaviour of Asana; gids here derive from the scenario's names.
