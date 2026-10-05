# Asana: where each behaviour comes from

Each row is a fact about how Asana answers that this provider keeps, the test that pins it, and its source:
**documented** (Asana's public reference says so; the page is linked) or **observed** (a predecessor's tests
asserted it on a reading of real responses, and the reference does not state it). Paths are under
`tests/providers/asana/`; `claims` is `test_asana_vendor_claims.py`.

## Ported

| Claim | Class | Pinned by | Documentation |
|---|---|---|---|
| A request with no bearer token, read or write, is a 401 "Not Authorized" and writes nothing | documented | `test_asana_refusals.py::test_a_request_with_no_token_is_refused_not_authorized`, `claims::test_a_write_with_no_token_is_refused_not_authorized_and_writes_nothing` | https://developers.asana.com/docs/errors |
| Every write body sits inside `data`; one that does not is a 400 "Missing input: data" | observed | `test_asana_refusals.py::test_a_write_without_its_data_wrapper_is_refused`, `claims::test_a_write_not_wrapped_in_data_is_refused_missing_input_data` | |
| A listed user is compact (no email); `opt_fields=email` brings the field | documented | `test_asana_api.py::test_users_carry_no_email_until_it_is_asked_for` | https://developers.asana.com/reference/getusersforworkspace |
| A tag is named by its gid; a name in its place is "tag: Not a Recognized ID" | documented | `test_asana_parity_refusals.py::test_a_tag_or_parent_that_names_nothing_is_refused`, `test_asana_parity_api.py::test_tags_are_listed_created_added_and_removed_by_gid` | https://developers.asana.com/reference/addtagfortask |
| Completing a task moves it to no section and rewrites no custom field | observed | `claims::test_completing_a_task_leaves_its_section_and_its_status_field_where_they_were` | |
| `completed` must be a JSON boolean, on create and on update | documented | `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it`, `claims::test_an_update_whose_completed_is_not_a_json_boolean_is_refused` | https://developers.asana.com/reference/updatetask |
| Search is GET only, `limit` is 1 to 100, an unknown parameter or a non-id assignee is a 400 | documented | `test_asana_refusals.py::test_search_rejects_what_it_does_not_know_and_its_limit` | https://developers.asana.com/reference/searchtasksforworkspace |
| Search on a workspace that is not premium is a 402 | documented | `test_asana_parity_refusals.py::test_a_free_workspace_answers_402_to_search_and_custom_fields` | https://developers.asana.com/reference/searchtasksforworkspace |
| A listing too large to answer without `limit` is a 400, never a truncated list; paged, it works | observed | `claims::test_an_unpaginated_listing_too_large_to_answer_is_refused_and_a_paged_one_is_not` | |
| A project listing without `archived` includes archived projects; `archived` filters both ways | documented | `claims::test_a_project_listing_without_archived_includes_archived_projects_and_the_filter_narrows_it` | https://developers.asana.com/reference/getprojectsforworkspace |
| A project's custom field settings are compact (gid, resource_type) until `opt_fields` asks for the field | documented | `claims::test_custom_field_settings_are_compact_until_the_field_is_asked_for` | https://developers.asana.com/reference/getcustomfieldsettingsforproject |
| `projects` on a create is an array of gids | documented | `test_asana_refusals.py::test_a_create_is_refused_the_way_asana_refuses_it` | https://developers.asana.com/reference/createtask |
| `html_notes` not enclosed in `<body>` is a 400 | documented | `claims::test_html_notes_not_enclosed_in_body_is_refused` | https://developers.asana.com/docs/rich-text |
| An assignee that is not a user identifier, or names nobody, is a 400 | observed | `test_asana_refusals.py::test_an_unknown_assignee_is_refused` | |
| A due date sent on create is stored | documented | `test_asana_api.py::test_update_changes_only_what_was_sent` | https://developers.asana.com/reference/createtask |
| `setParent` refuses the task itself or its own subtask as the parent | observed | `test_asana_parity_refusals.py::test_a_tag_or_parent_that_names_nothing_is_refused` | |
| `setParent` naming a task that does not exist is a 400 | observed | `claims::test_set_parent_to_a_task_that_does_not_exist_is_refused_400` | |
| A comment with no text, or only spaces, is a 400 "Missing input: text" | observed | `test_asana_refusals.py::test_a_comment_with_nothing_to_say_is_refused` | |
| A task in two projects has a membership in each | documented | `claims::test_a_task_created_in_two_projects_is_a_member_of_both` | https://developers.asana.com/reference/gettask |
| A subtask never added to a project has no membership | observed | `claims::test_a_subtask_never_added_to_a_project_has_no_membership` | |
| `addTask` on another project's section adds that project and keeps the first | observed | `test_asana_parity_api.py::test_a_task_in_a_section_of_another_project_joins_that_project` | |
| `addTask` within the task's own project moves it, with no second membership | documented | `test_asana_parity_api.py::test_add_task_moves_it_to_the_section_and_status_is_read_from_the_field` | https://developers.asana.com/reference/addtaskforsection |
| A project create naming no workspace (and no team to find one) is a 400 | documented | `claims::test_a_project_create_naming_no_workspace_is_refused_missing_input` | https://developers.asana.com/reference/createproject |
| A project name missing or blank is "name: Missing input" | observed | `test_asana_parity_refusals.py::test_an_organization_refuses_a_project_with_no_team`, `claims::test_a_blank_project_name_is_refused_missing_input` | |
| A project in a workspace that is not there is a 400 naming the gid | observed | `claims::test_a_project_in_a_workspace_or_team_that_is_not_there_is_refused_unknown_object` | |
| A new project has one "Untitled section" and no custom fields | observed | `test_asana_parity_api.py::test_a_created_project_has_one_untitled_section_no_fields_and_its_creator_as_member` | |
| `addCustomFieldSetting` needs a field's gid that the workspace defines; a name, an unknown gid or none is a 400 | documented | `test_asana_parity_refusals.py::test_a_field_already_on_a_project_or_unknown_is_refused_400`, `claims::test_a_custom_field_setting_naming_a_field_by_its_name_is_refused_not_a_recognized_id` | https://developers.asana.com/reference/addcustomfieldsettingforproject |
| A field already on the project is refused 400 | observed | `test_asana_parity_refusals.py::test_a_field_already_on_a_project_or_unknown_is_refused_400` | |
| `addCustomFieldSetting` on a project that is not there is a 404 | documented | `claims::test_a_custom_field_setting_on_a_project_that_is_not_there_is_refused_404` | https://developers.asana.com/docs/errors |
| In an organization, a project must name a team | documented | `test_asana_parity_refusals.py::test_an_organization_refuses_a_project_with_no_team` | https://developers.asana.com/reference/createproject |
| A project in a team that is not there is a 400 naming the gid | observed | `claims::test_a_project_in_a_workspace_or_team_that_is_not_there_is_refused_unknown_object` | |
| A created project answers its team, and reads back with it; a bare single read is the full record, team included | documented | `test_asana_parity_api.py::test_a_created_project_has_one_untitled_section_no_fields_and_its_creator_as_member`, `claims::test_a_created_project_reads_back_with_its_team_and_a_full_read_carries_it_unasked` | https://developers.asana.com/reference/getproject |
| A listed workspace is compact; `is_organization` is answered when asked, and on a bare single read | documented | `test_asana_parity_api.py::test_a_workspace_says_it_is_an_organization_only_when_asked`, `claims::test_a_full_workspace_read_says_whether_it_is_an_organization_unasked` | https://developers.asana.com/reference/getworkspace |
| `/users/me/teams` requires `organization`; missing or unknown is a 400; it lists the caller's teams | documented | `test_asana_parity_refusals.py::test_my_teams_need_an_organization`, `test_asana_parity_api.py::test_my_teams_are_the_teams_i_am_in_of_that_organization` | https://developers.asana.com/reference/getteamsforuser |
| In a workspace that is not an organization, asking for the caller's teams is a 400 "Not an organization" | observed | `claims::test_a_plain_workspace_refuses_to_list_my_teams_not_an_organization` | |
| In a workspace that is not an organization, a project needs no team and answers `team: null` | documented | `claims::test_a_project_in_a_plain_workspace_needs_no_team_and_has_none` | https://developers.asana.com/reference/createproject |

## Not carried over

- **A bare `GET /projects/{gid}` answered compact, without `team`.** Contradicted by the reference: a single
  project is the full record (https://developers.asana.com/reference/getproject). This provider answers the
  team; a client that relied on its absence will now see it.
- **A bare `GET /workspaces/{gid}` without `is_organization`.** Contradicted by the reference: a single
  workspace is the full record and its schema carries `is_organization`
  (https://developers.asana.com/reference/getworkspace).
- **Admin routes that reset the world or rewrite the workspace's limits** (premium, organization, the size
  past which an unpaginated read is refused). Asana has no such routes; here those are the scenario's seed
  (`AsanaSeed.workspace`), and the size is fixed.
- **An unpaginated-read threshold of two items.** An override of the predecessor's own; Asana publishes no
  threshold, and this provider refuses past a thousand.
- **Fixed gids and a fixture service account with no email.** Data of the predecessor's fixture files, not
  behaviour of Asana; gids here derive from the scenario's names.
- **The exact sentence of the unpaginated refusal.** Only its opening words are pinned; Asana does not
  document the rest.
