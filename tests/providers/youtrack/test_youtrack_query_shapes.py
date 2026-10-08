"""Every query shape a production client builds, and the rest of the language YouTrack users write, each through
the run's proxy against an instance whose answer is known: LAUNCH-1 (Tomas, Open, Critical, Task, due 26 August,
tagged docs, a comment about the partner call), LAUNCH-2 (Noor, Fixed, Normal, Feature, due 2 September, tagged
venue and big room), LAUNCH-3 (Iris, In Progress, Show-stopper, Task, no due date, made three days before the
start) and OPS-1 (nobody, Backlog, P0 - Outage, Bug, in a project without Due Date)."""

from __future__ import annotations

import httpx
import pytest

from tests.providers.youtrack.caller_world import TOMAS_TOKEN, as_user
from tests.providers.youtrack.youtrack_instance import entities, entity, refusal

L1, L2, L3, O1 = "LAUNCH-1", "LAUNCH-2", "LAUNCH-3", "OPS-1"


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("project: LAUNCH", [L3, L1, L2]),
        ("project: OPS", [O1]),
        ("assignee: tomas", [L1]),
        ("assignee: {Tomas Brandt}", [L1]),
        ("assignee: Unassigned", [O1]),
        ("for: iris", [L3]),
        ("Priority: Critical", [L1]),
        ("Priority: {P0 - Outage}", [O1]),
        ("State: Fixed", [L2]),
        ("State: {In Progress}", [L3]),
        ("State: Open, Backlog", [L1, O1]),
        ("tag: docs", [L1]),
        ("tag: {big room}", [L2]),
        ("Type: Feature", [L2]),
        ("#Unresolved", [L3, L1, O1]),
        ("#Resolved", [L2]),
        ("#{In Progress}", [L3]),
        ("#Bug", [O1]),
        ("project: LAUNCH Due Date: -", [L3]),
        ("Due Date: 2026-08-27 .. *", [L2]),
        ("Due Date: * .. 2026-08-26", [L1]),
        ("Due Date: 2026-08-26 .. 2026-09-02", [L1, L2]),
        ("venue", [L2]),
        ("pricing", [L1]),
        ("partner call", [L1]),
        ('"release notes"', [L1]),
        ("project: LAUNCH sort by: {Due Date} asc", [L1, L2, L3]),
        ("project: LAUNCH sort by: {Due Date} desc", [L2, L1, L3]),
        ("project: LAUNCH #Unresolved sort by: {Due Date} asc", [L1, L3]),
        ("sort by: Priority asc, created desc", [O1, L3, L1, L2]),
        ("updated: 2026-08-24", [L1, L2, O1]),
        ("created: * .. 2026-08-22", [L3]),
        ("updated: Yesterday", [L3]),
        ("issue id: LAUNCH-2", [L2]),
        ("State: -Fixed project: LAUNCH", [L3, L1]),
        ("project: LAUNCH -venue", [L3, L1]),
        ("Priority: Critical or tag: venue", [L1, L2]),
        ("has: {Story Points}", [L1]),
        ("Story Points: 2 .. 4", [L1]),
        ("reporter: iris", [L3, L1, L2, O1]),
        (
            "project: LAUNCH assignee: tomas State: Open #Unresolved Priority: Critical Type: Task tag: docs pricing",
            [L1],
        ),
    ],
)
async def test_each_query_shape_finds_exactly_what_it_names(
    yt: httpx.AsyncClient, query: str, found: list[str]
) -> None:
    answer = entities(await yt.get("/api/issues", params={"query": query, "fields": "idReadable", "$top": 100}))

    assert [i["idReadable"] for i in answer] == found


async def test_for_me_is_the_tokens_user(yt: httpx.AsyncClient) -> None:
    agents = entities(await yt.get("/api/issues", params={"query": "for: me", "fields": "idReadable"}))
    tomas = entities(
        await yt.get("/api/issues", params={"query": "for: me", "fields": "idReadable"}, headers=as_user(TOMAS_TOKEN))
    )
    shortcut = entities(
        await yt.get("/api/issues", params={"query": "#me", "fields": "idReadable"}, headers=as_user(TOMAS_TOKEN))
    )

    assert agents == []
    assert [i["idReadable"] for i in tomas] == [L1] == [i["idReadable"] for i in shortcut]


async def test_the_count_reads_the_same_language(yt: httpx.AsyncClient) -> None:
    counted = entity(
        await yt.post(
            "/api/issuesGetter/count", params={"fields": "count"}, json={"query": "project: LAUNCH #Unresolved"}
        )
    )

    assert counted["count"] == 2


@pytest.mark.parametrize(
    ("query", "child"),
    [
        ("State: Done", 'The value "Done" isn\'t used for the State field.'),
        ("project: LAUNCH Priority: {P0 - Outage}", 'The value "P0 - Outage" isn\'t used for the Priority field.'),
        ("assignee: nobody", 'The value "nobody" isn\'t used for the Assignee field.'),
        ("tag: nosuch", 'The value "nosuch" isn\'t used for the tag field.'),
        ("project: NOPE", 'The value "NOPE" isn\'t used for the project field.'),
        ("Due Date: soon", 'The value "soon" isn\'t used for the Due Date field.'),
        ("project: LAUNCH sort by: Wibble", "Sort field is expected."),
    ],
)
async def test_a_query_naming_what_nothing_has_is_refused_400_never_answered(
    yt: httpx.AsyncClient, query: str, child: str
) -> None:
    """As JetBrains' public instance answers (`data/observed/query_value_not_used.http`,
    `query_sort_field_unknown.http`)."""
    refused = refusal(await yt.get("/api/issues", params={"query": query, "fields": "idReadable"}), 400)
    counted = refusal(await yt.post("/api/issuesGetter/count", params={"fields": "count"}, json={"query": query}), 400)

    assert (
        refused
        == counted
        == {
            "error": "invalid_query",
            "error_description": "Can't parse search query, please check and update query syntax",
            "error_developer_message": "Can't parse search query",
            "error_field": "query",
            "error_children": [{"error": child, "error_description": ""}],
        }
    )


@pytest.mark.parametrize("query", ["(State: Open)", "Severity: High", "State: {In Progress"])
async def test_a_query_the_fake_cannot_read_is_refused_501_naming_it(yt: httpx.AsyncClient, query: str) -> None:
    """The public instance reads parentheses and an unknown attribute (`data/observed/query_parentheses.http`,
    `query_attribute_unknown.http`); this fake does not, and says so rather than inventing an error."""
    refused = refusal(await yt.get("/api/issues", params={"query": query, "fields": "idReadable"}), 501)

    assert repr(query) in str(refused["error_description"])
