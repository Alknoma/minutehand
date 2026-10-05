"""Facts about how YouTrack answers issue, field, search and link calls, each carried over from an older stand-in
that had learned it, driven through the run's proxy with `httpx`. Each docstring says whether the fact is in
YouTrack's documentation (with the page) or was observed and is undocumented. `CLAIMS.md` beside the provider lists
them all."""

from __future__ import annotations

import httpx
import pytest

from tests.providers.youtrack.youtrack_instance import Instance, entities, entity, named, refusal

LAUNCH = "0-1000"
VENDOR = "1-10000"
ISSUE_FIELDS = "idReadable,customFields(name,value(name))"


async def field_id(yt: httpx.AsyncClient, issue: str, name: str) -> str:
    read = entity(await yt.get(f"/api/issues/{issue}", params={"fields": "customFields(id,name)"}))
    found = named(read["customFields"], name)["id"]
    assert isinstance(found, str)
    return found


def value_name(issue: dict[str, object], field: str) -> object:
    value = named(issue["customFields"], field)["value"]
    return value["name"] if isinstance(value, dict) else value


async def search(yt: httpx.AsyncClient, query: str) -> list[dict[str, object]]:
    return entities(await yt.get("/api/issues", params={"query": query, "fields": ISSUE_FIELDS}))


# --------------------------------------------------------------------------- the create body


@pytest.mark.parametrize("reference", ["LAUNCH", "launch-board", "LAUNCH-1"])
async def test_a_project_reference_not_shaped_as_an_entity_id_is_refused_400_before_any_lookup(
    yt: httpx.AsyncClient, team: Instance, reference: str
) -> None:
    """Documented: an issue is created in a project named by its database id
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues.html). Observed, undocumented: anything
    not shaped `<n>-<n>` is refused 400 "Invalid structure of entity id" whether or not a project goes by that name,
    and a well-shaped id naming nothing is a 404."""
    head = team.store.head()

    refused = refusal(
        await yt.post("/api/issues", json={"project": {"id": reference}, "summary": "Order lanyards"}), 400
    )
    missing = refusal(await yt.post("/api/issues", json={"project": {"id": "0-77"}, "summary": "Order lanyards"}), 404)

    assert refused == {"error": "bad_request", "error_description": f"Invalid structure of entity id: {reference}"}
    assert missing["error_description"] == "Entity with id 0-77 not found"
    assert team.store.head() == head


async def test_a_state_at_the_top_level_of_an_update_is_refused_400(yt: httpx.AsyncClient, team: Instance) -> None:
    """Observed, undocumented: an issue's state is a custom field, so an update naming `state` beside `summary` is
    refused rather than ignored."""
    head = team.store.head()

    refusal(await yt.post("/api/issues/LAUNCH-1", json={"state": "Fixed"}), 400)

    assert team.store.head() == head
    assert value_name(entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": ISSUE_FIELDS})), "State") == "Open"


async def test_an_assignee_off_the_team_in_the_create_body_is_refused_value_not_allowed(
    yt: httpx.AsyncClient, team: Instance
) -> None:
    """Observed, undocumented: the create body's `customFields` writes the same Assignee field the field route does,
    so a user who exists but is not on the project's team is refused there too, in the same words, and no issue is
    made."""
    head = team.store.head()
    refused = refusal(
        await yt.post(
            "/api/issues",
            json={
                "project": {"id": LAUNCH},
                "summary": "Brief the caterers",
                "customFields": [{"name": "Assignee", "$type": "SingleUserIssueCustomField", "value": {"id": VENDOR}}],
            },
        ),
        400,
    )

    assert refused == {
        "error": "",
        "error_description": "Value is not allowed",
        "error_developer_message": "Value is not allowed",
        "error_field": "value",
    }
    assert team.store.head() == head


# --------------------------------------------------------------------------- field writes


async def test_clearing_the_state_is_refused_and_the_issue_still_reads_its_state(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: an issue cannot be in no state, so a clear of State through the field route is refused
    "Value is not allowed" and the issue keeps, and reads back, the state it had."""
    state = await field_id(yt, "LAUNCH-1", "State")

    refused = refusal(await yt.post(f"/api/issues/LAUNCH-1/customFields/{state}", json={"value": None}), 400)
    after = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": ISSUE_FIELDS}))

    assert refused["error_description"] == "Value is not allowed"
    assert value_name(after, "State") == "Open"


async def test_a_due_date_takes_a_clear(yt: httpx.AsyncClient) -> None:
    """Documented: a date field's value is a timestamp or null when empty
    (https://www.jetbrains.com/help/youtrack/devportal/api-entity-DateIssueCustomField.html), so writing null to an
    optional date drops it."""
    due = await field_id(yt, "LAUNCH-1", "Due Date")

    entity(await yt.post(f"/api/issues/LAUNCH-1/customFields/{due}", json={"value": None}))
    after = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "customFields(name,value)"}))

    assert named(after["customFields"], "Due Date")["value"] is None


# --------------------------------------------------------------------------- search


async def test_a_state_search_matches_the_whole_value_name(yt: httpx.AsyncClient) -> None:
    """Documented: a search names a field's value, matched as the whole value
    (https://www.jetbrains.com/help/youtrack/cloud/search-and-command-attributes.html), so `state: Open` does not find
    an issue that is Reopened. A project made from the default template has both states."""
    made = entity(
        await yt.post(
            "/api/admin/projects", json={"name": "Partner Summit", "shortName": "SUMMIT", "leader": {"id": "1-0"}}
        )
    )
    for summary in ("Open the doors", "Reopen the bar"):
        entity(await yt.post("/api/issues", json={"project": {"id": made["id"]}, "summary": summary}))
    for key, value in (("SUMMIT-1", "Open"), ("SUMMIT-2", "Reopened")):
        state = await field_id(yt, key, "State")
        entity(await yt.post(f"/api/issues/{key}/customFields/{state}", json={"value": {"name": value}}))

    found = await search(yt, "project: SUMMIT state: Open")
    reopened = await search(yt, "project: SUMMIT state: Reopened")

    assert [(i["idReadable"], value_name(i, "State")) for i in found] == [("SUMMIT-1", "Open")]
    assert [i["idReadable"] for i in reopened] == ["SUMMIT-2"]


async def test_unresolved_reads_each_projects_own_resolved_values(yt: httpx.AsyncClient) -> None:
    """Documented: #Unresolved follows the resolved flag set on each project's state values, not a list of state
    names (https://www.jetbrains.com/help/youtrack/cloud/search-and-command-attributes.html). Field Ops calls its
    finished state Shipped, a name no other project uses."""
    state = await field_id(yt, "OPS-1", "State")
    open_before = [i["idReadable"] for i in await search(yt, "project: OPS #Unresolved")]
    entity(await yt.post(f"/api/issues/OPS-1/customFields/{state}", json={"value": {"name": "Shipped"}}))

    open_after = [i["idReadable"] for i in await search(yt, "project: OPS #Unresolved")]
    resolved = [i["idReadable"] for i in await search(yt, "project: OPS #Resolved")]

    assert (open_before, open_after, resolved) == (["OPS-1"], [], ["OPS-1"])


async def test_a_field_search_leaves_out_an_issue_holding_another_value(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: a search on Priority or Type finds the issues holding exactly that value, and an issue
    created a moment before, holding the project's default, is not among them."""
    made = entity(
        await yt.post("/api/issues", params={"fields": "idReadable"}, json={"project": {"id": LAUNCH}, "summary": "x"})
    )

    critical = await search(yt, "project: LAUNCH priority: Critical")
    features = await search(yt, "project: LAUNCH type: Feature")

    assert made["idReadable"] not in [i["idReadable"] for i in critical + features]
    assert [(i["idReadable"], value_name(i, "Priority")) for i in critical] == [("LAUNCH-1", "Critical")]
    assert [(i["idReadable"], value_name(i, "Type")) for i in features] == [("LAUNCH-2", "Feature")]


async def test_a_value_the_field_has_not_got_is_refused_invalid_query_not_answered_empty(
    yt: httpx.AsyncClient,
) -> None:
    """Observed, undocumented: Field Ops has no state called Open, and a search for it is refused `invalid_query`
    naming the field, so "nothing matched" and "that is not a value here" never read the same."""
    refused = refusal(await yt.get("/api/issues", params={"query": "project: OPS state: Open", "fields": "id"}), 400)

    assert refused["error"] == "invalid_query"
    assert "isn't used for the State field" in str(refused["error_description"])


# --------------------------------------------------------------------------- links


async def test_a_readable_key_in_a_link_body_is_refused_with_the_entity_id_wording(yt: httpx.AsyncClient) -> None:
    """Documented: a link is added by posting the other issue's database id to one slot of the issue's links
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues-issueID-links.html). Observed: a readable
    key there is refused by its shape, naming it."""
    refused = refusal(await yt.post("/api/issues/LAUNCH-1/links/106-0t/issues", json={"id": "LAUNCH-2"}), 400)

    assert refused["error_description"] == "Invalid structure of entity id: LAUNCH-2"
