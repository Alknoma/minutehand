"""What earlier Jira stand-ins were found to need, each a fact about Jira Cloud, held by this fake.

Two calls carry them: creating a project (`POST /rest/api/3/project`) and asking what the caller may do
(`GET /rest/api/3/mypermissions`). Every case goes through the real proxy over TLS at the site's own host, signed in
as the agent with Basic. Provenance, class and source for each are in
`src/minutehand/adapters/providers/jira/CLAIMS.md`.
"""

from __future__ import annotations

from typing import Any

import pytest

from minutehand.domain.world import Operation
from tests.providers.jira.jira_site import AGENT, API, Site, basic, ok, refused

KANBAN = "com.pyxis.greenhopper.jira:gh-simplified-agility-kanban"
_LEAVE_OUT = object()


def _project(**changed: object) -> dict[str, Any]:
    """A body Jira accepts, with the named fields replaced (or, given `_LEAVE_OUT`, left out)."""
    body: dict[str, object] = {
        "key": "ORBIT",
        "name": "Orbit relaunch",
        "projectTypeKey": "software",
        "projectTemplateKey": KANBAN,
        "leadAccountId": AGENT,
    }
    body.update(changed)
    return {k: v for k, v in body.items() if v is not _LEAVE_OUT}


async def _create_refused(site: Site, body: dict[str, Any]) -> dict[str, str]:
    head = site.store.head()
    answer = refused(await site.http.post(f"{API}/project", json=body), 400)
    assert site.store.head() == head, "a refused create wrote to the world"
    errors: dict[str, str] = answer["errors"]
    return errors


# --------------------------------------------------------------------------- the create body


@pytest.mark.parametrize(
    ("missing", "named"), [("key", "projectKey"), ("name", "projectName"), ("leadAccountId", "leadAccountId")]
)
async def test_a_project_create_missing_a_required_field_is_refused_naming_it(
    site: Site, missing: str, named: str
) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — `key` and `name` are required, and either `lead` or `leadAccountId` must be set;
    an invalid request is a 400. The refusal is keyed by the field, in Jira's `errors` map."""
    errors = await _create_refused(site, _project(**{missing: _LEAVE_OUT}))

    assert set(errors) == {named}


async def test_a_project_create_with_neither_type_nor_template_is_refused_naming_the_type(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — without a project template the project type must be given."""
    errors = await _create_refused(site, _project(projectTypeKey=_LEAVE_OUT, projectTemplateKey=_LEAVE_OUT))

    assert set(errors) == {"projectTypeKey"}


async def test_a_project_create_with_a_template_and_no_type_takes_the_templates_type(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — the type is required only when no template is named, and each template belongs to
    one type. The earlier stand-in refused this body; the documentation accepts it."""
    made = ok(await site.http.post(f"{API}/project", json=_project(projectTypeKey=_LEAVE_OUT)), 201)

    assert made["key"] == "ORBIT"
    assert ok(await site.http.get(f"{API}/project/ORBIT"))["projectTypeKey"] == "software"


async def test_a_project_create_whose_template_belongs_to_another_type_is_refused(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — the template's type must match `projectTypeKey`."""
    errors = await _create_refused(site, _project(projectTypeKey="business"))

    assert "projectTemplateKey" in errors


async def test_an_empty_project_create_names_every_missing_field_at_once(site: Site) -> None:
    """Observed: Jira validates the whole body and reports every bad field in one 400, not the first one only."""
    errors = await _create_refused(site, {})

    assert set(errors) == {"projectKey", "projectName", "projectTypeKey", "leadAccountId"}


# --------------------------------------------------------------------------- the key


@pytest.mark.parametrize("key", ["orbit", "9ORBIT", "ORB_IT", "ORB-IT", "O"])
async def test_a_project_key_breaking_the_key_rule_is_refused(site: Site, key: str) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — a key starts with an uppercase letter followed by one or more uppercase letters or
    digits: lowercase, a leading digit, an underscore, a hyphen and a lone letter all break it."""
    errors = await _create_refused(site, _project(key=key))

    assert set(errors) == {"projectKey"}


async def test_a_project_key_of_eleven_characters_is_refused(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — a key is at most 10 characters."""
    errors = await _create_refused(site, _project(key="ORBITALPLAN"))

    assert set(errors) == {"projectKey"}
    assert "10" in errors["projectKey"]


async def test_a_project_key_of_exactly_ten_characters_is_accepted(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — 10 is the cap, inclusive."""
    made = ok(await site.http.post(f"{API}/project", json=_project(key="ORBITPLAN1")), 201)

    assert made["key"] == "ORBITPLAN1"


async def test_a_project_key_another_project_holds_is_refused_naming_that_project(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — keys are unique. Observed: the refusal names the project that already holds it."""
    errors = await _create_refused(site, _project(key="FIELD"))

    assert set(errors) == {"projectKey"}
    assert "Field Ops" in errors["projectKey"]


async def test_a_project_name_another_project_holds_is_refused(site: Site) -> None:
    """Observed: a second project with a name already in use is a 400 on `projectName`; the create page does not
    say so."""
    errors = await _create_refused(site, _project(name="field ops"))

    assert set(errors) == {"projectName"}


# --------------------------------------------------------------------------- the lead and the template


async def test_an_email_address_as_the_lead_is_refused(site: Site) -> None:
    """Observed: `leadAccountId` takes an account id; an email address names no account and is a 400 on that
    field."""
    errors = await _create_refused(site, _project(leadAccountId="tomas@example.com"))

    assert set(errors) == {"leadAccountId"}


async def test_the_callers_own_account_is_a_valid_lead(site: Site) -> None:
    """Observed: the account `/myself` answers with may lead the project it creates."""
    me = ok(await site.http.get(f"{API}/myself"))

    made = ok(await site.http.post(f"{API}/project", json=_project(leadAccountId=me["accountId"])), 201)

    assert ok(await site.http.get(f"{API}/project/{made['key']}"))["lead"]["accountId"] == me["accountId"]


async def test_a_template_key_jira_does_not_have_is_refused(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post lists the template keys; a key not among them creates nothing."""
    errors = await _create_refused(site, _project(projectTemplateKey="com.example:orbit-board"))

    assert set(errors) == {"projectTemplateKey"}


@pytest.mark.parametrize(
    "template",
    [
        "com.pyxis.greenhopper.jira:gh-simplified-agility-kanban",
        "com.pyxis.greenhopper.jira:gh-simplified-agility-scrum",
        "com.pyxis.greenhopper.jira:gh-simplified-basic",
        "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
        "com.pyxis.greenhopper.jira:gh-simplified-scrum-classic",
        "com.pyxis.greenhopper.jira:gh-cross-team-template",
        "com.pyxis.greenhopper.jira:gh-cross-team-planning-template",
    ],
)
async def test_every_software_template_jira_lists_is_accepted(site: Site, template: str) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post (`projectTemplateKey`'s enum) lists these seven templates for the `software` type; the refusal of an unknown
    template must not refuse a listed one."""
    ok(await site.http.post(f"{API}/project", json=_project(projectTemplateKey=template)), 201)


# --------------------------------------------------------------------------- the permission probe


async def test_mypermissions_answers_exactly_the_keys_asked_for(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get answers per requested key with `havePermission`. Observed: no key that was not
    asked for comes back (the parameterless form that listed every permission is gone)."""
    found = ok(await site.http.get(f"{API}/mypermissions", params={"permissions": "BULK_CHANGE,CREATE_PROJECT"}))

    assert set(found["permissions"]) == {"BULK_CHANGE", "CREATE_PROJECT"}
    assert found["permissions"]["CREATE_PROJECT"]["havePermission"] is True


async def test_mypermissions_reports_a_permission_the_caller_lacks_as_false(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get — a key the caller does not hold is answered, 200, with `havePermission`
    false; it is neither an error nor left out. Minutehand enforces no permission, so the only one an account
    lacks is a project permission where it sees no project: the app account belongs to none."""
    async with site.client(basic("automation@lanternworks.invalid", "any-token")) as app:
        found = ok(await app.get(f"{API}/mypermissions", params={"permissions": "BROWSE_PROJECTS,SYSTEM_ADMIN"}))

    assert found["permissions"]["BROWSE_PROJECTS"]["havePermission"] is False
    assert found["permissions"]["SYSTEM_ADMIN"]["havePermission"] is True


async def test_mypermissions_without_the_permissions_parameter_is_refused(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get — `permissions` is required, and an empty one is a 400."""
    answer = refused(await site.http.get(f"{API}/mypermissions"), 400)

    assert answer["errorMessages"] and answer["errors"] == {}


async def test_mypermissions_with_one_unknown_key_refuses_the_whole_call_naming_it(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get — an invalid key is a 400 for the call, so the valid keys beside it go
    unanswered. Observed: the message names the key."""
    answer = refused(
        await site.http.get(f"{API}/mypermissions", params={"permissions": "CREATE_PROJECT,LAUNCH_ROCKETS"}), 400
    )

    assert "LAUNCH_ROCKETS" in answer["errorMessages"][0]


# --------------------------------------------------------------------------- a malformed key anywhere else

MALFORMED_KEYS = ["orb_it", "9ORBIT", "ORBITALPLANNER"]


@pytest.mark.parametrize("key", MALFORMED_KEYS)
async def test_a_read_naming_a_malformed_project_key_is_refused_404(site: Site, key: str) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-projectidorkey-get
    and https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get
    — a project that is not found is a 404. A key no project could ever hold is simply one that is not found."""
    refused(await site.http.get(f"{API}/project/{key}"), 404)
    refused(await site.http.get(f"{API}/project/{key}/statuses"), 404)
    refused(
        await site.http.get(f"{API}/mypermissions", params={"permissions": "BROWSE_PROJECTS", "projectKey": key}), 404
    )


@pytest.mark.parametrize("key", MALFORMED_KEYS)
async def test_an_issue_create_naming_a_malformed_project_key_is_refused_400_on_project(site: Site, key: str) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-post
    — a create whose fields are not valid is a 400 in the `errors` shape."""
    body = {"fields": {"project": {"key": key}, "summary": "Plan the orbit", "issuetype": {"name": "Task"}}}

    assert set(refused(await site.http.post(f"{API}/issue", json=body), 400)["errors"]) == {"project"}


@pytest.mark.parametrize("permissions", ["BROWSE_PROJECTS,launch_rockets", "browse_projects", "BROWSE PROJECTS"])
async def test_mypermissions_with_a_malformed_permission_key_is_refused_400(site: Site, permissions: str) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get
    — a `permissions` list holding an invalid key is a 400; keys are matched exactly, so a lowercase spelling or a
    space for the underscore is invalid."""
    refused(await site.http.get(f"{API}/mypermissions", params={"permissions": permissions}), 400)


# --------------------------------------------------------------------------- what the retired emulator tests held


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_the_retired_search_is_refused_410_and_writes_nothing(site: Site, method: str) -> None:
    """Documented: https://developer.atlassian.com/changelog/#CHANGE-2046 — `/rest/api/3/search` is removed in
    favour of `/rest/api/3/search/jql`. Observed: callers of the removed endpoint are answered 410 Gone."""
    head = site.store.head()
    if method == "GET":
        answer = await site.http.get(f"{API}/search", params={"jql": "project = LAUNCH"})
    else:
        answer = await site.http.post(f"{API}/search", json={"jql": "project = LAUNCH"})

    body = refused(answer, 410)
    assert "search/jql" in " ".join(body["errorMessages"])
    assert site.store.head() == head


async def test_an_edit_refused_for_one_field_writes_none_of_them(site: Site) -> None:
    """Documented: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-put
    — an edit naming a value the field cannot take is a 400. Observed: the whole edit is refused, so a valid field
    beside the refused one is not written either, and the issue reads as before."""
    before = ok(await site.http.get(f"{API}/issue/LAUNCH-1"))
    head = site.store.head()

    refused(
        await site.http.put(
            f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Renamed", "priority": {"name": "No such priority"}}}
        ),
        400,
    )

    after = ok(await site.http.get(f"{API}/issue/LAUNCH-1"))
    assert after["fields"]["summary"] == before["fields"]["summary"] == "Write the release notes"
    changes = [e for e in site.store.events(since=head) if e.operation not in (Operation.READ, Operation.SEARCH)]
    assert changes == []
