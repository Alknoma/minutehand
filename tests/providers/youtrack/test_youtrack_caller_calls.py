"""Every call a production YouTrack client makes, made through the run's proxy with `httpx`, each with the `fields=`
projection that client sends, asserting the wire shape it reads back: `$type` on every entity, only what was asked,
database ids where a body references an entity, epoch milliseconds."""

from __future__ import annotations

import httpx

from minutehand.domain.world import Actor, Operation
from tests.providers.youtrack.caller_world import CLIENT_ID, TOMAS_TOKEN, as_user
from tests.providers.youtrack.youtrack_instance import Instance, entities, entity, millis_now, named, refusal

SEARCH_FIELDS = (
    "id,idReadable,summary,description,created,updated,resolved,"
    "project(id,shortName,name),"
    "customFields(id,name,value(id,name,login,email,fullName,avatarUrl,ordinal,minutes)),"
    "reporter(id,login,email,fullName,avatarUrl),"
    "tags(id,name),"
    "links(id,direction,linkType(id,name,sourceToTarget,targetToSource,directed),issues(id,idReadable,summary))"
)
USER_FIELDS = "id,login,email,fullName,avatarUrl,banned"
PROJECT_FIELDS = "id,shortName,name,description,leader(id,login,fullName),customFields(field(id,name),bundle(values(id,name,ordinal)))"
LAUNCH, OPS = "0-1000", "0-1001"
AGENT, IRIS, TOMAS, NOOR, VENDOR = "1-0", "1-1", "1-2", "1-3", "1-10000"


async def field_id(yt: httpx.AsyncClient, issue: str, name: str) -> str:
    """What the client does before every field write: read the issue's fields and find the one by name."""
    read = entity(await yt.get(f"/api/issues/{issue}", params={"fields": "customFields(id,name)"}))
    found = named(read["customFields"], name)["id"]
    assert isinstance(found, str)
    return found


# --------------------------------------------------------------------------- issues


async def test_search_issues_answers_the_full_projection(yt: httpx.AsyncClient, team: Instance) -> None:
    found = entities(
        await yt.get(
            "/api/issues", params={"query": "project: LAUNCH", "fields": SEARCH_FIELDS, "$skip": 0, "$top": 25}
        )
    )

    assert [i["idReadable"] for i in found] == ["LAUNCH-3", "LAUNCH-1", "LAUNCH-2"]
    notes = found[1]
    assert set(notes) == {
        "id", "idReadable", "summary", "description", "created", "updated", "resolved", "project",
        "customFields", "reporter", "tags", "links", "$type",
    }  # fmt: skip
    assert notes["project"] == {"id": LAUNCH, "shortName": "LAUNCH", "name": "Launch", "$type": "Project"}
    assert notes["reporter"] == {
        "id": IRIS,
        "login": "iris",
        "email": "iris@example.com",
        "fullName": "Iris Calder",
        "avatarUrl": notes["reporter"]["avatarUrl"],  # type: ignore[index]
        "$type": "User",
    }
    assert notes["created"] == millis_now(team.clock) and notes["resolved"] is None
    assert named(notes["customFields"], "Due Date")["value"] == 1787745600000  # 2026-08-26 12:00 UTC
    assert named(notes["customFields"], "Estimation")["value"] == {"id": "600", "minutes": 600, "$type": "PeriodValue"}
    assert named(notes["customFields"], "Story Points") == {
        "id": named(notes["customFields"], "Story Points")["id"],
        "name": "Story Points",
        "value": 3.0,
        "$type": "SimpleIssueCustomField",
    }
    assert named(notes["customFields"], "Priority")["value"] == {
        "id": named(notes["customFields"], "Priority")["value"]["id"],  # type: ignore[index]
        "name": "Critical",
        "ordinal": 1,
        "$type": "EnumBundleElement",
    }
    assert notes["tags"] == [{"id": notes["tags"][0]["id"], "name": "docs", "$type": "IssueTag"}]  # type: ignore[index]
    parent = named_link(notes["links"], "106-0s")
    assert parent["direction"] == "OUTWARD"
    assert parent["linkType"] == {
        "id": "106-0",
        "name": "Subtask",
        "sourceToTarget": "parent for",
        "targetToSource": "subtask of",
        "directed": True,
        "$type": "IssueLinkType",
    }
    assert [i["idReadable"] for i in parent["issues"]] == ["LAUNCH-3"]  # type: ignore[union-attr,index]


def named_link(links: object, slot: str) -> dict[str, object]:
    assert isinstance(links, list)
    return next(link for link in links if isinstance(link, dict) and link["id"] == slot)


async def test_search_without_the_description_leaves_it_out(yt: httpx.AsyncClient) -> None:
    without = SEARCH_FIELDS.replace(",description,", ",")
    found = entities(await yt.get("/api/issues", params={"query": "issue id: LAUNCH-1", "fields": without}))

    assert "description" not in found[0] and found[0]["summary"] == "Write the release notes"


async def test_get_issue_by_readable_or_database_id(yt: httpx.AsyncClient) -> None:
    by_key = entity(await yt.get("/api/issues/LAUNCH-2", params={"fields": SEARCH_FIELDS}))
    by_id = entity(await yt.get(f"/api/issues/{by_key['id']}", params={"fields": "id"}))

    assert by_id == {"id": by_key["id"], "$type": "Issue"}
    assert by_key["resolved"] is not None and named(by_key["customFields"], "State")["value"]["name"] == "Fixed"  # type: ignore[index]


async def test_create_issue_takes_the_project_database_id_and_fills_defaults(yt: httpx.AsyncClient) -> None:
    fields = "id,idReadable,summary,description,created,project(id,shortName,name),customFields(id,name,value(id,name))"
    made = entity(
        await yt.post(
            "/api/issues",
            params={"fields": fields},
            json={"project": {"id": LAUNCH}, "summary": "Print the badges", "description": "Two hundred"},
        )
    )

    assert made["idReadable"] == "LAUNCH-4" and made["project"]["shortName"] == "LAUNCH"  # type: ignore[index]
    assert {n: named(made["customFields"], n)["value"] for n in ("Priority", "Type", "State", "Assignee")} == {
        "Priority": {"id": made_value(made, "Priority"), "name": "Normal", "$type": "EnumBundleElement"},
        "Type": {"id": made_value(made, "Type"), "name": "Bug", "$type": "EnumBundleElement"},
        "State": {"id": made_value(made, "State"), "name": "Open", "$type": "StateBundleElement"},
        "Assignee": None,
    }
    refusal(await yt.post("/api/issues", json={"project": {"id": "LAUNCH"}, "summary": "x"}), 400)


def made_value(made: dict[str, object], name: str) -> object:
    value = named(made["customFields"], name)["value"]
    assert isinstance(value, dict)
    return value["id"]


async def test_update_issue_writes_summary_and_description(yt: httpx.AsyncClient, team: Instance) -> None:
    answer = entity(
        await yt.post(
            "/api/issues/LAUNCH-1",
            params={"fields": "id,idReadable,summary,description,updated,customFields(id,name,value(id,name))"},
            json={"summary": "Write and send the release notes", "description": "Covers pricing"},
        )
    )

    assert (answer["summary"], answer["description"]) == ("Write and send the release notes", "Covers pricing")
    assert answer["updated"] == millis_now(team.clock)


async def test_delete_issue_then_it_is_gone_404(yt: httpx.AsyncClient, team: Instance) -> None:
    gone = await yt.delete("/api/issues/LAUNCH-2")

    assert (gone.status_code, gone.content) == (200, b"")
    refusal(await yt.get("/api/issues/LAUNCH-2", params={"fields": "id"}), 404)
    refusal(await yt.delete("/api/issues/LAUNCH-2"), 404)
    deleted = [e for e in team.store.events() if e.operation is Operation.DELETE]
    assert [e.actor for e in deleted] == [Actor.AGENT]


async def test_state_moves_by_the_custom_field_route_and_stamps_resolved(yt: httpx.AsyncClient, team: Instance) -> None:
    state = await field_id(yt, "LAUNCH-1", "State")
    answer = entity(await yt.post(f"/api/issues/LAUNCH-1/customFields/{state}", json={"value": {"name": "Fixed"}}))
    resolved = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "resolved"}))["resolved"]
    entity(await yt.post(f"/api/issues/LAUNCH-1/customFields/{state}", json={"value": {"name": "Open"}}))
    reopened = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "resolved"}))["resolved"]

    assert answer == {"id": state, "$type": "StateIssueCustomField"}
    assert resolved == millis_now(team.clock) and reopened is None


async def test_assignee_moves_by_database_id_and_off_the_team_is_refused(yt: httpx.AsyncClient) -> None:
    assignee = await field_id(yt, "LAUNCH-1", "Assignee")
    entity(await yt.post(f"/api/issues/LAUNCH-1/customFields/{assignee}", json={"value": {"id": NOOR}}))
    read = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "customFields(name,value(id,login))"}))
    off_team = refusal(
        await yt.post(f"/api/issues/LAUNCH-1/customFields/{assignee}", json={"value": {"id": VENDOR}}), 400
    )
    unassigned = entity(await yt.post(f"/api/issues/LAUNCH-1/customFields/{assignee}", json={"value": None}))

    assert named(read["customFields"], "Assignee")["value"] == {"id": NOOR, "login": "noor", "$type": "User"}
    assert off_team == {
        "error": "",
        "error_description": "Value is not allowed",
        "error_developer_message": "Value is not allowed",
        "error_field": "value",
    }
    assert unassigned["$type"] == "SingleUserIssueCustomField"


async def test_priority_and_type_take_a_bundle_value_by_name(yt: httpx.AsyncClient) -> None:
    priority = await field_id(yt, "OPS-1", "Priority")
    kind = await field_id(yt, "OPS-1", "Type")
    entity(await yt.post(f"/api/issues/OPS-1/customFields/{priority}", json={"value": {"name": "P1 - Urgent"}}))
    entity(await yt.post(f"/api/issues/OPS-1/customFields/{kind}", json={"value": {"name": "Epic"}}))
    read = entity(await yt.get("/api/issues/OPS-1", params={"fields": "customFields(name,value(name))"}))
    not_in_this_bundle = refusal(
        await yt.post(f"/api/issues/OPS-1/customFields/{priority}", json={"value": {"name": "Critical"}}), 501
    )

    assert named(read["customFields"], "Priority")["value"] == {"name": "P1 - Urgent", "$type": "EnumBundleElement"}
    assert named(read["customFields"], "Type")["value"] == {"name": "Epic", "$type": "EnumBundleElement"}
    assert "the value Critical for Priority, which its bundle has not got" in str(
        not_in_this_bundle["error_description"]
    ), "unrecorded for a bundle: refused by name"


async def test_due_date_takes_epoch_milliseconds_and_story_points_a_number(yt: httpx.AsyncClient) -> None:
    due = await field_id(yt, "LAUNCH-2", "Due Date")
    points = await field_id(yt, "LAUNCH-2", "Story Points")
    entity(await yt.post(f"/api/issues/LAUNCH-2/customFields/{due}", json={"value": 1788004800000}))
    entity(await yt.post(f"/api/issues/LAUNCH-2/customFields/{points}", json={"value": 5}))
    read = entity(await yt.get("/api/issues/LAUNCH-2", params={"fields": "customFields(name,value)"}))
    as_text = refusal(await yt.post(f"/api/issues/LAUNCH-2/customFields/{due}", json={"value": "2026-08-29"}), 400)

    assert named(read["customFields"], "Due Date")["value"] == 1788004800000
    assert named(read["customFields"], "Story Points")["value"] == 5.0
    assert as_text["error_description"] == "Value is not allowed"


async def test_sprint_takes_a_version_by_name_and_clears(yt: httpx.AsyncClient) -> None:
    sprint = await field_id(yt, "LAUNCH-1", "Sprint")
    entity(await yt.post(f"/api/issues/LAUNCH-1/customFields/{sprint}", json={"value": {"name": "Sprint 2"}}))
    set_to = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "customFields(name,value(id,name))"}))
    entity(await yt.post(f"/api/issues/LAUNCH-1/customFields/{sprint}", json={"value": None}))
    cleared = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "customFields(name,value(name))"}))

    assert named(set_to["customFields"], "Sprint")["$type"] == "SingleVersionIssueCustomField"
    assert named(set_to["customFields"], "Sprint")["value"]["name"] == "Sprint 2"  # type: ignore[index]
    assert named(cleared["customFields"], "Sprint")["value"] is None


async def test_a_field_the_project_does_not_carry_is_absent_and_refused_404(yt: httpx.AsyncClient) -> None:
    read = entity(await yt.get("/api/issues/OPS-1", params={"fields": "customFields(id,name)"}))
    names = [f["name"] for f in read["customFields"]]  # type: ignore[union-attr,index]
    due = await field_id(yt, "LAUNCH-1", "Due Date")

    assert names == ["Priority", "Type", "State", "Assignee"]
    refusal(await yt.post(f"/api/issues/OPS-1/customFields/{due}", json={"value": 1788004800000}), 404)


async def test_add_comment_answers_its_author(yt: httpx.AsyncClient, team: Instance) -> None:
    made = entity(
        await yt.post(
            "/api/issues/LAUNCH-1/comments",
            params={"fields": "id,text,created,author(id,login,fullName)"},
            json={"text": "Draft attached"},
        )
    )
    listed = entities(await yt.get("/api/issues/LAUNCH-1/comments", params={"fields": "text,author(login)"}))

    assert made == {
        "id": made["id"],
        "text": "Draft attached",
        "created": millis_now(team.clock),
        "author": {"id": AGENT, "login": "agent-bot", "fullName": "Agent", "$type": "User"},
        "$type": "IssueComment",
    }
    assert [c["text"] for c in listed] == ["Due before the partner call", "Draft attached"]


async def test_count_issues_answers_the_number_asked_for(yt: httpx.AsyncClient) -> None:
    counted = entity(
        await yt.post("/api/issuesGetter/count", params={"fields": "count"}, json={"query": "project: LAUNCH"})
    )
    unasked = entity(await yt.post("/api/issuesGetter/count", json={"query": "project: LAUNCH"}))

    assert counted == {"count": 3, "$type": "IssueCountResponse"}
    assert unasked == {"id": "IssueCountResponse", "$type": "IssueCountResponse"}


# --------------------------------------------------------------------------- projects and fields


async def test_get_project_with_its_bundles(yt: httpx.AsyncClient) -> None:
    project = entity(await yt.get("/api/admin/projects/OPS", params={"fields": PROJECT_FIELDS}))

    assert (project["id"], project["shortName"], project["leader"]) == (
        OPS,
        "OPS",
        {"id": AGENT, "login": "agent-bot", "fullName": "Agent", "$type": "User"},
    )
    state = named(project["customFields"], "State")
    assert [v["name"] for v in state["bundle"]["values"]] == ["Backlog", "Doing", "Shipped", "Dropped"]  # type: ignore[index]
    assert named(project["customFields"], "Assignee")["bundle"] == {"$type": "UserBundle"}


async def test_project_custom_fields_with_resolved_flags(yt: httpx.AsyncClient) -> None:
    fields = entities(
        await yt.get(
            f"/api/admin/projects/{OPS}/customFields",
            params={"fields": "field(id,name),bundle(values(id,name,ordinal,isResolved))"},
        )
    )

    states = named(fields, "State")["bundle"]["values"]  # type: ignore[index]
    assert [(v["name"], v["isResolved"]) for v in states] == [
        ("Backlog", False),
        ("Doing", False),
        ("Shipped", True),
        ("Dropped", True),
    ]


async def test_project_custom_fields_with_the_assignee_bundle(yt: httpx.AsyncClient) -> None:
    fields = entities(
        await yt.get(
            "/api/admin/projects/LAUNCH/customFields",
            params={"fields": "field(id,name),bundle(id,aggregatedUsers(id,login,name,fullName,email))"},
        )
    )

    users = named(fields, "Assignee")["bundle"]["aggregatedUsers"]  # type: ignore[index]
    assert [u["login"] for u in users] == ["agent-bot", "iris", "tomas", "noor"]
    assert users[1] == {
        "id": IRIS,
        "login": "iris",
        "name": "Iris Calder",
        "fullName": "Iris Calder",
        "email": "iris@example.com",
        "$type": "User",
    }
    assert named(fields, "Due Date")["bundle"] is None


async def test_project_field_names_off_the_project_resource(yt: httpx.AsyncClient) -> None:
    project = entity(await yt.get(f"/api/admin/projects/{OPS}", params={"fields": "customFields(field(name))"}))

    assert [f["field"]["name"] for f in project["customFields"]] == ["Priority", "Type", "State", "Assignee"]  # type: ignore[union-attr,index]


async def test_list_projects_pages(yt: httpx.AsyncClient) -> None:
    first = entities(await yt.get("/api/admin/projects", params={"fields": "id,shortName,name", "$skip": 0, "$top": 1}))
    rest = entities(
        await yt.get("/api/admin/projects", params={"fields": "id,shortName,name", "$skip": 1, "$top": 100})
    )

    assert first == [{"id": LAUNCH, "shortName": "LAUNCH", "name": "Launch", "$type": "Project"}]
    assert rest == [{"id": OPS, "shortName": "OPS", "name": "Field Ops", "$type": "Project"}]


async def test_project_team(yt: httpx.AsyncClient) -> None:
    team = entity(
        await yt.get(f"/api/admin/projects/{LAUNCH}/team", params={"fields": "id,users(id,login,name,fullName,email)"})
    )

    assert team["$type"] == "ProjectTeam"
    assert [u["login"] for u in team["users"]] == ["agent-bot", "iris", "tomas", "noor"]  # type: ignore[union-attr,index]


async def test_adding_to_a_team_through_youtrack_is_refused_405(yt: httpx.AsyncClient) -> None:
    refusal(await yt.post(f"/api/admin/projects/{LAUNCH}/team/users", json={"id": VENDOR}), 405)


async def test_create_project_from_the_default_template(yt: httpx.AsyncClient) -> None:
    made = entity(
        await yt.post(
            "/api/admin/projects",
            params={"fields": "id,shortName,name"},
            json={"name": "Partner Summit", "shortName": "SUMMIT", "leader": {"id": AGENT}, "description": "Why"},
        )
    )
    fields = entities(await yt.get(f"/api/admin/projects/{made['id']}/customFields", params={"fields": "field(name)"}))
    team = entity(await yt.get(f"/api/admin/projects/{made['id']}/team", params={"fields": "users(login)"}))
    states = entity(await yt.get("/api/admin/projects/SUMMIT", params={"fields": PROJECT_FIELDS}))

    assert made == {"id": "0-1002", "shortName": "SUMMIT", "name": "Partner Summit", "$type": "Project"}
    assert [f["field"]["name"] for f in fields] == ["Priority", "Type", "State", "Assignee"]  # type: ignore[index]
    assert team["users"] == [{"login": "agent-bot", "$type": "User"}]
    assert [v["name"] for v in named(states["customFields"], "State")["bundle"]["values"]][:2] == ["Submitted", "Open"]  # type: ignore[index]
    refusal(
        await yt.post("/api/admin/projects", json={"name": "Again", "shortName": "SUMMIT", "leader": {"id": AGENT}}),
        501,
    )
    refusal(
        await yt.post(
            "/api/admin/projects", json={"name": "By login", "shortName": "BYLOGIN", "leader": {"id": "agent-bot"}}
        ),
        400,
    )


async def test_list_instance_custom_fields_with_their_types(yt: httpx.AsyncClient) -> None:
    fields = entities(
        await yt.get("/api/admin/customFieldSettings/customFields", params={"fields": "id,name,fieldType(id)"})
    )

    assert {f["name"]: f["fieldType"]["id"] for f in fields} == {  # type: ignore[index]
        "State": "state[1]",
        "Assignee": "user[1]",
        "Priority": "enum[1]",
        "Type": "enum[1]",
        "Due Date": "date",
        "Estimation": "period",
        "Spent time": "period",
        "Story Points": "float",
        "Sprint": "version[1]",
    }
    assert named(fields, "Due Date") == {
        "id": "58-5",
        "name": "Due Date",
        "fieldType": {"id": "date", "$type": "FieldType"},
        "$type": "CustomField",
    }


async def test_attaching_a_field_to_a_created_project_is_not_refused_for_update_project(
    yt: httpx.AsyncClient, team: Instance
) -> None:
    """Hub's cache lists nobody holding Update Project on a project made through the API, but Minutehand enforces no
    permission: the attach is answered."""
    made = entity(
        await yt.post(
            "/api/admin/projects", json={"name": "Partner Summit", "shortName": "SUMMIT", "leader": {"id": AGENT}}
        )
    )
    body = {"field": {"id": "58-5"}, "$type": "SimpleProjectCustomField"}
    on_made = entity(await yt.post(f"/api/admin/projects/{made['id']}/customFields", json=body))
    attached = entity(
        await yt.post(f"/api/admin/projects/{OPS}/customFields", params={"fields": "id,field(id,name)"}, json=body)
    )
    again = refusal(await yt.post(f"/api/admin/projects/{OPS}/customFields", json=body), 501)
    wrong_type = refusal(
        await yt.post(
            f"/api/admin/projects/{OPS}/customFields",
            json={"field": {"id": "58-6"}, "$type": "SimpleProjectCustomField"},
        ),
        501,
    )

    assert on_made["$type"] == "SimpleProjectCustomField"
    assert attached == {
        "id": attached["id"],
        "field": {"id": "58-5", "name": "Due Date", "$type": "CustomField"},
        "$type": "SimpleProjectCustomField",
    }
    assert "which OPS already carries" in str(again["error_description"])
    assert "PeriodProjectCustomField" in str(wrong_type["error_description"])
    due = await field_id(yt, "OPS-1", "Due Date")
    entity(await yt.post(f"/api/issues/OPS-1/customFields/{due}", json={"value": 1788004800000}))


# --------------------------------------------------------------------------- users


async def test_get_user_for_its_hub_id(yt: httpx.AsyncClient) -> None:
    """`/users/{login}` reads a user as `/users/{id}` does
    (https://www.jetbrains.com/help/youtrack/devportal/api-users-yt-vs-hub.html)."""
    user = entity(await yt.get(f"/api/users/{NOOR}", params={"fields": "id,login,ringId"}))

    by_login = entity(await yt.get("/api/users/noor", params={"fields": "id,login"}))

    assert set(user) == {"id", "login", "ringId", "$type"} and user["login"] == "noor"
    assert by_login == {"id": NOOR, "login": "noor", "$type": "User"}
    refusal(await yt.get("/api/users/1-99", params={"fields": "id"}), 404)


async def test_find_users_pages_and_a_query_is_refused_by_name(yt: httpx.AsyncClient) -> None:
    """YouTrack's description of `GET /users` takes `fields`, `$skip` and `$top` and no `query`: a filter is refused
    by name rather than guessed."""
    found = entities(await yt.get("/api/users", params={"fields": USER_FIELDS, "$top": 2, "$skip": 2}))
    noor = entities(await yt.get("/api/users", params={"fields": USER_FIELDS, "$top": 1, "$skip": 3}))
    refused = refusal(await yt.get("/api/users", params={"query": "halvorsen", "fields": "id"}), 501)

    assert [u["login"] for u in found] == ["tomas", "noor"]
    assert "GET /users takes no query parameter" in str(refused["error_description"])
    assert noor == [
        {
            "id": NOOR,
            "login": "noor",
            "email": "noor@example.com",
            "fullName": "Noor Halvorsen",
            "avatarUrl": noor[0]["avatarUrl"],
            "banned": False,
            "$type": "User",
        }
    ]


async def test_current_user_is_the_tokens(yt: httpx.AsyncClient) -> None:
    me = entity(await yt.get("/api/users/me", params={"fields": "id,login,fullName,email"}))
    tomas = entity(await yt.get("/api/users/me", params={"fields": "login"}, headers=as_user(TOMAS_TOKEN)))

    assert me == {
        "id": AGENT,
        "login": "agent-bot",
        "fullName": "Agent",
        "email": "agent-bot@youtrack.invalid",
        "$type": "Me",
    }
    assert tomas == {"login": "tomas", "$type": "Me"}


async def test_get_user_with_the_user_fields(yt: httpx.AsyncClient) -> None:
    user = entity(await yt.get(f"/api/users/{TOMAS}", params={"fields": "id,login,fullName"}))

    assert user == {"id": TOMAS, "login": "tomas", "fullName": "Tomas Brandt", "$type": "User"}


# --------------------------------------------------------------------------- links


async def test_link_types_name_each_direction(yt: httpx.AsyncClient) -> None:
    types = entities(
        await yt.get(
            "/api/issueLinkTypes", params={"fields": "id,name,sourceToTarget,targetToSource,directed", "$top": 100}
        )
    )

    assert [(t["id"], t["sourceToTarget"], t["targetToSource"], t["directed"]) for t in types] == [
        ("106-0", "parent for", "subtask of", True),
        ("106-1", "is required for", "depends on", True),
        ("106-2", "duplicates", "is duplicated by", True),
        ("106-3", "relates to", "relates to", False),
    ]


async def test_a_link_is_added_to_one_slot_by_database_id_and_read_from_both_ends(yt: httpx.AsyncClient) -> None:
    venue = entity(await yt.get("/api/issues/LAUNCH-2", params={"fields": "id"}))
    added = await yt.post("/api/issues/LAUNCH-1/links/106-1t/issues", json={"id": venue["id"]})
    mine = entities(await yt.get("/api/issues/LAUNCH-1/links", params={"fields": "id,direction,issues(idReadable)"}))
    theirs = entities(await yt.get("/api/issues/LAUNCH-2/links", params={"fields": "id,direction,issues(idReadable)"}))

    assert (added.status_code, added.content) == (200, b"")
    assert named_link(mine, "106-1t") == {
        "id": "106-1t",
        "direction": "INWARD",
        "issues": [{"idReadable": "LAUNCH-2", "$type": "Issue"}],
        "$type": "IssueLink",
    }
    assert named_link(theirs, "106-1s")["issues"] == [{"idReadable": "LAUNCH-1", "$type": "Issue"}]
    refusal(await yt.post("/api/issues/LAUNCH-1/links/106-1t/issues", json={"id": "LAUNCH-2"}), 400)
    refusal(await yt.post("/api/issues/LAUNCH-1/links/106-3s/issues", json={"id": venue["id"]}), 404)
    refusal(await yt.post("/api/issues/LAUNCH-1/links", json={"id": venue["id"]}), 405)


async def test_an_undirected_link_uses_the_bare_type_id(yt: httpx.AsyncClient) -> None:
    kits = entity(await yt.get("/api/issues/OPS-1", params={"fields": "id"}))
    entity_answer = await yt.post("/api/issues/LAUNCH-2/links/106-3/issues", json={"id": kits["id"]})
    both = named_link(
        entities(await yt.get("/api/issues/OPS-1/links", params={"fields": "id,direction,issues(idReadable)"})), "106-3"
    )

    assert entity_answer.status_code == 200
    assert (both["direction"], both["issues"]) == ("BOTH", [{"idReadable": "LAUNCH-2", "$type": "Issue"}])


async def test_a_link_is_removed_from_its_slot(yt: httpx.AsyncClient) -> None:
    notes = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "id"}))
    removed = await yt.delete(f"/api/issues/LAUNCH-3/links/106-0t/issues/{notes['id']}")
    after = named_link(
        entities(await yt.get("/api/issues/LAUNCH-3/links", params={"fields": "id,issues(id)"})), "106-0t"
    )

    assert removed.status_code == 200 and after["issues"] == []
    refusal(await yt.delete(f"/api/issues/LAUNCH-3/links/106-0t/issues/{notes['id']}"), 404)


# --------------------------------------------------------------------------- tags


async def test_tags_list_and_create(yt: httpx.AsyncClient) -> None:
    listed = entities(await yt.get("/api/tags", params={"fields": "id,name", "$top": 1000}))
    made = entity(await yt.post("/api/tags", params={"fields": "id,name"}, json={"name": "press"}))

    assert [t["name"] for t in listed] == ["docs", "venue", "big room"]
    assert made == {"id": made["id"], "name": "press", "$type": "IssueTag"}
    refusal(await yt.post("/api/tags", json={"name": "press"}), 400)


async def test_an_issue_carries_an_existing_tag_by_id_and_drops_it(yt: httpx.AsyncClient) -> None:
    press = entity(await yt.post("/api/tags", params={"fields": "id,name"}, json={"name": "press"}))
    added = entity(await yt.post("/api/issues/LAUNCH-1/tags", params={"fields": "id,name"}, json={"id": press["id"]}))
    carried = entities(await yt.get("/api/issues/LAUNCH-1/tags", params={"fields": "id,name"}))
    dropped = await yt.delete(f"/api/issues/LAUNCH-1/tags/{press['id']}")
    left = entities(await yt.get("/api/issues/LAUNCH-1/tags", params={"fields": "name"}))

    assert added == press
    assert [t["name"] for t in carried] == ["docs", "press"]
    assert dropped.status_code == 200 and [t["name"] for t in left] == ["docs"]
    refusal(await yt.post("/api/issues/LAUNCH-1/tags", json={"name": "press"}), 400)
    refusal(await yt.post("/api/issues/LAUNCH-1/tags", json={"id": "6-999"}), 501)


# --------------------------------------------------------------------------- Hub


async def test_hub_holds_no_youtrack_project(yt: httpx.AsyncClient) -> None:
    """Since YouTrack 2026.1 Hub's `/projects` answers Hub's own projects and no YouTrack one
    (https://www.jetbrains.com/help/youtrack/devportal/hub-rest-api-deprecated-endpoints-2026-1.html); a live
    instance answered `total: 0` for a project the account had made itself."""
    page = entity(
        await yt.get("/hub/api/rest/projects", params={"query": "key: LAUNCH", "fields": "id,key,name,team(id,name)"})
    )

    assert page == {"skip": 0, "top": 100, "total": 0, "projects": []}


async def test_a_user_joins_a_team_through_own_users_and_the_assignee_bundle_grows(yt: httpx.AsyncClient) -> None:
    """`POST /admin/projects/{id}/team/ownUsers` with the user's database id is how a team grows since YouTrack 2026.1
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects-projectID-team-ownUsers.html)."""
    assignee = await field_id(yt, "LAUNCH-1", "Assignee")
    refused = refusal(
        await yt.post(f"/api/issues/LAUNCH-1/customFields/{assignee}", json={"value": {"id": VENDOR}}), 400
    )
    added = entity(
        await yt.post(f"/api/admin/projects/{LAUNCH}/team/ownUsers", params={"fields": "id,login"}, json={"id": VENDOR})
    )
    entity(await yt.post(f"/api/issues/LAUNCH-1/customFields/{assignee}", json={"value": {"id": VENDOR}}))
    users = entities(await yt.get(f"/api/admin/projects/{LAUNCH}/team/users", params={"fields": "login"}))

    assert refused["error_description"] == "Value is not allowed"
    assert added == {"id": VENDOR, "login": "vendor", "$type": "User"}
    assert [u["login"] for u in users][-1] == "vendor"
    refusal(await yt.post(f"/api/admin/projects/{LAUNCH}/team/ownUsers", json={"id": "vendor"}), 400)


async def test_hub_permissions_cache_lists_where_each_is_held(yt: httpx.AsyncClient, team: Instance) -> None:
    cache = await yt.get(
        "/hub/api/rest/permissions/cache",
        params={"fields": "permission/key,permission/name,global,projects/id,projects/key"},
    )
    entries = cache.json()
    by_key = {e["permission"]["key"]: e for e in entries}
    launch = team.youtrack.project(LAUNCH)

    assert cache.status_code == 200 and launch is not None
    assert by_key["jetbrains.jetpass.project-create"] == {
        "permission": {"key": "jetbrains.jetpass.project-create", "name": "Create Project"},
        "global": True,
        "projects": [],
    }
    assert by_key["jetbrains.jetpass.project-update"]["global"] is False
    assert [p["key"] for p in by_key["jetbrains.jetpass.project-update"]["projects"]] == ["LAUNCH", "OPS"]
    assert by_key["jetbrains.jetpass.project-update"]["projects"][0]["id"] == launch.ringId


async def test_a_project_made_through_the_api_has_no_hub_project(yt: httpx.AsyncClient) -> None:
    await yt.post(
        "/api/admin/projects", json={"name": "Partner Summit", "shortName": "SUMMIT", "leader": {"id": AGENT}}
    )
    page = entity(await yt.get("/hub/api/rest/projects", params={"query": "key: SUMMIT", "fields": "id"}))

    assert (page["total"], page["projects"]) == (0, [])


async def test_hub_issues_a_token_to_a_service_that_then_acts_as_its_user(yt: httpx.AsyncClient) -> None:
    import base64

    basic = base64.b64encode(f"{CLIENT_ID}:s3cr3t-planning".encode()).decode()
    issued = await yt.post(
        "/hub/api/rest/oauth2/token",
        content=b"grant_type=client_credentials&scope=YouTrack",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    token = issued.json()["access_token"]
    me = entity(await yt.get("/api/users/me", params={"fields": "login"}, headers=as_user(token)))
    wrong = await yt.post(
        "/hub/api/rest/oauth2/token",
        content=f"grant_type=client_credentials&client_id={CLIENT_ID}&client_secret=nope".encode(),
        headers={"Authorization": "", "Content-Type": "application/x-www-form-urlencoded"},
    )
    as_wrong = entity(
        await yt.get("/api/users/me", params={"fields": "login"}, headers=as_user(wrong.json()["access_token"]))
    )

    assert issued.status_code == 200
    assert issued.json() == {"access_token": token, "token_type": "bearer", "expires_in": 3600, "scope": "YouTrack"}
    assert me == {"login": "agent-bot", "$type": "Me"}
    assert wrong.status_code == 200 and as_wrong["login"] == "agent-bot"
