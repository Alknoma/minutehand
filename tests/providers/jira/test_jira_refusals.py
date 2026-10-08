"""What Jira refuses, the fake refuses: with Jira's status and its `errorMessages`/`errors` body."""

from __future__ import annotations

from tests.providers.jira.jira_site import (
    API,
    CLOUD_ID,
    IRIS_TOKEN,
    TOMAS,
    Site,
    basic,
    ok,
    refused,
)

EX = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"


async def test_an_unknown_issue_is_404(site: Site) -> None:
    body = refused(await site.http.get(f"{API}/issue/LAUNCH-99"), 404)
    assert body["errorMessages"] == ["Issue does not exist or you do not have permission to see it."]


async def test_a_deleted_issue_is_404_and_its_key_is_never_reused(site: Site) -> None:
    assert (await site.http.delete(f"{API}/issue/FIELD-1")).status_code == 204
    refused(await site.http.get(f"{API}/issue/FIELD-1"), 404)
    refused(await site.http.post(f"{API}/issue/FIELD-1/comment", json={"body": {}}), 404)
    made = ok(
        await site.http.post(
            f"{API}/issue",
            json={"fields": {"project": {"key": "FIELD"}, "summary": "Again", "issuetype": {"name": "Task"}}},
        ),
        201,
    )
    assert made["key"] == "FIELD-2"


async def test_an_issue_in_a_project_without_browse_permission_is_404_not_403(site: Site) -> None:
    refused(await site.http.get(f"{API}/issue/VAULT-1"), 404)
    refused(await site.http.get(f"{API}/project/VAULT"), 404)
    async with site.client(basic("iris@example.com", IRIS_TOKEN)) as iris:
        assert ok(await iris.get(f"{API}/issue/VAULT-1"))["key"] == "VAULT-1"


async def test_a_transition_not_open_from_the_current_status_is_refused(site: Site) -> None:
    body = refused(await site.http.post(f"{API}/issue/LAUNCH-1/transitions", json={"transition": {"id": "31"}}), 400)
    assert body["errorMessages"] == ["Returned if the request is invalid for any other reason."]
    refused(await site.http.post(f"{API}/issue/LAUNCH-1/transitions", json={"transition": {"id": "99"}}), 400)


async def test_a_field_not_on_the_transition_screen_is_refused(site: Site) -> None:
    body = refused(
        await site.http.post(
            f"{API}/issue/LAUNCH-1/transitions",
            json={"transition": {"id": "11"}, "fields": {"resolution": {"name": "Done"}}},
        ),
        400,
    )
    assert body["errors"] == {
        "resolution": "Field 'resolution' cannot be set. It is not on the appropriate screen, or unknown."
    }


async def test_an_unknown_field_on_create_is_refused_with_every_bad_field_named(site: Site) -> None:
    body = refused(
        await site.http.post(
            f"{API}/issue",
            json={
                "fields": {
                    "project": {"key": "LAUNCH"},
                    "issuetype": {"name": "Task"},
                    "customfield_99999": "x",
                    "priority": {"name": "Urgent"},
                }
            },
        ),
        400,
    )
    assert body == {
        "errorMessages": [],
        "errors": {
            "customfield_99999": "Field 'customfield_99999' cannot be set. It is not on the appropriate screen, or unknown.",
            "priority": "Returned if the request contains invalid field values.",
            "summary": "Returned if the request is missing required fields.",
        },
    }


async def test_an_option_a_select_field_does_not_have_is_refused(site: Site) -> None:
    body = refused(
        await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"customfield_10050": {"value": "Legal"}}}), 400
    )
    assert body["errors"] == {"customfield_10050": "Returned if the request contains invalid field values."}
    assert (
        await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"customfield_10050": {"value": "Field"}}})
    ).status_code == 204


async def test_a_description_written_as_plain_text_is_refused(site: Site) -> None:
    body = refused(await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"description": "plain"}}), 400)
    assert set(body["errors"]) == {"description"}
    refused(await site.http.post(f"{API}/issue/LAUNCH-1/comment", json={"body": "plain"}), 400)


async def test_an_assignee_outside_the_project_is_refused(site: Site) -> None:
    former = next(u for u in site.jira.users() if u.displayName == "Former Colleague")
    body = refused(await site.http.put(f"{API}/issue/LAUNCH-1/assignee", json={"accountId": former.accountId}), 400)
    assert set(body["errors"]) == {"assignee"}


async def test_a_subtask_without_a_parent_and_an_issue_type_the_project_lacks_are_refused(site: Site) -> None:
    no_parent = refused(
        await site.http.post(
            f"{API}/issue",
            json={"fields": {"project": {"key": "LAUNCH"}, "summary": "Orphan", "issuetype": {"name": "Subtask"}}},
        ),
        400,
    )
    assert no_parent["errors"] == {"parent": "Returned if the request is missing required fields."}
    await site.http.post(
        f"{API}/project",
        json={"key": "BETA", "name": "Beta", "leadAccountId": site.jira.site().agent, "projectTypeKey": "software"},
    )
    no_story = refused(
        await site.http.post(
            f"{API}/issue",
            json={"fields": {"project": {"key": "BETA"}, "summary": "A story", "issuetype": {"name": "Story"}}},
        ),
        400,
    )
    assert set(no_story["errors"]) == {"issuetype"}


async def test_deleting_an_issue_with_subtasks_needs_delete_subtasks(site: Site) -> None:
    await site.http.post(
        f"{API}/issue",
        json={"fields": {"project": {"key": "LAUNCH"}, "summary": "Proofread", "issuetype": {"name": "Subtask"},
                         "parent": {"key": "LAUNCH-1"}}},
    )  # fmt: skip
    refused(await site.http.delete(f"{API}/issue/LAUNCH-1", params={"deleteSubtasks": "false"}), 400)
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["key"] == "LAUNCH-1"


async def test_a_declared_rate_limit_answers_429_with_retry_after_then_lets_the_call_through(site: Site) -> None:
    body = {"body": {"type": "doc", "version": 1, "content": []}}
    limited = await site.http.post(f"{API}/issue/LAUNCH-2/comment", json=body)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "7"
    assert limited.json() == {"errorMessages": ["Returned if the rate limit is exceeded."], "errors": {}}
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-2/comment"))["total"] == 0, "the refused call wrote nothing"
    full = {"body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "Booked."}]}]}}  # fmt: skip
    ok(await site.http.post(f"{API}/issue/LAUNCH-2/comment", json=full), 201)


async def test_the_retired_search_answers_410(site: Site) -> None:
    refused(await site.http.get(f"{API}/search", params={"jql": "project = LAUNCH"}), 410)


async def test_a_page_token_from_another_query_is_refused(site: Site) -> None:
    first = ok(await site.http.post(f"{API}/search/jql", json={"jql": "project in (LAUNCH, FIELD)", "maxResults": 1}))
    other = await site.http.post(
        f"{API}/search/jql", json={"jql": "project = LAUNCH", "nextPageToken": first["nextPageToken"]}
    )
    refused(other, 400)


async def test_a_project_create_with_bad_fields_names_all_of_them(site: Site) -> None:
    body = refused(await site.http.post(f"{API}/project", json={"key": "launch_1"}), 400)
    assert set(body["errors"]) == {"projectKey", "projectName", "projectTypeKey", "leadAccountId"}
    taken = refused(
        await site.http.post(
            f"{API}/project",
            json={"key": "LAUNCH", "name": "Another", "leadAccountId": TOMAS, "projectTypeKey": "software"},
        ),
        400,
    )
    assert taken["errors"] == {
        "projectKey": "Returned if the request is not valid and the project could not be created."
    }


async def test_a_site_this_world_does_not_hold_is_404(site: Site) -> None:
    """As recorded (`data/observed/site_unknown.http`): an HTML page whose heading is "Page unavailable"."""
    response = await site.http.get("https://elsewhere.atlassian.net/rest/api/3/myself")
    assert response.status_code == 404 and response.headers["content-type"].startswith("text/html")
    assert "<h1>Page unavailable</h1>" in response.text


async def test_mypermissions_needs_its_keys(site: Site) -> None:
    refused(await site.http.get(f"{API}/mypermissions"), 400)
    refused(await site.http.get(f"{API}/mypermissions", params={"permissions": "FLY"}), 400)
