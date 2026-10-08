# YouTrack provider: where its behaviour comes from

Each row is one fact about how YouTrack (and the Hub beside it) answers, the test that pins it here, and its
source. Tests are under `tests/providers/youtrack/`; `claims` is `test_youtrack_vendor_claims.py`,
`claims_projects` is `test_youtrack_vendor_claims_projects.py`, `surface` is `test_youtrack_surface.py`.

## Sources, and what "observed" meant

- **documented**: a JetBrains page, cited. The devportal REST reference is
  `https://www.jetbrains.com/help/youtrack/devportal/…`; its troubleshooting pages (`api-troubleshoot-*.html`)
  show real error bodies, request by request.
- **described**: YouTrack's own OpenAPI description, which every instance serves at `/api/openapi.json`. The subset
  for the resources this fake claims is `tests/data/vendor_surface/youtrack.json`: JetBrains' public instance
  (`https://youtrack.jetbrains.com/api/openapi.json`), version 2026.3, fetched 2026-10-08. Hub publishes no OpenAPI
  description there (that URL answers an HTML page), so Hub rows cite devportal pages.
- **recorded live**: what a live YouTrack Cloud instance answered on 2026-09-04 and 2026-09-07, written down in prose
  in the older stand-in's README by the people who ran a production YouTrack client against it. That client calls
  the REST API with its own plain HTTP code (YouTrack has no client library); no request or response was kept,
  only the prose and, for one call, an operation id. Each such row says what the prose records.
- **unverified**: the older stand-in's own assertion. Earlier versions of this file called all of these
  "observed", meaning "learned by watching a real instance answer"; checked row by row against the stand-in's
  README, most were its own design (several marked there "wording unverified" or "REST wording unverified"), not
  recordings of the service. Each is labelled here as what it is.

Of the 66 rows that were here, 46 were "observed". Now: 14 are documented or described (some with an unverified
part, said in the row), 10 rest on the live record (most also documented), 7 went with the credential and
permission checks below, 4 are replaced because the documentation contradicts them, 1 is refused by name as not
served, and 10 stay unverified.

## Credentials and permissions: deliberately not enforced

Minutehand checks no credential and enforces no permission. A token the seed names, or one Hub issued, acts as its
user; any other token, a Basic credential, an empty `Authorization` or none at all acts as the agent's account.
Removed, each pinned by a test that it no longer refuses:

| Removed check | Was | Pinned by |
|---|---|---|
| No bearer token | 401 `Unauthorized` | `test_youtrack_refusals::test_a_request_with_no_token_acts_as_the_agent` |
| A Basic credential | 401 | `test_youtrack_refusals::test_a_basic_credential_acts_as_the_agent` |
| An unknown token once tokens are seeded (`tokensRequired`) | 401 | `test_youtrack_people_and_refusals::test_an_unknown_token_acts_as_the_agent_even_once_tokens_are_seeded` |
| An issued token past its hour | 401 | `test_youtrack_people_and_refusals::test_an_issued_token_keeps_acting_as_its_user_after_its_hour` |
| A banned user's token | 401 | `tests/serve/test_tracker_live.py::test_a_youtrack_account_banned_reads_banned_and_its_token_still_acts_as_it` |
| Hub `oauth2/token` with a wrong secret or an unknown client | 401 `invalid_client` | `test_youtrack_people_and_refusals::test_a_wrong_client_secret_still_gets_a_token` |
| Read Issue or Read Project Basic withheld | 403; the project left out of searches and counts | `test_youtrack_people_and_refusals::test_a_withheld_read_issue_leaves_a_project_in_searches_and_readable` |
| Update Issue, Delete Issue or Create Issue withheld | 403 | `test_youtrack_people_and_refusals::test_a_withheld_update_issue_does_not_refuse_a_field_write` |
| Create Project withheld | 403 | `test_youtrack_people_and_refusals::test_a_withheld_create_project_does_not_refuse_a_create_and_the_cache_still_reports_it` |
| Update Project on a project made through the API, or withheld | 403 on attaching a field, adding a bundle value, changing a team | `test_youtrack_caller_calls::test_attaching_a_field_to_a_created_project_is_not_refused_for_update_project`, `test_youtrack_people_and_refusals::test_a_withheld_update_project_does_not_refuse_a_team_change` |
| Hub `/projects` listing only what the token may read | `total: 0` by permission | replaced: Hub holds no YouTrack project at all (row 48) |

The seed's `grants` stay world data: Hub's permissions cache reports them (rows 30, 66).

## Rows

| # | Claim | Source | Pinned by |
|---|---|---|---|
| 1 | ~~A call with no bearer token is a 401~~ | removed: credentials are not checked | above |
| 2 | With no `fields`, an entity is its `id` and `$type` alone | documented, [fields syntax](https://www.jetbrains.com/help/youtrack/devportal/api-fields-syntax.html) | `test_youtrack_issues::test_an_issue_without_fields_is_its_id_and_type_alone` |
| 3 | An attribute comes back only when `fields` names it, the field register too | documented, fields syntax | `test_youtrack_issues::test_fields_select_nested_fields_and_nothing_else`, `claims_projects::test_the_field_register_answers_only_the_attributes_asked_for` |
| 4 | `created`/`updated` are epoch milliseconds | documented, [Issue](https://www.jetbrains.com/help/youtrack/devportal/api-entity-Issue.html) | `test_youtrack_issues::test_timestamps_are_the_clock_in_milliseconds` |
| 5 | A create needs `summary` and `project` | documented, [issues](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues.html) | `test_youtrack_refusals::test_an_issue_without_a_summary_is_refused_400` |
| 6 | The project slot takes a database id; another shape is 400 `bad_request` "Invalid structure of entity id: …" | documented, issues and [ring id](https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-ring-id.html) (the body word for word); a well-shaped id naming nothing being a 404 is unverified | `claims::test_a_project_reference_not_shaped_as_an_entity_id_is_refused_400_before_any_lookup` |
| 7 | An issue created by project id lands in that project | documented, issues | `test_youtrack_caller_calls::test_create_issue_takes_the_project_database_id_and_fills_defaults` |
| 8 | A body property the Issue has not got (`title`, `assignee`, `labels`…) is refused, naming it | unverified (the Issue schema lists its properties; what an unlisted one answers is not documented) | `test_youtrack_refusals::test_a_property_the_issue_has_not_got_is_refused_400` |
| 9 | `state` at the top level of an update is refused | unverified (same) | `claims::test_a_state_at_the_top_level_of_an_update_is_refused_400` |
| 10 | A bundle value outside this project's bundle is "Value is not allowed"; one inside is written | unverified for bundles; the wording is recorded live for an assignee (row 57) | `test_youtrack_caller_calls::test_priority_and_type_take_a_bundle_value_by_name` |
| 11 | A field the project does not carry is absent from its issues and a write to it is refused | recorded live (2026-09-07: issues in a project made through the API were each refused a Due Date); the 404 status is unverified | `test_youtrack_caller_calls::test_a_field_the_project_does_not_carry_is_absent_and_refused_404`, `claims_projects::test_an_issue_in_a_created_project_carries_only_its_projects_fields` |
| 12 | Clearing State is refused and the issue keeps its state | documented that State cannot be empty ([Default template](https://www.jetbrains.com/help/youtrack/cloud/default-project-template.html), "Can be empty: false"); the refusal's wording unverified | `claims::test_clearing_the_state_is_refused_and_the_issue_still_reads_its_state` |
| 13 | A date field takes null | documented, [DateIssueCustomField](https://www.jetbrains.com/help/youtrack/devportal/api-entity-DateIssueCustomField.html) | `claims::test_a_due_date_takes_a_clear` |
| 14 | A date is epoch milliseconds; text is refused | documented, DateIssueCustomField | `test_youtrack_caller_calls::test_due_date_takes_epoch_milliseconds_and_story_points_a_number` |
| 15 | An issue's links collection takes no POST (405) | described: `/issues/{id}/links` is GET alone | `test_youtrack_refusals::test_a_method_youtrack_does_not_list_for_a_path_is_refused_405` |
| 16 | A link type names both directions | documented, [links](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues-issueID-links.html) | `test_youtrack_caller_calls::test_link_types_name_each_direction` |
| 17 | An undirected type has one slot, its bare id; a marked slot is a 404 | documented, links | `test_youtrack_caller_calls::test_an_undirected_link_uses_the_bare_type_id` |
| 18 | A readable key in a link body is refused by its shape | documented, ring id (the shape refusal) | `claims::test_a_readable_key_in_a_link_body_is_refused_with_the_entity_id_wording` |
| 19 | A link reads from both ends | documented, links | `test_youtrack_caller_calls::test_a_link_is_added_to_one_slot_by_database_id_and_read_from_both_ends` |
| 20 | A tag is added by id: a name there is a 400, an id naming nothing a 404 | documented that the body is `{"id": …}` ([issue tags](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues-issueID-tags.html)); both refusals unverified | `test_youtrack_caller_calls::test_an_issue_carries_an_existing_tag_by_id_and_drops_it` |
| 21 | A search naming a value the field has not got is a 400 `invalid_query`, not an empty answer | unverified | `claims::test_a_value_the_field_has_not_got_is_refused_invalid_query_not_answered_empty` |
| 22 | A value in a search matches the whole value name | documented, [search attributes](https://www.jetbrains.com/help/youtrack/cloud/search-and-command-attributes.html) | `claims::test_a_state_search_matches_the_whole_value_name` |
| 23 | `#Unresolved` follows each project's resolved flags | documented, search attributes | `claims::test_unresolved_reads_each_projects_own_resolved_values` |
| 24 | The count is a field that has to be asked for; -1 while still counting | documented, [count](https://www.jetbrains.com/help/youtrack/devportal/resource-api-issuesGetter-count.html) | `test_youtrack_caller_calls::test_count_issues_answers_the_number_asked_for` |
| 25 | A field search leaves out an issue holding another value | documented, search attributes | `claims::test_a_field_search_leaves_out_an_issue_holding_another_value` |
| 26 | `$top` and `$skip` page without repeating; 42 by default | documented, [pagination](https://www.jetbrains.com/help/youtrack/devportal/api-concept-pagination.html) | `test_youtrack_issues::test_top_and_skip_page_without_repeating`, `test_youtrack_caller_calls::test_list_projects_pages` |
| 27 | A project create needs `name`, `shortName` and `leader` | documented, [projects](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html) | `claims_projects::test_a_project_with_no_leader_is_refused_400_naming_the_leader` |
| 28 | The leader is a database id (a login is refused by shape); a short name in use is refused | documented, projects and ring id; the short-name refusal unverified | `claims_projects::test_a_login_as_leader_and_a_short_name_in_use_are_refused_400` |
| 29 | A created project lists, takes issues prefixed by its short name, and answers its leader | documented, projects | `claims_projects::test_a_created_project_lists_takes_an_issue_and_answers_its_leader` |
| 30 | Hub's permissions cache lists Create Project as global | recorded live (2026-09-04: `jetbrains.jetpass.project-create`, global) | `test_youtrack_caller_calls::test_hub_permissions_cache_lists_where_each_is_held` |
| 31 | The field register answers each field's type id (`enum[1]`, `state[1]`, `user[1]`, `date`…) | documented, [custom fields](https://www.jetbrains.com/help/youtrack/devportal/api-concept-custom-fields.html) | `test_youtrack_caller_calls::test_list_instance_custom_fields_with_their_types` |
| 32 | A project made with no template carries the Default template's fields | documented, Default template; recorded live 2026-09-07 that it carried no Due Date | `test_youtrack_caller_calls::test_create_project_from_the_default_template`, `claims_projects::test_a_template_gives_a_project_the_fields_its_page_lists` |
| 33 | `template` takes `scrum` or `kanban`, each giving the fields its page lists | documented, projects, [Scrum](https://www.jetbrains.com/help/youtrack/cloud/scrum-project-template.html), [Kanban](https://www.jetbrains.com/help/youtrack/cloud/kanban-project-template.html) | `claims_projects::test_a_template_gives_a_project_the_fields_its_page_lists`, `claims_projects::test_the_kanban_template_starts_an_issue_in_backlog` |
| 34 | ~~Every stock template carries the Default template's fields~~ | replaced by 33: the template pages list other fields | |
| 35 | Any other `template` is a 501 naming it, and creates nothing | documented that scrum and kanban are the listed values; YouTrack also has custom templates, which are not served | `claims_projects::test_a_custom_template_is_refused_by_name_and_creates_nothing` |
| 36 | ~~Attaching a field needs Update Project~~ | removed: permissions are not enforced | above |
| 37 | An attached field reaches the project's issues and takes writes | documented, [project fields](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects-projectID-customFields.html) | `test_youtrack_caller_calls::test_attaching_a_field_to_a_created_project_is_not_refused_for_update_project` |
| 38 | An attachment's `$type` must be the field's own | unverified (a wrong top-level type is documented as a 500, [field prototype](https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-custom-field-prototype.html), a different case) | same |
| 39 | A bundled field attached without its bundle is refused | unverified | `claims_projects::test_a_bundled_field_attached_without_its_bundle_is_refused_400` |
| 40 | The field to attach is named by id; a name is refused by shape | documented that the slot takes an id (project fields); the shape refusal by the ring id page | `claims_projects::test_a_field_named_by_its_name_is_refused_by_shape` |
| 41 | Attaching a field already present is refused | unverified | `test_youtrack_caller_calls::test_attaching_a_field_to_a_created_project_is_not_refused_for_update_project` |
| 42 | A project's fields answer with the Assignee bundle's users; an unknown project's are a 404 | documented that the Assignee values are the team (Default template); the 404 unverified | `test_youtrack_caller_calls::test_project_custom_fields_with_the_assignee_bundle`, `test_youtrack_refusals::test_an_unknown_project_is_refused_404` |
| 43 | A project made through the API is teamed by its leader and its maker | documented that the maker joins the team ([create a project](https://www.jetbrains.com/help/youtrack/cloud/create-new-project.html)); recorded live (2026-09-04) that a project made and led by one account was teamed by it alone | `claims_projects::test_a_created_projects_team_is_its_leader_and_its_maker` |
| 44 | A team is a ProjectTeam named after the project, counting its users, `ringId` null; members carry Hub ids | documented, [ProjectTeam](https://www.jetbrains.com/help/youtrack/devportal/api-entity-ProjectTeam.html); recorded live (2026-09-04, `fields=id,name,ringId,users(id,login,ringId)`) for the nulls | `claims_projects::test_a_team_is_a_group_of_its_own_and_youtrack_withholds_its_hub_id` |
| 45 | An unknown project's team is a 404 | unverified | `claims_projects::test_an_unknown_projects_team_is_refused_404` |
| 46 | `POST /admin/projects/{id}/team/users` is a 405 and changes nothing | described (GET alone); recorded live (2026-09-04 13:50Z, a 405) | `claims_projects::test_youtracks_team_users_route_refuses_a_post_405_and_changes_nothing`, `test_youtrack_caller_calls::test_adding_to_a_team_through_youtrack_is_refused_405` |
| 47 | ~~Hub's team id is not YouTrack's~~ | replaced: Hub holds no YouTrack team since 2026.1 ([deprecated Hub endpoints](https://www.jetbrains.com/help/youtrack/devportal/hub-rest-api-deprecated-endpoints-2026-1.html)) | |
| 48 | Hub's `/projects` holds no YouTrack project, whatever the query | documented, deprecated Hub endpoints; recorded live (`total: 0` for a project the account had made). The earlier reading, that `total: 0` meant "may not read", is replaced | `claims_projects::test_hub_answers_no_youtrack_project`, `test_youtrack_caller_calls::test_hub_holds_no_youtrack_project` |
| 49 | ~~Hub refuses a project query it cannot read~~ | replaced by 48 | |
| 50 | Hub lists All Users and Registered Users and no team group | documented, deprecated Hub endpoints; recorded live (the account saw the two groups and no team) | `claims_projects::test_the_group_listing_shows_the_instance_groups_and_no_team` |
| 51 | A user joins a team at `POST /admin/projects/{id}/team/ownUsers`, by database id | documented, [ownUsers](https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects-projectID-team-ownUsers.html) and deprecated Hub endpoints | `test_youtrack_caller_calls::test_a_user_joins_a_team_through_own_users_and_the_assignee_bundle_grows` |
| 52 | A second add of the same user leaves one membership | unverified | `claims_projects::test_adding_a_member_twice_leaves_one_membership` |
| 53 | A Hub id in the ownUsers body is refused by shape | documented, ring id | `claims_projects::test_a_team_add_naming_a_hub_id_is_refused_by_shape` |
| 54 | ~~A Hub add with no user id is a 400~~ | replaced: Hub holds no team to add to | |
| 55 | A Hub group id that is no instance group is a 404 | documented that Hub holds no YouTrack team (deprecated Hub endpoints) | `claims_projects::test_a_hub_group_that_is_no_instance_group_is_refused_404` |
| 56 | Adding to an instance-wide Hub group is a 501 naming it | not served | `claims_projects::test_adding_to_an_instance_wide_group_is_refused_by_name` |
| 57 | An assignee off the team is a 400 "Value is not allowed" | recorded live (the wording, 2026-09-04) | `test_youtrack_caller_calls::test_assignee_moves_by_database_id_and_off_the_team_is_refused` |
| 58 | Once the user joins the team, the assignment works | documented, Default template (Assignee takes the team) | `test_youtrack_caller_calls::test_a_user_joins_a_team_through_own_users_and_the_assignee_bundle_grows` |
| 59 | The same refusal reaches the create body's `customFields` | unverified | `claims::test_an_assignee_off_the_team_in_the_create_body_is_refused_value_not_allowed` |
| 60 | The Assignee bundle and the team name the same users | documented, Default template | `claims_projects::test_the_assignee_bundle_and_the_team_agree_after_an_add` |
| 61–65 | ~~Reading a team needs no Update Project; without Read Project Basic it is a 403; a Hub add without Update Project is a 403; Update Project is held per project; permissions are a user's~~ | removed: permissions are not enforced | above |
| 66 | The permissions cache lists a per-project permission as not global, by Hub id and key, leaving out a project it was withheld in and one made through the API | recorded live (2026-09-04: the keys, per project, never global; Update Project on no project for the account that made one) | `claims_projects::test_the_permissions_cache_leaves_out_a_withheld_project_and_one_made_through_the_api` |

## Added in this pass

| Claim | Source | Pinned by |
|---|---|---|
| Every operation of the claimed resources is served or a 501 naming it (184 described: 55 served, 129 refused); a path outside them is a 501 naming it | described | `surface::test_every_described_operation_is_served_or_refused_by_name_and_none_is_both`, `surface::test_the_counts_of_served_and_refused_operations`, `surface::test_an_unserved_operation_is_refused_501_naming_it`, `surface::test_a_path_outside_the_claimed_resources_is_refused_501_naming_it` |
| A method the description does not list for a path is a 405 | described; the status recorded live (row 46) | `test_youtrack_refusals::test_a_method_youtrack_does_not_list_for_a_path_is_refused_405` |
| A path outside `/api` and `/hub/api/rest` is 404 `{"error": "Not Found", "error_description": "HTTP 404 Not Found"}` | documented, [incorrect URL](https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-incorrect-issue-url.html) | `test_youtrack_refusals::test_a_path_outside_the_api_is_refused_404_in_youtracks_words` |
| A required field left empty on a create is `{"error": "Field required", "error_description": "<Field> is required", "error_field": "<Field>"}` | documented, [missing type](https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-missing-type.html) (its workflow-specific keys are not sent) | `test_youtrack_people_and_refusals::test_a_required_field_with_no_default_refuses_a_create_without_it_400` |
| A numeric `shortName` is `invalid_properties` "Project ID cannot be numeric" with its `error_children`; the letters-digits-underscore rule the stand-in took from the web UI is dropped | documented, [numeric project id](https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-numeric-project-id.html) | `claims_projects::test_a_numeric_short_name_is_refused_in_youtracks_words` |
| `GET /users/{login}` reads the user as `/users/{id}` does (it was a 400) | documented, [users in YouTrack and Hub](https://www.jetbrains.com/help/youtrack/devportal/api-users-yt-vs-hub.html) | `test_youtrack_caller_calls::test_get_user_for_its_hub_id` |
| `GET /users?query=` is a 501: the description's `GET /users` takes no `query` (it was filtered here) | described | `test_youtrack_caller_calls::test_find_users_pages_and_a_query_is_refused_by_name` |
| `GET /issues?customFields=<name>`, repeated, narrows the custom fields answered | documented, issues | `surface::test_get_issues_shows_only_the_custom_fields_named` |
| `wikifiedDescription`, `textPreview` and `ParsedCommand.description` are a 501 naming them: YouTrack renders or words them, and the fake answered raw text or its own words | documented, Issue, [IssueComment](https://www.jetbrains.com/help/youtrack/devportal/api-entity-IssueComment.html) | `surface::test_an_attribute_youtrack_renders_from_markup_is_refused_by_name`, `surface::test_a_command_description_is_refused_by_name` |
| An Issue's `fields` attribute is gone: neither the Issue page nor its schema has one | documented, Issue | |
| Names are stored as sent (project, tag, field and bundle value names were trimmed); summaries, descriptions and comments round-trip byte for byte, under any token | the owner's rule | `claims_projects::test_a_project_name_and_short_name_are_kept_as_sent`, `surface::test_an_issue_and_its_comment_round_trip_as_sent` |

## Divergences that stay

- **A template's fields of types not held.** The Default template also attaches Subsystem (`ownedField[1]`), Fix
  Versions and Affected versions (`version[*]`) and Fixed in build (`build[1]`); Scrum attaches Priority and
  Sprints as `enum[*]`. A project made here does not carry them (`fields.TEMPLATE_GAPS`). Scrum's "Story points"
  draws on the instance's "Story Points" (float) when it has one, as field names are matched case-insensitively.
- **Empty texts** of fields that cannot be empty ("No priority", "No state", "No stage") are this fake's: the
  template pages give none.
- **The 405 body** reads "HTTP 405 Method Not Allowed" by analogy with the documented 404 body; its wording is not
  documented.
- **`avatarUrl`** is a path this fake forms from the user's Hub id; YouTrack assigns its own.

## Not carried over

- **A seeded project carries every field the instance defines, and its team is every user.** Width chosen for the
  older stand-in's fixture data; here a project carries what its seed names.
- **The order of a created project's fields.** The older stand-in's own order; here it is the template page's.
- **Giving back a withheld permission through an admin route, refusing an unknown permission there, and leaving the
  fixture files untouched.** The older stand-in's admin control plane; permissions here are the seed's `grants`.
- **"A search survives an issue with no Priority."** It guarded a crash in the older stand-in.

## Read by the older stand-in, not adopted

- **`id` beside the attributes asked for.** YouTrack's samples show `id` in answers whose `fields` did not name it,
  and the older stand-in always added it. Not adopted: the fields syntax page states the rule that an attribute
  comes back only when named, and a stated rule outranks a sample.
- **Defaults on a new issue.** The older stand-in said a created issue holds no Priority or Type. Not adopted: the
  template pages give each field a default value, which a new issue takes.
