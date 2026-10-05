# Jira Cloud: where the fake's behaviour comes from

Each row is a fact about Jira Cloud that an earlier stand-in for it was built or tested to hold, and the test in
`tests/providers/jira/test_jira_vendor_claims.py` that holds this fake to it. **Documented** facts cite the page they
are read from; **observed** facts are what callers of the real service reported and no public page states.

Pages cited:

- Create project: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post
- Get my permissions: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get

## Creating a project (`POST /rest/api/3/project`)

| Claim | Class | Test | Source |
|---|---|---|---|
| A body missing `key`, `name` or the lead is a 400 keyed by `projectKey`, `projectName` or `leadAccountId` | documented | `test_a_project_create_missing_a_required_field_is_refused_naming_it` | Create project |
| With neither a type nor a template, the 400 names `projectTypeKey` | documented | `test_a_project_create_with_neither_type_nor_template_is_refused_naming_the_type` | Create project |
| A template with no type is accepted, and the project takes the template's type | documented | `test_a_project_create_with_a_template_and_no_type_takes_the_templates_type` | Create project |
| A template of another type than `projectTypeKey` is a 400 on `projectTemplateKey` | documented | `test_a_project_create_whose_template_belongs_to_another_type_is_refused` | Create project |
| An empty body reports all four fields in one 400 | observed | `test_an_empty_project_create_names_every_missing_field_at_once` | |
| A key that is lowercase, starts with a digit, holds `_` or `-`, or is one letter is a 400 on `projectKey` | documented | `test_a_project_key_breaking_the_key_rule_is_refused` | Create project |
| An eleven-character key is refused; a ten-character one is accepted | documented | `test_a_project_key_of_eleven_characters_is_refused`, `test_a_project_key_of_exactly_ten_characters_is_accepted` | Create project |
| A key already held is a 400 on `projectKey` that names the holding project | documented (uniqueness) / observed (the name) | `test_a_project_key_another_project_holds_is_refused_naming_that_project` | Create project |
| A name already held (any case) is a 400 on `projectName` | observed | `test_a_project_name_another_project_holds_is_refused` | |
| An email address in `leadAccountId` is a 400 on that field | observed | `test_an_email_address_as_the_lead_is_refused` | |
| The caller's own account (`/myself`) is a valid lead | observed | `test_the_callers_own_account_is_a_valid_lead` | |
| A template key Jira does not list is a 400 on `projectTemplateKey` | documented | `test_a_template_key_jira_does_not_have_is_refused` | Create project |
| Every software template Jira lists, `gh-simplified-basic` included, is accepted | documented | `test_every_software_template_jira_lists_is_accepted` | Create project |

## Asking what the caller may do (`GET /rest/api/3/mypermissions`)

| Claim | Class | Test | Source |
|---|---|---|---|
| Exactly the keys asked for come back, each with `havePermission` | documented (per key) / observed (nothing else) | `test_mypermissions_answers_exactly_the_keys_asked_for` | Get my permissions |
| A permission the caller lacks is answered 200 with `havePermission: false` | documented | `test_mypermissions_reports_a_permission_the_caller_lacks_as_false` | Get my permissions |
| No `permissions` parameter is a 400 | documented | `test_mypermissions_without_the_permissions_parameter_is_refused` | Get my permissions |
| One unknown key refuses the whole call, and the message names it | documented (400) / observed (the name) | `test_mypermissions_with_one_unknown_key_refuses_the_whole_call_naming_it` | Get my permissions |
| No credentials at all is a 401 | observed | `test_mypermissions_without_credentials_is_refused_401` | The page lists 401 for missing credentials and also says the call can be made anonymously |
| A lowercase or space-separated permission key is invalid, a 400 | documented | `test_mypermissions_with_a_malformed_permission_key_is_refused_400` | Get my permissions |

## A key no project could hold, named elsewhere

| Claim | Class | Test | Source |
|---|---|---|---|
| A project read, its statuses, or `mypermissions?projectKey=` naming a malformed key is a 404 | documented | `test_a_read_naming_a_malformed_project_key_is_refused_404` | https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-projectidorkey-get |
| An issue create naming a malformed project key is a 400 on `project` | documented | `test_an_issue_create_naming_a_malformed_project_key_is_refused_400_on_project` | https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-post |

## Where the earlier stand-in was wrong

- It refused a create that named a template but no `projectTypeKey`. The create-project reference makes the type
  necessary only when no template is given, so this fake accepts the body and gives the project the template's
  type. A caller that was taught to always send the type is unaffected; one that leaves it out now succeeds here
  as it does on Jira Cloud.

## Not carried over

- Refusal sentences word for word. The stand-in reconstructed Atlassian's English; this fake answers in its own
  words, in Jira's `errorMessages`/`errors` shape, keyed by the same fields.
- A created project surviving a reload of the stand-in's in-memory cache, and an admin reset restoring four fixed
  projects. Both describe the stand-in's own storage and admin routes; here every write is in the run's store.
- One fixed principal that holds every global permission whatever the token. Here an account's permissions come
  from the scenario's seed.
