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


async def test_a_request_with_no_token_is_refused_401(instance: Instance) -> None:
    async with client_for(instance.provider, instance.store, instance.clock, token=None) as anonymous:
        answer = refusal(await anonymous.get("/api/issues/LAUNCH-1"), 401)

    assert answer["error"] == "Unauthorized"


async def test_a_basic_credential_is_refused_401(instance: Instance) -> None:
    async with client_for(instance.provider, instance.store, instance.clock) as c:
        refusal(await c.get("/api/users/me", headers={"Authorization": "Basic dXNlcjpwYXNz"}), 401)


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
    head = instance.store.head()
    refusal(await client.post("/api/issues", json={"project": {"id": "0-77"}, "summary": "x"}), 404)
    refusal(await client.get("/api/admin/projects/0-77"), 404)
    refusal(await client.get("/api/admin/projects/NOPE/customFields"), 404)

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
async def test_a_property_the_issue_has_not_got_is_refused_400(client: httpx.AsyncClient, field: str) -> None:
    answer = refusal(
        await client.post("/api/issues", json={"project": {"id": LAUNCH}, "summary": "x", field: "y"}), 400
    )

    assert answer["error_description"] == f"Unsupported property: {field}"


@pytest.mark.parametrize("value", ["Done", "Closed", "Resolved"])
async def test_a_state_the_project_has_not_got_is_refused_400(
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
        assert refusal(answer, 400)["error_description"] == "Value is not allowed"
    assert instance.store.head() == head


async def test_clearing_the_state_is_refused_400(client: httpx.AsyncClient) -> None:
    answer = refusal(
        await client.post(
            "/api/issues/LAUNCH-1",
            json={
                "customFields": [
                    {"name": "State", "$type": "StateIssueCustomField", "value": None},
                ]
            },
        ),
        400,
    )

    assert answer["error_description"] == "Value is not allowed"


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

    for answer in (by_login, by_id, on_create, commanded):
        assert refusal(answer, 400)["error_description"] == "Value is not allowed"
    assert instance.store.head() == head


async def test_an_assignee_who_does_not_exist_is_refused_400(client: httpx.AsyncClient) -> None:
    refusal(await client.post("/api/issues/LAUNCH-1", json={"customFields": [assignee_field("nobody")]}), 400)


async def test_a_field_the_project_has_not_got_is_refused_404(client: httpx.AsyncClient) -> None:
    refusal(
        await client.post(
            "/api/issues/LAUNCH-1",
            json={
                "customFields": [
                    {"name": "Severity", "value": {"name": "Critical"}},
                ]
            },
        ),
        404,
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
    assert f"isn't used for the {field} field" in str(answer["error_description"])


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


async def test_a_route_youtrack_does_not_serve_is_refused_404_in_its_shape(client: httpx.AsyncClient) -> None:
    refusal(await client.get("/api/agiles"), 404)
    refusal(await client.put("/api/issues/LAUNCH-1", json={}), 405)
