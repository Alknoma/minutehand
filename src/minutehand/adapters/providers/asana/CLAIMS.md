# Asana: where each behaviour comes from

Each row is a fact about how Asana answers that this provider keeps, the test that pins it, and its source:
**documented** (Asana's reference or its OpenAPI document says so; the page is linked), **observed** (a recording of
the real service under `tests/data/asana_rest_1_0/`, or a public report quoting its answer; linked), or
Minutehand's own (a choice the rules make: credentials not enforced, what is not served refused by name). A case no
page, recording or report gives Asana's answer to is not answered: it raises the shared not-served refusal (501,
naming the case and saying Asana's answer to it is not documented), as every operation and feature this provider does
not serve does.
Paths are under `tests/providers/asana/`; `claims` is `test_asana_vendor_claims.py`, `fidelity` is
`test_asana_fidelity.py`.

The surface is Asana's OpenAPI document (https://github.com/Asana/openapi, `defs/asana_oas.yaml` at `4cf6c7c`),
whose subset for the resources this provider claims is `tests/data/asana_rest_1_0/openapi-subset-2026-10-08.json`:
122 operations, 51 served and 71 refused by name. The provider holds the whole document's 251 operations
(`surface.OPERATIONS`): 51 served, 200 raising the shared not-served refusal naming method, path and operationId
(`app.UNSERVED`, `test_asana_surface.py`); only a path Asana does not document is 404 "No matching route for
request".

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
| `opt_fields` answers exactly the fields it names, and `gid` | documented | `fidelity::test_opt_fields_answers_exactly_what_it_names_and_the_gid` | https://developers.asana.com/docs/inputoutput-options |
| An `opt_fields` path may start with `this.` and group terms `(a\|b)` | documented | `fidelity::test_opt_fields_paths_may_start_with_this_and_group_terms` | https://developers.asana.com/docs/inputoutput-options |
| A field `opt_fields` names that this provider never answers (one Asana has and is not served, one Asana's document does not give the resource, a path into a value) is refused by name | Minutehand's own: refused by name, never dropped | `fidelity::test_a_field_this_fake_never_answers_is_refused_501_naming_it` | Asana's behaviour for an unknown field is not documented |
| A task's name and notes, and a custom field's number, come back as they were sent | Minutehand's own: data as sent | `fidelity::test_a_task_comes_back_with_its_words_as_they_were_sent` | https://developers.asana.com/reference/gettask |
| A date-time (`due_at`, a date field's `date_time`) is answered `YYYY-MM-DDTHH:mm:ss.fffZ`, in UTC, the same moment as was sent | documented | `fidelity::test_a_date_time_is_answered_in_asanas_documented_form`, `test_asana_api.py::test_update_changes_only_what_was_sent` | https://developers.asana.com/docs/dates-and-times |
| `due_on` of a task given `due_at`, and `date` of a date field given `date_time`, is that moment's date in UTC | documented, with the world's people in UTC | `fidelity::test_a_date_time_is_answered_in_asanas_documented_form` | `TaskBase.due_on` ("the localized date") in the OpenAPI subset |
| A created object answers 201 with the URL it can be retrieved at in `Location` | documented | `fidelity::test_a_created_object_answers_where_it_can_be_retrieved` | https://developers.asana.com/docs/errors |
| An unknown object named in a path is 404 "<resource>: Unknown object: <gid>"; in a body field "<field>: Unknown object: <gid>", in a list "projects: [0]: Unknown object: <gid>" | observed | `test_asana_refusals.py::test_an_unknown_gid_is_refused_404`, `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it`, `claims::test_a_project_in_a_workspace_or_team_that_is_not_there_is_refused_unknown_object`, `claims::test_set_parent_to_a_task_that_does_not_exist_is_refused_400` | https://forum.asana.com/t/72142, https://forum.asana.com/t/91831, https://forum.asana.com/t/110890, https://stackoverflow.com/questions/37837171, https://stackoverflow.com/a/42913309 |
| A gid that is not digits is "<field>: Not a recognized ID: <value>"; a gid sent as another JSON type "<field>: Not a valid GID type: <type>" | observed | `test_asana_refusals.py::test_a_malformed_gid_is_refused_400`, `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it`, `claims::test_a_custom_field_setting_naming_a_field_by_its_name_is_refused_not_a_recognized_id` | https://forum.asana.com/t/19570, https://forum.asana.com/t/1011489, https://github.com/Asana/python-asana/issues/93, https://forum.asana.com/t/1108779 |
| An assignee naming nobody is "assignee: Not a user in Organization: <as sent>", in a plain workspace "assignee: Not a user in Workspace: <its gid>" | observed | `test_asana_refusals.py::test_an_unknown_assignee_is_refused`, `test_asana_sdk_client.py::test_a_refusal_reaches_the_sdk_as_its_own_error`, `claims::test_an_assignee_naming_nobody_in_a_plain_workspace_is_refused_in_asanas_words` | https://forum.asana.com/t/60069, https://forum.asana.com/t/852848 |
| An object the caller may not see is 403 "You do not have access to this project\|task\|team." | observed | `test_asana_parity_refusals.py::test_a_private_project_is_refused_403_and_hidden_from_listings`, `test_asana_parity_refusals.py::test_a_project_in_a_team_the_caller_is_not_in_is_refused_403` | https://forum.asana.com/t/67666, https://forum.asana.com/t/95502, https://forum.asana.com/t/289156 |
| A required field missing is "<field>: Missing input"; a task created naming no workspace, project or parent "You should specify one of workspace, parent, projects"; a project in an organization with no team "Missing required team field"; a membership with no project "memberships: [n]: project: Missing required field" | documented (the first), observed | `test_asana_refusals.py::test_a_task_needs_a_workspace_or_a_project_is_refused_without`, `test_asana_parity_refusals.py::test_an_organization_refuses_a_project_with_no_team`, `test_asana_refusals.py::test_a_comment_with_nothing_to_say_is_refused`, `claims::test_a_blank_project_name_is_refused_missing_input` | https://developers.asana.com/docs/errors, https://forum.asana.com/t/44096, https://forum.asana.com/t/31198, https://forum.asana.com/t/10481 |
| A body that is not JSON is "Could not parse request data, invalid JSON" | observed | `test_asana_refusals.py::test_a_body_that_is_not_json_is_refused` | https://stackoverflow.com/questions/38211523 |
| GET /tasks without one filter is "Must specify exactly one of project, tag, section, user task list, or assignee + workspace" | observed | `test_asana_refusals.py::test_listing_tasks_without_a_filter_is_refused` | https://forum.asana.com/t/793126 |
| A custom field not on the task's projects is "Custom field with ID <gid> is not on given object"; custom_fields given as a scalar "custom_fields: Value is not a JSON object: <value>" | observed | `test_asana_parity_refusals.py::test_an_unknown_custom_field_or_option_is_refused_400` | https://forum.asana.com/t/618448, https://forum.asana.com/t/170757 |
| `projects` or `tags` written on a task update is "<field>: Cannot write this property" | observed | `test_asana_refusals.py::test_moving_a_task_between_projects_or_workspaces_by_put_is_refused` | https://forum.asana.com/t/77626, https://stackoverflow.com/questions/42604985 |
| A due date that is not one is "due_on: Date must be in ISO-8601 (yyyy-mm-dd) format, not: <value>" | observed (the message, of a date parameter) | `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it` | https://forum.asana.com/t/69643 |
| An offset Asana did not issue is "offset: Your pagination token is invalid." | observed | `test_asana_refusals.py::test_an_offset_asana_did_not_issue_is_refused_in_asanas_words` | https://forum.asana.com/t/538741 |
| An unknown search parameter is ignored; one Asana has and this provider does not serve is refused by name | observed | `test_asana_refusals.py::test_search_rejects_what_it_does_not_know_and_its_limit` | https://stackoverflow.com/a/28948207 (an Asana engineer: unknown parameters are "silently ignored") |
| On a workspace that is not premium, search is 402 "Search is only available to premium users.", a custom field value "Custom Fields are not available for free users or guests.", a custom field setting "Custom Field Settings are not available for free users." | observed | `test_asana_parity_refusals.py::test_a_free_workspace_answers_402_to_search_and_custom_fields` | https://forum.asana.com/t/106546, https://forum.asana.com/t/189341, https://forum.asana.com/t/100330 |
| Every call in a throttled stretch is 429 with `Retry-After` and "You've made too many requests and hit a rate limit. Please retry after the given amount of time." | documented | `test_asana_parity_refusals.py::test_every_call_is_answered_429_with_retry_after_while_throttled` | https://developers.asana.com/docs/rate-limits |
| Completing a task changes no section and no custom field | documented | `claims::test_completing_a_task_leaves_its_section_and_its_status_field_where_they_were` | updateTask in the OpenAPI subset: "Only the fields provided in the data block will be updated; any unspecified fields will remain unchanged." |
| `completed` must be a JSON boolean | documented (Asana's words for one that is not are not, so it is refused by name) | `claims::test_an_update_whose_completed_is_not_a_json_boolean_is_refused` | https://developers.asana.com/reference/updatetask |
| `addCustomFieldSetting` needs a field's gid the workspace defines | documented | `test_asana_parity_refusals.py::test_a_field_already_on_a_project_or_unknown_is_refused_400`, `claims::test_a_custom_field_setting_naming_a_field_by_its_name_is_refused_not_a_recognized_id` | https://developers.asana.com/reference/addcustomfieldsettingforproject |
| Every case no page, recording or report gives Asana's answer to is refused by name: a parent that is the task or its subtask; a comment of nothing but spaces; a task put in a section of a project it is not in; a custom field setting already on (or not on) the project; teams of a workspace that is not an organization; a write body with no `data` object; a value of the wrong JSON type with no reported words (`name`, `completed`, `projects` as a string, ...); a limit, count or boolean flag out of range; an offset without a limit; an unparseable `due_at`; `workspace`, `memberships` or `parent` written on an update; both `projects` and `memberships` on a create; enum and people values naming nothing; the OAuth code grant | Minutehand's own: never an invented error or wording | `fidelity::test_a_value_asana_gives_no_words_for_is_refused_by_name`, `test_asana_parity_refusals.py::test_a_tag_or_parent_that_names_nothing_is_refused`, `test_asana_refusals.py::test_a_comment_with_nothing_to_say_is_refused`, `test_asana_parity_api.py::test_a_task_put_in_a_section_of_another_project_is_refused_by_name`, `test_asana_parity_refusals.py::test_a_field_already_on_a_project_or_unknown_is_refused_400`, `claims::test_my_teams_in_a_plain_workspace_are_refused_by_name`, `test_asana_refusals.py::test_a_write_without_its_data_wrapper_is_refused`, `test_asana_refusals.py::test_a_page_asana_gives_no_answer_for_is_refused_by_name`, `test_asana_parity_refusals.py::test_the_code_grant_is_refused_by_name` | |
| A full record leaves out what Asana's OpenAPI document marks [Opt In] (`num_subtasks`, `dependencies`, `dependents`; a team's `description`; a project membership's `parent` and `project`) until `opt_fields` names it | documented | `fidelity::test_a_full_task_leaves_out_what_is_opt_in_until_it_is_asked_for`, `fidelity::test_a_project_membership_answers_neither_an_invented_access_level_nor_opt_in_fields` | `TaskBase`, `TeamResponse`, `ProjectMembershipCompact` in the OpenAPI subset |
| A task's `dependencies` and `dependents`, asked for, are empty | the world's: nothing can link tasks, since `addDependencies` is refused by name | `fidelity::test_a_full_task_leaves_out_what_is_opt_in_until_it_is_asked_for` | `TaskBase` in the OpenAPI subset |
| A full task holds its custom fields full; a listed custom field is compact, without `resource_subtype` or `precision` | documented | `fidelity::test_a_full_task_holds_its_custom_fields_full_and_a_listed_one_compact` | `TaskResponse.custom_fields`, `CustomFieldCompact` in the OpenAPI subset |
| `html_notes` is refused by name, written or read | documented that the two are one text; how either is derived from the other is not | `test_asana_parity_api.py::test_html_notes_are_refused_by_name_written_or_read` | https://developers.asana.com/docs/rich-text |
| Sending both `due_on` and `due_at` is 400 "You may only provide one of due_on or due_at!" | observed | `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it` | https://forum.asana.com/t/808508 |
| A listing too large to answer without `limit` is 400 "The result is too large. You should use pagination (may require specifying a workspace)!"; past 1,000 items | observed | `claims::test_an_unpaginated_listing_too_large_to_answer_is_refused_and_a_paged_one_is_not` | https://forum.asana.com/t/21374, https://forum.asana.com/t/29153; "truncated at about 1,000 objects", https://developers.asana.com/docs/pagination |
| A paged listing carries `next_page` `{offset, path, uri}`, null on the last page | documented | `test_asana_api.py::test_an_unpaginated_list_has_a_null_next_page`, the conformance listing property | https://developers.asana.com/docs/pagination |
| A listed user is compact (no email); `opt_fields=email` brings the field | documented | `test_asana_api.py::test_users_carry_no_email_until_it_is_asked_for` | https://developers.asana.com/reference/getusersforworkspace |
| A project listing without `archived` includes archived projects; `archived` filters both ways | documented | `claims::test_a_project_listing_without_archived_includes_archived_projects_and_the_filter_narrows_it` | https://developers.asana.com/reference/getprojectsforworkspace |
| A project's custom field settings are compact (gid, resource_type) until `opt_fields` asks for the field | documented | `claims::test_custom_field_settings_are_compact_until_the_field_is_asked_for` | https://developers.asana.com/reference/getcustomfieldsettingsforproject |
| `projects` on a create is an array of gids | documented | `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it` | https://developers.asana.com/reference/createtask |
| A due date sent on create is stored | documented | `test_asana_api.py::test_update_changes_only_what_was_sent` | https://developers.asana.com/reference/createtask |
| A task in two projects has a membership in each | documented | `claims::test_a_task_created_in_two_projects_is_a_member_of_both` | https://developers.asana.com/reference/gettask |
| A subtask never added to a project has no membership | documented | `claims::test_a_subtask_never_added_to_a_project_has_no_membership` | `TaskBase.projects` ("directly associated") in the OpenAPI subset; https://forum.asana.com/t/26339 |
| `addTask` within the task's own project moves it, with no second membership | documented | `test_asana_parity_api.py::test_add_task_moves_it_to_the_section_and_status_is_read_from_the_field` | https://developers.asana.com/reference/addtaskforsection |
| A project create naming no workspace (and no team to find one) is a 400 | documented | `claims::test_a_project_create_naming_no_workspace_is_refused_missing_input` | https://developers.asana.com/reference/createproject |
| `addCustomFieldSetting` on a project that is not there is a 404 | documented | `claims::test_a_custom_field_setting_on_a_project_that_is_not_there_is_refused_404` | https://developers.asana.com/docs/errors |
| A created project answers its team, and reads back with it; a bare single read is the full record, team included | documented | `claims::test_a_created_project_reads_back_with_its_team_and_a_full_read_carries_it_unasked` | https://developers.asana.com/reference/getproject |
| A listed workspace is compact; `is_organization` is answered when asked, and on a bare single read | documented | `claims::test_a_full_workspace_read_says_whether_it_is_an_organization_unasked` | https://developers.asana.com/reference/getworkspace |
| `/users/me/teams` requires `organization`; missing or unknown is a 400; it lists the caller's teams | documented | `test_asana_parity_refusals.py::test_my_teams_need_an_organization`, `test_asana_parity_api.py::test_my_teams_are_the_teams_i_am_in_of_that_organization` | https://developers.asana.com/reference/getteamsforuser |
| In a workspace that is not an organization, a project needs no team and answers `team: null` | documented | `claims::test_a_project_in_a_plain_workspace_needs_no_team_and_has_none` | https://developers.asana.com/reference/createproject |
| A new project has one "Untitled section" and no custom fields | observed (the section) | `test_asana_parity_api.py::test_a_created_project_has_one_untitled_section_no_fields_and_its_creator_as_member` | https://forum.asana.com/t/1062521 |

## Not carried over

- **Credential enforcement** (see Credentials): every 401, expiry, strict tokens and the refresh's `invalid_grant`.
- **`html_notes` read as the text it shows, and synthesised from `notes`; a story's `html_text` synthesised from
  `text`.** Asana documents neither conversion (https://developers.asana.com/docs/rich-text); both are refused by
  name instead.
- **A task's `subtasks` field.** Not in Asana's `TaskResponse`; subtasks are read at `/tasks/{gid}/subtasks`, and
  `opt_fields=subtasks` is refused by name.
- **A project membership's `access_level: "editor"` and `write_access: "full_write"`, and a custom field's
  `has_notifications_enabled: false`.** Constants nothing in the world holds; refused by name when asked for.
- **405 "Method not allowed"** for a method a path does not take: Asana answers 404 (recorded).
- **Every refusal in the earlier stand-in's own words** ("Forbidden", "Not a Recognized ID", "Not a string",
  "Unrecognized parameter", "Must be between 1 and 100", "Invalid offset token", "Cannot specify both ...", the
  premium sentences, "Please, be chill.", ...): replaced by Asana's documented or reported words above, or a refusal
  by name.
- **`addTask` on another project's section adding that project, a 400 for a parent cycle, a blank comment, a field
  already on the project or "Not an organization"**: no source gives Asana's answer; refused by name.
- **`due_at` returned exactly as sent.** Asana documents its date-time form; it is followed.
- **A bare `GET /projects/{gid}` answered compact, without `team`**, and **a bare `GET /workspaces/{gid}` without
  `is_organization`**: contradicted by the reference (a single resource is its full record).
- **Admin routes that reset the world or rewrite the workspace's limits.** Asana has no such routes; here those are
  the scenario's seed (`AsanaSeed.workspace`).
- **Fixed gids and a fixture service account with no email.** Data of the predecessor's fixture files, not
  behaviour of Asana; gids here derive from the scenario's names.
