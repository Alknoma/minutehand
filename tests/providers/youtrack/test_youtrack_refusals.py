"""What the real YouTrack refuses, refused here with its status and its words, and nothing written for it."""

from __future__ import annotations

import httpx
import pytest

from tests.providers.youtrack.youtrack_instance import (
    LAUNCH,
    Instance,
    assignee_field,
    client_for,
    create,
    entity,
    refusal,
    state_field,
)


async def test_a_request_with_no_token_acts_as_the_agent(instance: Instance) -> None:
    """Minutehand deliberately checks no credential: no Authorization at all reaches the route as the agent."""
    async with client_for(instance.provider, instance.store, instance.clock, token=None) as anonymous:
        me = entity(await anonymous.get("/api/users/me", params={"fields": "login"}))
        issue = entity(await anonymous.get("/api/issues/LAUNCH-1", params={"fields": "idReadable"}))

    assert me["login"] == "agent-bot"
    assert issue["idReadable"] == "LAUNCH-1"


async def test_a_basic_credential_acts_as_the_agent(instance: Instance) -> None:
    async with client_for(instance.provider, instance.store, instance.clock) as c:
        basic = {"Authorization": "Basic dXNlcjpwYXNz"}
        me = entity(await c.get("/api/users/me", params={"fields": "login"}, headers=basic))

    assert me["login"] == "agent-bot"


async def test_any_bearer_token_is_accepted(instance: Instance) -> None:
    async with client_for(instance.provider, instance.store, instance.clock, token="not-a-perm-token") as c:
        assert entity(await c.get("/api/users/me", params={"fields": "login"}))["login"] == "agent-bot"


@pytest.mark.parametrize(
    "path",
    [
        "/api/issues/LAUNCH-99",
        "/api/issues/2-9999",
        "/api/issues/LAUNCH-99/comments",
        "/api/issues/NOPE-1/customFields",
    ],
)
async def test_an_unknown_issue_is_refused_404(client: httpx.AsyncClient, path: str) -> None:
    answer = refusal(await client.get(path), 404)

    assert answer["error_description"] == f"Entity with id {path.split('/')[3]} not found"


async def test_an_update_delete_or_comment_on_an_unknown_issue_is_refused_404(
    instance: Instance, client: httpx.AsyncClient
) -> None:
    head = instance.store.head()
    refusal(await client.post("/api/issues/LAUNCH-99", json={"summary": "x"}), 404)
    refusal(await client.delete("/api/issues/LAUNCH-99"), 404)
    refusal(await client.post("/api/issues/LAUNCH-99/comments", json={"text": "x"}), 404)
    refusal(
        await client.post("/api/commands", json={"query": "State Fixed", "issues": [{"idReadable": "LAUNCH-99"}]}), 404
    )

    assert instance.store.head() == head


async def test_a_user_id_is_not_an_issue_and_is_refused_404(client: httpx.AsyncClient) -> None:
    refusal(await client.get("/api/issues/1-1"), 404)


async def test_an_unknown_project_is_refused_404(instance: Instance, client: httpx.AsyncClient) -> None:
    """Recorded from JetBrains' public instance (`data/observed/unknown_project_custom_fields.http`): an unknown
    project in the path is a 404 "Entity with id … not found"."""
    head = instance.store.head()
    for path in ("/api/admin/projects/0-77", "/api/admin/projects/NOPE/customFields"):
        answer = refusal(await client.get(path), 404)
        assert answer == {"error": "Not Found", "error_description": f"Entity with id {path.split('/')[4]} not found"}

    assert instance.store.head() == head


@pytest.mark.parametrize("reference", ["LAUNCH", "launch-project"])
async def test_a_short_name_in_the_project_id_slot_is_refused_400(client: httpx.AsyncClient, reference: str) -> None:
    answer = refusal(await client.post("/api/issues", json={"project": {"id": reference}, "summary": "x"}), 400)

    assert answer["error_description"] == f"Invalid structure of entity id: {reference}"


@pytest.mark.parametrize("summary", [None, "", "   "])
async def test_an_issue_without_a_summary_is_refused_400(
    instance: Instance, client: httpx.AsyncClient, summary: str | None
) -> None:
    head = instance.store.head()
    payload: dict[str, object] = {"project": {"id": LAUNCH}}
    if summary is not None:
        payload["summary"] = summary
    answer = refusal(await client.post("/api/issues", json=payload), 400)

    assert answer["error_description"] == "summary is required"
    assert instance.store.head() == head


async def test_an_update_clearing_the_summary_is_refused_400(client: httpx.AsyncClient) -> None:
    refusal(await client.post("/api/issues/LAUNCH-1", json={"summary": ""}), 400)


@pytest.mark.parametrize("field", ["title", "assignee", "state", "priority", "labels"])
async def test_a_property_the_issue_has_not_got_is_refused_501_naming_it(client: httpx.AsyncClient, field: str) -> None:
    """What YouTrack answers for a property its Issue has not got is neither documented nor recorded."""
    answer = refusal(
        await client.post("/api/issues", json={"project": {"id": LAUNCH}, "summary": "x", field: "y"}), 501
    )

    assert f"the body property '{field}'" in str(answer["error_description"])


@pytest.mark.parametrize("value", ["Done", "Closed", "Resolved"])
async def test_a_state_the_project_has_not_got_is_refused_501_naming_it(
    instance: Instance, client: httpx.AsyncClient, value: str
) -> None:
    head = instance.store.head()
    created = await client.post(
        "/api/issues", json={"project": {"id": LAUNCH}, "summary": "x", "customFields": [state_field(value)]}
    )
    updated = await client.post("/api/issues/LAUNCH-1", json={"customFields": [state_field(value)]})
    commanded = await client.post(
        "/api/commands", json={"query": f"State {value}", "issues": [{"idReadable": "LAUNCH-1"}]}
    )

    for answer in (created, updated, commanded):
        assert f"the value {value} for State" in str(refusal(answer, 501)["error_description"])
    assert instance.store.head() == head


async def test_clearing_the_state_is_refused_501_naming_it(client: httpx.AsyncClient) -> None:
    answer = refusal(
        await client.post(
            "/api/issues/LAUNCH-1",
            json={
                "customFields": [
                    {"name": "State", "$type": "StateIssueCustomField", "value": None},
                ]
            },
        ),
        501,
    )

    assert "clearing State, which cannot be empty" in str(answer["error_description"])


async def test_an_assignee_off_the_project_team_is_refused_400(instance: Instance, client: httpx.AsyncClient) -> None:
    outsider = instance.outsider()
    head = instance.store.head()
    by_login = await client.post("/api/issues/LAUNCH-1", json={"customFields": [assignee_field(outsider.login)]})
    by_id = await client.post(
        "/api/issues/LAUNCH-1",
        json={
            "customFields": [
                {"name": "Assignee", "value": {"id": outsider.id}},
            ]
        },
    )
    on_create = await client.post(
        "/api/issues",
        json={"project": {"id": LAUNCH}, "summary": "x", "customFields": [assignee_field(outsider.login)]},
    )
    commanded = await client.post(
        "/api/commands", json={"query": f"for {outsider.login}", "issues": [{"idReadable": "LAUNCH-1"}]}
    )

    for answer in (by_login, by_id, commanded):
        assert refusal(answer, 400)["error_description"] == "Value is not allowed"
    assert "off LAUNCH's team" in str(refusal(on_create, 501)["error_description"]), "the create body is unrecorded"
    assert instance.store.head() == head


async def test_an_assignee_who_does_not_exist_is_refused_400(client: httpx.AsyncClient) -> None:
    refusal(await client.post("/api/issues/LAUNCH-1", json={"customFields": [assignee_field("nobody")]}), 400)


async def test_a_field_the_project_has_not_got_is_refused_501_naming_it(client: httpx.AsyncClient) -> None:
    refusal(
        await client.post(
            "/api/issues/LAUNCH-1",
            json={
                "customFields": [
                    {"name": "Severity", "value": {"name": "Critical"}},
                ]
            },
        ),
        501,
    )


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("State: Done", "State"),
        ("for: nobody", "Assignee"),
        ("project: NOPE", "project"),
    ],
)
async def test_a_query_naming_a_value_nothing_has_is_refused_400_not_answered_empty(
    client: httpx.AsyncClient, query: str, field: str
) -> None:
    answer = refusal(await client.get("/api/issues", params={"query": query}), 400)

    assert answer["error"] == "invalid_query"
    assert answer["error_description"] == "Can't parse search query, please check and update query syntax"
    assert answer["error_field"] == "query"
    assert answer["error_children"] == [
        {"error": f'The value "{query.split(": ")[1]}" isn\'t used for the {field} field.', "error_description": ""}
    ], "as data/observed/query_value_not_used.http records it"


@pytest.mark.parametrize("query", ["", "Severity Critical", "close it", "State"])
async def test_a_command_that_is_not_one_is_refused_400(client: httpx.AsyncClient, query: str) -> None:
    refusal(await client.post("/api/commands", json={"query": query, "issues": [{"idReadable": "LAUNCH-1"}]}), 400)


async def test_a_command_with_no_issues_is_refused_400(client: httpx.AsyncClient) -> None:
    refusal(await client.post("/api/commands", json={"query": "State Fixed", "issues": []}), 400)


async def test_a_command_refused_on_one_issue_changes_none_of_them(
    instance: Instance, client: httpx.AsyncClient
) -> None:
    second = await create(client, "Second")
    head = instance.store.head()

    refusal(
        await client.post(
            "/api/commands",
            json={
                "query": "State Fixed",
                "issues": [
                    {"idReadable": second["idReadable"]},
                    {"idReadable": "LAUNCH-99"},
                ],
            },
        ),
        404,
    )

    assert instance.store.head() == head


async def test_an_empty_comment_is_refused_400(client: httpx.AsyncClient) -> None:
    refusal(await client.post("/api/issues/LAUNCH-1/comments", json={"text": ""}), 400)


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]"])
async def test_a_body_that_is_not_an_entity_is_refused_400(client: httpx.AsyncClient, raw: bytes) -> None:
    refusal(await client.post("/api/issues", content=raw, headers={"Content-Type": "application/json"}), 400)


@pytest.mark.parametrize("params", [{"$top": "ten"}, {"$skip": "-1"}, {"fields": "id,project(name"}])
async def test_a_malformed_paging_or_fields_parameter_is_refused_400(
    client: httpx.AsyncClient, params: dict[str, str]
) -> None:
    refusal(await client.get("/api/issues", params=params), 400)


async def test_a_method_youtrack_does_not_list_for_a_path_is_refused_405(client: httpx.AsyncClient) -> None:
    """YouTrack's API description lists GET, POST and DELETE on an issue, and GET alone on its links: any other
    method is a 405 (the status a live instance answered for a method it does not serve)."""
    refusal(await client.put("/api/issues/LAUNCH-1", json={}), 405)
    refusal(await client.post("/api/issues/LAUNCH-1/links", json={}), 405)


async def test_a_path_outside_the_api_is_refused_404_in_youtracks_words(client: httpx.AsyncClient) -> None:
    """https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-incorrect-issue-url.html"""
    answer = refusal(await client.get("/nothing/here"), 404)

    assert answer == {"error": "Not Found", "error_description": "HTTP 404 Not Found"}
