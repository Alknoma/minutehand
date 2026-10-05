# YouTrack provider: where its behaviour comes from

Each row is one fact about how YouTrack (and the Hub beside it) answers, the test that pins it here, and where the
fact comes from: **documented** (the page is cited, paraphrased in the test) or **observed** (learned by watching a
real instance answer, not stated in the documentation). Tests are under `tests/providers/youtrack/`; `claims` is
`test_youtrack_vendor_claims.py`, `claims_projects` is `test_youtrack_vendor_claims_projects.py`.

## Ported

| # | Claim | Class | Pinned by |
|---|---|---|---|
| 1 | A call with no bearer token is a 401 `Unauthorized` | observed | `test_youtrack_refusals::test_a_request_with_no_token_is_refused_401` |
| 2 | With no `fields`, an entity is its `id` and `$type` alone — [fields syntax](https://www.jetbrains.com/help/youtrack/devportal/api-fields-syntax.html) | documented | `test_youtrack_issues::test_an_issue_without_fields_is_its_id_and_type_alone` |
| 3 | An attribute comes back only when `fields` names it, the field register too — [fields syntax](https://www.jetbrains.com/help/youtrack/devportal/api-fields-syntax.html) | documented | `test_youtrack_issues::test_fields_select_nested_fields_and_nothing_else`, `claims_projects::test_the_field_register_answers_only_the_attributes_asked_for` |
| 4 | `created`/`updated` are epoch milliseconds — [Issue](https://www.jetbrains.com/help/youtrack/devportal/api-entity-Issue.html) | documented | `test_youtrack_issues::test_timestamps_are_the_clock_in_milliseconds` |
| 5 | A create needs `summary` and `project` — [issues](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues.html) | documented | `test_youtrack_refusals::test_an_issue_without_a_summary_is_refused_400` |
| 6 | The project slot takes a database id: any other shape is a 400 "Invalid structure of entity id" before any lookup, a well-shaped id naming nothing a 404 — [issues](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues.html) | documented (wording observed) | `claims::test_a_project_reference_not_shaped_as_an_entity_id_is_refused_400_before_any_lookup` |
| 7 | An issue created by project id lands in that project | documented | `test_youtrack_caller_calls::test_create_issue_takes_the_project_database_id_and_fills_defaults` |
| 8 | A body property YouTrack's issue has not got (`title`, `assignee`, `labels`…) is refused, naming it | observed | `test_youtrack_refusals::test_a_property_the_issue_has_not_got_is_refused_400` |
| 9 | `state` at the top level of an update is refused | observed | `claims::test_a_state_at_the_top_level_of_an_update_is_refused_400` |
| 10 | A bundle value outside this project's bundle is "Value is not allowed"; one inside is written | observed | `test_youtrack_caller_calls::test_priority_and_type_take_a_bundle_value_by_name` |
| 11 | A field the project does not carry is absent from its issues and a write to it is a 404 | observed | `test_youtrack_caller_calls::test_a_field_the_project_does_not_carry_is_absent_and_refused_404`, `claims_projects::test_an_issue_in_a_created_project_carries_only_its_projects_fields` |
| 12 | Clearing State is refused and the issue keeps its state | observed | `claims::test_clearing_the_state_is_refused_and_the_issue_still_reads_its_state` |
| 13 | A date field takes null — [DateIssueCustomField](https://www.jetbrains.com/help/youtrack/devportal/api-entity-DateIssueCustomField.html) | documented | `claims::test_a_due_date_takes_a_clear` |
| 14 | A date is epoch milliseconds; text is refused — [DateIssueCustomField](https://www.jetbrains.com/help/youtrack/devportal/api-entity-DateIssueCustomField.html) | documented | `test_youtrack_caller_calls::test_due_date_takes_epoch_milliseconds_and_story_points_a_number` |
| 15 | An issue's links collection is read-only (405 on POST) — [links](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues-issueID-links.html) | documented | `test_youtrack_caller_calls::test_a_link_is_added_to_one_slot_by_database_id_and_read_from_both_ends` |
| 16 | A link type names both directions — [links](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues-issueID-links.html) | documented | `test_youtrack_caller_calls::test_link_types_name_each_direction` |
| 17 | An undirected type has one slot, its bare id; a marked slot is a 404 — [links](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues-issueID-links.html) | documented | `test_youtrack_caller_calls::test_an_undirected_link_uses_the_bare_type_id`, `…_read_from_both_ends` |
| 18 | A readable key in a link body is refused by its shape | observed | `claims::test_a_readable_key_in_a_link_body_is_refused_with_the_entity_id_wording` |
| 19 | A link reads from both ends | observed | `test_youtrack_caller_calls::test_a_link_is_added_to_one_slot_by_database_id_and_read_from_both_ends` |
| 20 | A tag is added by id: a name there is a 400, an id naming nothing a 404 | observed | `test_youtrack_caller_calls::test_an_issue_carries_an_existing_tag_by_id_and_drops_it` |
| 21 | A search naming a value the field has not got is a 400 `invalid_query`, not an empty answer | observed | `test_youtrack_refusals::test_a_query_naming_a_value_nothing_has_is_refused_400_not_answered_empty`, `claims::test_a_value_the_field_has_not_got_is_refused_invalid_query_not_answered_empty` |
| 22 | A value in a search matches the whole value name — [search attributes](https://www.jetbrains.com/help/youtrack/cloud/search-and-command-attributes.html) | documented | `claims::test_a_state_search_matches_the_whole_value_name` |
| 23 | `#Unresolved` follows each project's resolved flags — [search attributes](https://www.jetbrains.com/help/youtrack/cloud/search-and-command-attributes.html) | documented | `claims::test_unresolved_reads_each_projects_own_resolved_values` |
| 24 | The count is a field that has to be asked for — [count](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issuesGetter-count.html) | documented | `test_youtrack_caller_calls::test_count_issues_answers_the_number_asked_for` |
| 25 | A field search leaves out an issue holding another value, a just-created one too | observed | `claims::test_a_field_search_leaves_out_an_issue_holding_another_value` |
| 26 | `$top` and `$skip` page without repeating — [pagination](https://www.jetbrains.com/help/youtrack/devportal/api-concept-pagination.html) | documented | `test_youtrack_issues::test_top_and_skip_page_without_repeating`, `test_youtrack_caller_calls::test_list_projects_pages` |
| 27 | A project create needs a leader — [projects](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html) | documented | `claims_projects::test_a_project_with_no_leader_is_refused_400_naming_the_leader` |
| 28 | The leader is a database id (a login is refused by shape); a short name in use is refused — [projects](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html) | documented (wording observed) | `claims_projects::test_a_login_as_leader_and_a_short_name_in_use_are_refused_400` |
| 29 | A created project lists, takes issues prefixed by its short name, and answers its leader — [projects](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html) | documented | `claims_projects::test_a_created_project_lists_takes_an_issue_and_answers_its_leader` |
| 30 | Hub's permissions cache lists Create Project as global | observed | `test_youtrack_caller_calls::test_hub_permissions_cache_lists_where_each_is_held` |
| 31 | The field register answers each field's type id | observed | `test_youtrack_caller_calls::test_list_instance_custom_fields_with_their_types` |
| 32 | A project made with no template carries the default template's fields, no Due Date | observed | `test_youtrack_caller_calls::test_create_project_from_the_default_template` |
| 33 | `template` takes `scrum` or `kanban` — [projects](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html) | documented | `claims_projects::test_a_stock_template_is_accepted_and_carries_no_due_date` |
| 34 | No stock template carries Due Date | observed | `claims_projects::test_a_stock_template_is_accepted_and_carries_no_due_date` |
| 35 | Any other `template` (a project's id or short name included) is a 400 and creates nothing — [projects](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html) | documented (refusal observed) | `claims_projects::test_a_template_that_is_not_a_stock_one_is_refused_400_and_creates_nothing` |
| 36 | Attaching a field needs Update Project, which the maker of a project made through the API does not hold | observed | `test_youtrack_caller_calls::test_attaching_a_field_to_a_created_project_needs_update_project` |
| 37 | An attached field reaches the project's issues and takes writes | observed | same |
| 38 | An attachment's `$type` must be the field's own | observed | same |
| 39 | A bundled field attached without its bundle is refused — [BundleProjectCustomField](https://www.jetbrains.com/help/youtrack/devportal/api-entity-BundleProjectCustomField.html) | observed | `claims_projects::test_a_bundled_field_attached_without_its_bundle_is_refused_400` |
| 40 | The field to attach is named by id; a name is refused by shape | observed | `claims_projects::test_a_field_named_by_its_name_is_refused_by_shape` |
| 41 | Attaching a field already present is refused | observed | `test_youtrack_caller_calls::test_attaching_a_field_to_a_created_project_needs_update_project` |
| 42 | A project's fields answer with the Assignee bundle's users; an unknown project's are a 404 | observed | `test_youtrack_caller_calls::test_project_custom_fields_with_the_assignee_bundle`, `test_youtrack_refusals::test_an_unknown_project_is_refused_404` |
| 43 | A project made through the API is teamed by its leader alone: the leader can be assigned, nobody else | observed | `claims_projects::test_a_created_projects_team_is_its_leader_alone` |
| 44 | A team is its own entity, named after the project, counting its users, `ringId` null; members carry Hub ids | observed | `claims_projects::test_a_team_is_a_group_of_its_own_and_youtrack_withholds_its_hub_id` |
| 45 | An unknown project's team is a 404 | observed | `claims_projects::test_an_unknown_projects_team_is_refused_404` |
| 46 | YouTrack's team route takes no write (405), whatever is held, and changes nothing | observed | `test_youtrack_caller_calls::test_adding_to_a_team_through_youtrack_is_refused_405`, `claims_projects::test_youtracks_team_route_refuses_405_whatever_is_held_and_changes_nothing` |
| 47 | Hub's team id is not YouTrack's | observed | `claims_projects::test_hubs_team_id_is_not_youtracks_team_id` |
| 48 | Hub answers `total: 0` for a project the token may not read | observed | `claims_projects::test_hub_answers_no_project_the_account_may_not_read` |
| 49 | Hub refuses a project query it cannot read | observed | `claims_projects::test_hub_refuses_a_project_query_it_cannot_read_400` |
| 50 | Hub lists All Users, Registered Users, then only the teams the caller may update | observed | `claims_projects::test_the_group_listing_shows_the_instance_groups_and_only_the_teams_the_caller_manages` |
| 51 | A user joins a team through its Hub group, by Hub id | observed | `test_youtrack_caller_calls::test_hub_adds_a_member_by_ring_id_and_the_assignee_bundle_grows` |
| 52 | A second add of the same user leaves one membership | observed | `claims_projects::test_adding_a_member_twice_leaves_one_membership` |
| 53 | A YouTrack user id names nobody in Hub (404) | observed | `test_youtrack_caller_calls::test_hub_adds_a_member_by_ring_id_and_the_assignee_bundle_grows` |
| 54 | A Hub add with no user id is a 400 | observed | `claims_projects::test_a_hub_add_with_no_user_id_is_refused_400` |
| 55 | A Hub add to a group naming nothing is a 404 | observed | `claims_projects::test_a_hub_group_that_is_no_teams_is_refused_404` |
| 56 | All Users is not a project's team: an add to it is a 403 | observed | `claims_projects::test_an_instance_wide_group_is_refused_403` |
| 57 | An assignee off the team is a 400 in YouTrack's four-field refusal | observed | `test_youtrack_caller_calls::test_assignee_moves_by_database_id_and_off_the_team_is_refused` |
| 58 | Once the user joins the team, the assignment works | observed | `test_youtrack_caller_calls::test_hub_adds_a_member_by_ring_id_and_the_assignee_bundle_grows` |
| 59 | The same refusal reaches the create body's `customFields` | observed | `claims::test_an_assignee_off_the_team_in_the_create_body_is_refused_value_not_allowed` |
| 60 | The Assignee bundle and the team name the same users | observed | `claims_projects::test_the_assignee_bundle_and_the_team_agree_after_a_hub_add` |
| 61 | Reading a team needs no Update Project | observed | `test_youtrack_caller_calls::test_create_project_from_the_default_template` |
| 62 | Reading a team without Read Project Basic is a 403 naming it | observed | `claims_projects::test_a_team_read_without_read_project_is_refused_403` |
| 63 | A Hub add without Update Project is a 403 | observed | `test_youtrack_people_and_refusals::test_hub_refuses_a_team_change_without_update_project_403` |
| 64 | Update Project is held per project | observed | `claims_projects::test_withholding_update_project_in_one_project_leaves_another_writable` |
| 65 | Permissions are a user's: one taken from another user leaves the caller's | observed | `claims_projects::test_a_permission_taken_from_another_user_leaves_the_caller_alone` |
| 66 | The permissions cache lists a per-project permission as not global, by Hub id and key, leaving out a project it was taken from and one made through the API | observed | `claims_projects::test_the_permissions_cache_leaves_out_a_withheld_project_and_one_made_through_the_api` |

## Not carried over

- **A seeded project carries every field the instance defines, and its team is every user.** Width chosen for the
  older stand-in's fixture data; here a project carries what its seed names.
- **The order of a created project's fields.** The older stand-in's own order; nothing documents one.
- **The wording of the 405 on the team route naming the Hub route.** That text was the older stand-in's own advice.
- **Giving back a withheld permission, refusing an unknown permission, and leaving the fixture files untouched.**
  These tested the older stand-in's admin control plane and its files; permissions here are the seed's `grants`.
- **"A search survives an issue with no Priority."** It guarded a crash in the older stand-in.

## Observed by the old emulator, not adopted

Where YouTrack's documentation is silent or contradicts itself, this provider keeps its own behaviour and the older
stand-in's reading is recorded here rather than carried.

- **`id` beside the attributes asked for.** YouTrack's samples
  ([fields syntax](https://www.jetbrains.com/help/youtrack/devportal/api-fields-syntax.html),
  [issues](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues.html)) show `id` in answers whose
  `fields` did not name it, and the older stand-in always added it. Not adopted: the same page states the rule that
  an attribute comes back only when named, and a stated rule outranks a sample.
- **Hub and a project made through the API.** The older stand-in gave such a project a Hub project and team group,
  hidden until the token could read it, and let a member be added there once Update Project was granted. Not
  adopted: no page documents either reading, and both rest on the same live `total: 0`, so this provider keeps no
  Hub project for it (`test_a_project_made_through_the_api_has_no_hub_project`).
- **Defaults on a new issue.** The older stand-in said a created issue holds no Priority or Type. Not adopted: the
  project-field pages say nothing either way, so this provider keeps the project's defaults
  (`test_create_issue_takes_the_project_database_id_and_fills_defaults`).
