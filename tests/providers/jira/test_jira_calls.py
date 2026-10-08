"""Every call a tracker client makes to Jira Cloud, through the proxy, each with the query string and body that
client sends, asserting the wire shape it reads back."""

from __future__ import annotations

from minutehand.domain.world import Actor, Operation
from tests.providers.jira.jira_site import (
    AGENT,
    AGILE,
    API,
    IRIS,
    NOOR,
    SITE,
    TOMAS,
    Site,
    ok,
)


def _doc(text: str) -> dict[str, object]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


async def test_myself_is_the_account_the_token_signs_in_as(site: Site) -> None:
    me = ok(await site.http.get(f"{API}/myself"))
    assert me["accountId"] == AGENT and me["accountType"] == "atlassian" and me["active"] is True
    assert me["displayName"] == "Lantern Agent" and me["emailAddress"] == "agent@lanternworks.example"
    assert set(me["avatarUrls"]) == {"48x48", "24x24", "16x16", "32x32"}
    assert me["self"] == f"{API}/user?accountId={AGENT}"


async def test_mypermissions_answers_global_keys_and_project_keys_for_a_project(site: Site) -> None:
    globally = ok(await site.http.get(f"{API}/mypermissions", params={"permissions": "ADMINISTER,CREATE_PROJECT"}))
    assert {k: v["havePermission"] for k, v in globally["permissions"].items()} == {
        "ADMINISTER": True,
        "CREATE_PROJECT": True,
    }
    assert globally["permissions"]["ADMINISTER"]["type"] == "GLOBAL"
    launch = ok(
        await site.http.get(
            f"{API}/mypermissions", params={"permissions": "ADMINISTER_PROJECTS", "projectKey": "LAUNCH"}
        )
    )
    assert launch["permissions"]["ADMINISTER_PROJECTS"] == {
        "id": "101",
        "key": "ADMINISTER_PROJECTS",
        "name": "Administer Projects",
        "type": "PROJECT",
        "description": "",
        "havePermission": True,
    }


async def test_project_search_pages_with_is_last(site: Site) -> None:
    first = ok(await site.http.get(f"{API}/project/search", params={"startAt": "0", "maxResults": "1"}))
    second = ok(await site.http.get(f"{API}/project/search", params={"startAt": "1", "maxResults": "1"}))
    assert (first["total"], first["isLast"], second["isLast"]) == (2, False, True)
    assert [first["values"][0]["key"], second["values"][0]["key"]] == ["FIELD", "LAUNCH"], "by key; VAULT unseen"
    assert {"id", "key", "name", "projectTypeKey", "self"} <= set(first["values"][0])
    assert isinstance(first["values"][0]["id"], str)


async def test_project_get_has_issue_types_and_lead_but_no_statuses(site: Site) -> None:
    project = ok(await site.http.get(f"{API}/project/LAUNCH"))
    assert project["key"] == "LAUNCH" and project["lead"]["accountId"] == IRIS
    assert [t["name"] for t in project["issueTypes"]] == ["Epic", "Task", "Story", "Bug", "Subtask"]
    assert "statuses" not in project
    assert ok(await site.http.get(f"{API}/project/{project['id']}"))["key"] == "LAUNCH"


async def test_project_create_answers_a_numeric_id_and_a_board(site: Site) -> None:
    made = ok(
        await site.http.post(
            f"{API}/project",
            json={
                "key": "BETA",
                "name": "Beta programme",
                "leadAccountId": AGENT,
                "projectTypeKey": "software",
                "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-simplified-agility-kanban",
                "description": "Who tries it first",
            },
        ),
        201,
    )
    assert made == {"self": f"{API}/project/{made['id']}", "id": made["id"], "key": "BETA"}
    assert isinstance(made["id"], int)
    types = ok(await site.http.get(f"{API}/project/BETA"))["issueTypes"]
    assert [t["name"] for t in types] == ["Epic", "Task", "Subtask"]
    boards = ok(await site.http.get(f"{AGILE}/board", params={"projectKeyOrId": "BETA"}))
    assert [b["type"] for b in boards["values"]] == ["kanban"]
    assert site.store.events()[-2].actor is Actor.AGENT


async def test_project_statuses_per_issue_type_carry_status_categories(site: Site) -> None:
    found = ok(await site.http.get(f"{API}/project/LAUNCH/statuses"))
    story = next(t for t in found if t["name"] == "Story")
    assert [(s["name"], s["statusCategory"]["key"]) for s in story["statuses"]] == [
        ("To Do", "new"),
        ("In Progress", "indeterminate"),
        ("In Review", "indeterminate"),
        ("Done", "done"),
        ("Won't Do", "done"),
    ]
    assert story["subtask"] is False and next(t for t in found if t["name"] == "Subtask")["subtask"] is True


async def test_search_jql_post_answers_the_fields_asked_and_is_last(site: Site) -> None:
    found = ok(
        await site.http.post(
            f"{API}/search/jql",
            json={"jql": "project = LAUNCH ORDER BY created ASC", "maxResults": 25, "fields": ["summary", "status"]},
        )
    )
    assert found["isLast"] is True and "nextPageToken" not in found
    assert [(i["key"], set(i["fields"])) for i in found["issues"]] == [
        ("LAUNCH-2", {"summary", "status"}),
        ("LAUNCH-1", {"summary", "status"}),
    ]


async def test_search_jql_with_no_fields_answers_ids_only_and_pages_by_token(site: Site) -> None:
    first = ok(
        await site.http.get(f"{API}/search/jql", params={"jql": "project in (LAUNCH, FIELD)", "maxResults": "2"})
    )
    assert first["isLast"] is False and [set(i) for i in first["issues"]] == [
        {"expand", "id", "self", "key"},
        {"expand", "id", "self", "key"},
    ]
    second = ok(
        await site.http.post(
            f"{API}/search/jql",
            json={"jql": "project in (LAUNCH, FIELD)", "maxResults": 2, "nextPageToken": first["nextPageToken"]},
        )
    )
    assert second["isLast"] is True and len(second["issues"]) == 1
    keys = [i["key"] for i in first["issues"] + second["issues"]]
    assert sorted(keys) == ["FIELD-1", "LAUNCH-1", "LAUNCH-2"]
    assert site.store.events()[-1].operation is Operation.SEARCH


async def test_approximate_count_counts_what_the_query_matches(site: Site) -> None:
    assert ok(await site.http.post(f"{API}/search/approximate-count", json={"jql": 'project = "LAUNCH"'})) == {
        "count": 2
    }


async def test_issue_get_answers_every_field_the_client_reads(site: Site) -> None:
    custom = "customfield_10016,customfield_10020,customfield_10014"
    asked = (
        "summary,description,project,issuetype,status,priority,assignee,reporter,parent,subtasks,labels,"
        f"components,fixVersions,issuelinks,created,updated,resolutiondate,duedate,timetracking,{custom}"
    )
    issue = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": asked}))
    fields = issue["fields"]
    assert set(fields) == set(asked.split(",")) - {"parent"}, "an issue with no parent carries no parent field"
    assert (issue["key"], issue["id"].isdigit()) == ("LAUNCH-1", True)
    assert fields["project"]["key"] == "LAUNCH" and fields["issuetype"]["name"] == "Story"
    assert fields["status"]["statusCategory"]["key"] == "new"
    assert fields["priority"] == {
        "self": f"{API}/priority/2",
        "iconUrl": f"{SITE}/images/icons/priorities/high.svg",
        "name": "High",
        "id": "2",
    }
    assert fields["assignee"]["accountId"] == TOMAS and fields["reporter"]["accountId"] == IRIS
    assert fields["labels"] == ["docs", "beta"] and fields["duedate"] == "2026-08-28"
    assert fields["created"] == "2026-08-24T10:50:03.000+0000" and fields["resolutiondate"] is None
    assert fields["description"]["content"][1]["content"][0]["text"] == "Link the changelog."
    assert fields["timetracking"] == {
        "originalEstimate": "1d",
        "remainingEstimate": "5h",
        "originalEstimateSeconds": 28800,
        "remainingEstimateSeconds": 18000,
        "timeSpent": "3h",
        "timeSpentSeconds": 10800,
    }
    assert fields["customfield_10016"] == 5 and fields["customfield_10014"] is None
    assert [(s["name"], s["state"], s["boardId"]) for s in fields["customfield_10020"]] == [("Launch 2", "active", 1)]
    [link] = fields["issuelinks"]
    assert link["type"]["outward"] == "blocks" and link["outwardIssue"]["key"] == "LAUNCH-2"
    assert link["outwardIssue"]["fields"]["summary"] == "Book the venue"
    venue = ok(await site.http.get(f"{API}/issue/LAUNCH-2", params={"fields": "issuelinks,resolutiondate"}))
    assert venue["fields"]["issuelinks"][0]["inwardIssue"]["key"] == "LAUNCH-1"
    assert venue["fields"]["resolutiondate"] == "2026-08-21T10:50:03.000+0000"


async def test_issue_create_answers_id_key_and_self(site: Site) -> None:
    project = ok(await site.http.get(f"{API}/project/LAUNCH"))
    made = ok(
        await site.http.post(
            f"{API}/issue",
            json={
                "fields": {
                    "project": {"id": project["id"]},
                    "summary": "Print the posters",
                    "description": _doc("A3, matte."),
                    "issuetype": {"name": "Task"},
                    "priority": {"name": "Low"},
                    "assignee": {"accountId": NOOR},
                    "duedate": "2026-09-01",
                }
            },
        ),
        201,
    )
    assert made == {"id": made["id"], "key": "LAUNCH-3", "self": f"{API}/issue/{made['id']}"}
    read = ok(await site.http.get(f"{API}/issue/LAUNCH-3", params={"fields": "priority,assignee,reporter,status"}))
    assert read["fields"]["priority"]["name"] == "Low" and read["fields"]["assignee"]["accountId"] == NOOR
    assert read["fields"]["reporter"]["accountId"] == AGENT and read["fields"]["status"]["name"] == "To Do"


async def test_a_subtask_is_created_under_its_parent(site: Site) -> None:
    made = ok(
        await site.http.post(
            f"{API}/issue",
            json={
                "fields": {
                    "project": {"key": "LAUNCH"},
                    "summary": "Proofread",
                    "issuetype": {"name": "Subtask"},
                    "parent": {"key": "LAUNCH-1"},
                }
            },
        ),
        201,
    )
    parent = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "subtasks"}))
    assert [s["key"] for s in parent["fields"]["subtasks"]] == [made["key"]]
    child = ok(await site.http.get(f"{API}/issue/{made['key']}", params={"fields": "parent"}))
    assert child["fields"]["parent"]["key"] == "LAUNCH-1"


async def test_issue_edit_writes_each_field_the_client_sends(site: Site) -> None:
    for fields in (
        {"summary": "Write the release notes, v2", "description": _doc("Now with screenshots.")},
        {"priority": {"name": "Highest"}, "customfield_10016": 8, "duedate": "2026-08-30"},
        {"assignee": {"accountId": NOOR}},
        {"customfield_10020": None},
        {"labels": ["docs"]},
    ):
        assert (await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": fields})).status_code == 204
    read = ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["fields"]
    assert read["summary"] == "Write the release notes, v2" and read["priority"]["name"] == "Highest"
    assert (read["customfield_10016"], read["duedate"], read["labels"]) == (8, "2026-08-30", ["docs"])
    assert read["assignee"]["accountId"] == NOOR and read["customfield_10020"] is None
    assert read["updated"] == "2026-08-24T10:50:03.000+0000"


async def test_set_and_clear_a_parent(site: Site) -> None:
    epic = ok(
        await site.http.post(
            f"{API}/issue",
            json={"fields": {"project": {"key": "LAUNCH"}, "summary": "Beta", "issuetype": {"name": "Epic"}}},
        ),
        201,
    )
    assert (
        await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"parent": {"key": epic["key"]}}})
    ).status_code == 204
    assert (
        ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "parent"}))["fields"]["parent"]["key"]
        == epic["key"]
    )
    assert (await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"parent": None}})).status_code == 204
    assert "parent" not in ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "parent"}))["fields"]


async def test_issue_delete_with_subtasks_takes_them_too(site: Site) -> None:
    child = ok(
        await site.http.post(
            f"{API}/issue",
            json={"fields": {"project": {"key": "LAUNCH"}, "summary": "Proofread", "issuetype": {"name": "Subtask"},
                             "parent": {"key": "LAUNCH-1"}}},
        ),
        201,
    )  # fmt: skip
    gone = await site.http.delete(f"{API}/issue/LAUNCH-1", params={"deleteSubtasks": "true"})
    assert gone.status_code == 204
    assert (await site.http.get(f"{API}/issue/{child['key']}")).status_code == 404
    venue = ok(await site.http.get(f"{API}/issue/LAUNCH-2", params={"fields": "issuelinks"}))
    assert venue["fields"]["issuelinks"] == [], "the link went with the issue"


async def test_transitions_list_ids_and_destinations_from_the_current_status(site: Site) -> None:
    found = ok(await site.http.get(f"{API}/issue/LAUNCH-1/transitions"))
    assert [(t["id"], t["name"], t["to"]["name"]) for t in found["transitions"]] == [
        ("11", "Start work", "In Progress"),
        ("51", "Drop", "Won't Do"),
    ]
    assert found["transitions"][0]["isGlobal"] is False and found["transitions"][1]["isGlobal"] is True


async def test_transition_do_moves_the_issue(site: Site) -> None:
    moved = await site.http.post(f"{API}/issue/LAUNCH-1/transitions", json={"transition": {"id": "11"}})
    assert moved.status_code == 204
    status = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "status"}))["fields"]["status"]
    assert status["name"] == "In Progress"


async def test_comment_add_keeps_the_document_and_answers_201(site: Site) -> None:
    body = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Assigned to "},
                                          {"type": "mention", "attrs": {"id": NOOR, "text": "@Noor"}}]}]}  # fmt: skip
    written = ok(await site.http.post(f"{API}/issue/LAUNCH-1/comment", json={"body": body}), 201)
    assert written["body"] == body and written["author"]["accountId"] == AGENT
    assert written["created"] == "2026-08-24T10:50:03.000+0000"
    listed = ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment"))
    assert [c["id"] for c in listed["comments"]][-1] == written["id"] and listed["total"] == 2
    event = next(e for e in reversed(site.store.events()) if e.entity.external_id == written["id"])
    assert event.after is not None and event.after.kind == "message"
    assert event.after.text == "Assigned to @Noor"


async def test_user_search_by_query(site: Site) -> None:
    found = ok(await site.http.get(f"{API}/user/search", params={"query": "no", "startAt": "0", "maxResults": "2"}))
    assert [u["accountId"] for u in found] == [NOOR]


async def test_users_search_lists_everyone_with_their_account_type(site: Site) -> None:
    found = ok(await site.http.get(f"{API}/users/search", params={"startAt": "0", "maxResults": "200"}))
    by_name = {u["displayName"]: u for u in found}
    assert len(found) == 8
    assert by_name["Automation for Jira"]["accountType"] == "app"
    assert by_name["Former Colleague"]["active"] is False
    assert by_name["Portal Customer"]["accountType"] == "customer"
    assert "emailAddress" not in by_name["Quiet Person"]


async def test_user_get_by_account_id(site: Site) -> None:
    user = ok(await site.http.get(f"{API}/user", params={"accountId": IRIS}))
    assert (user["displayName"], user["emailAddress"]) == ("Iris Calder", "iris@example.com")


async def test_a_blocks_link_reads_back_as_the_outward_issue_blocking_the_inward_one(site: Site) -> None:
    """Atlassian's reference: the outward issue is the link's "from" end, so `outwardIssue: A, inwardIssue: B`
    with Blocks says A blocks B; on A the link names B as `outwardIssue` (A blocks it), on B it names A as
    `inwardIssue` (B is blocked by it)."""
    made = await site.http.post(
        f"{API}/issueLink",
        json={"type": {"name": "Blocks"}, "outwardIssue": {"key": "FIELD-1"}, "inwardIssue": {"key": "LAUNCH-2"}},
    )
    assert made.status_code == 201
    blocker = ok(await site.http.get(f"{API}/issue/FIELD-1", params={"fields": "issuelinks"}))["fields"]["issuelinks"]
    blocked = ok(await site.http.get(f"{API}/issue/LAUNCH-2", params={"fields": "issuelinks"}))["fields"]["issuelinks"]
    [on_blocker] = blocker
    on_blocked = next(link for link in blocked if link["id"] == on_blocker["id"])
    assert (on_blocker["type"]["outward"], on_blocker["outwardIssue"]["key"]) == ("blocks", "LAUNCH-2")
    assert (on_blocked["type"]["inward"], on_blocked["inwardIssue"]["key"]) == ("is blocked by", "FIELD-1")


async def test_issue_link_create_and_delete(site: Site) -> None:
    made = await site.http.post(
        f"{API}/issueLink",
        json={"type": {"name": "Relates"}, "inwardIssue": {"key": "FIELD-1"}, "outwardIssue": {"key": "LAUNCH-2"}},
    )
    assert made.status_code == 201 and made.content == b""
    links = ok(await site.http.get(f"{API}/issue/FIELD-1", params={"fields": "issuelinks"}))["fields"]["issuelinks"]
    [link] = links
    assert link["inwardIssue"]["key"] == "LAUNCH-2", "FIELD-1 is the inward end, so its entry names the other end"
    read = ok(await site.http.get(f"{API}/issueLink/{link['id']}"))
    assert (read["inwardIssue"]["key"], read["outwardIssue"]["key"]) == ("FIELD-1", "LAUNCH-2")
    assert (await site.http.delete(f"{API}/issueLink/{link['id']}")).status_code == 204
    assert (
        ok(await site.http.get(f"{API}/issue/FIELD-1", params={"fields": "issuelinks"}))["fields"]["issuelinks"] == []
    )
    types = ok(await site.http.get(f"{API}/issueLinkType"))["issueLinkTypes"]
    assert [t["name"] for t in types] == ["Blocks", "Cloners", "Duplicate", "Relates"]


async def test_boards_sprints_and_moving_an_issue_into_one(site: Site) -> None:
    boards = ok(await site.http.get(f"{AGILE}/board", params={"projectKeyOrId": "LAUNCH", "maxResults": "50"}))
    [board] = boards["values"]
    assert (board["name"], board["location"]["projectKey"]) == ("Launch board", "LAUNCH")
    sprints = ok(
        await site.http.get(f"{AGILE}/board/{board['id']}/sprint", params={"startAt": "0", "maxResults": "50"})
    )
    assert [(s["name"], s["state"]) for s in sprints["values"]] == [
        ("Launch 1", "closed"),
        ("Launch 2", "active"),
        ("Launch 3", "future"),
    ]
    future = sprints["values"][2]["id"]
    assert (await site.http.post(f"{AGILE}/sprint/{future}/issue", json={"issues": ["LAUNCH-1"]})).status_code == 204
    held = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "customfield_10020"}))
    assert [s["name"] for s in held["fields"]["customfield_10020"]] == ["Launch 3"]


async def test_field_lists_system_and_custom_fields_with_schemas(site: Site) -> None:
    found = ok(await site.http.get(f"{API}/field"))
    by_name = {f["name"].lower(): f for f in found}
    assert by_name["story point estimate"]["id"] == "customfield_10016"
    assert by_name["sprint"]["schema"]["custom"] == "com.pyxis.greenhopper.jira:gh-sprint"
    assert by_name["epic link"]["id"] == "customfield_10014"
    assert by_name["team"]["schema"] == {
        "type": "option",
        "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select",
        "customId": 10050,
    }
    assert by_name["summary"]["custom"] is False


async def test_status_priority_issuetype_resolution_and_categories(site: Site) -> None:
    statuses = ok(await site.http.get(f"{API}/status"))
    assert [s["name"] for s in statuses] == ["To Do", "In Progress", "Done", "Won't Do", "In Review"]
    priorities = ok(await site.http.get(f"{API}/priority"))
    assert [(p["id"], p["name"]) for p in priorities] == [
        ("1", "Highest"),
        ("2", "High"),
        ("3", "Medium"),
        ("4", "Low"),
        ("5", "Lowest"),
    ]
    types = ok(await site.http.get(f"{API}/issuetype"))
    assert [(t["name"], t["hierarchyLevel"]) for t in types][-1] == ("Subtask", -1)
    assert [r["name"] for r in ok(await site.http.get(f"{API}/resolution"))] == ["Done", "Won't Do", "Duplicate"]
    categories = ok(await site.http.get(f"{API}/statuscategory"))
    assert [(c["id"], c["key"]) for c in categories] == [(2, "new"), (4, "indeterminate"), (3, "done")]


async def test_createmeta_lists_issue_types_then_each_types_fields(site: Site) -> None:
    types = ok(
        await site.http.get(f"{API}/issue/createmeta/LAUNCH/issuetypes", params={"startAt": "0", "maxResults": "50"})
    )
    assert types["total"] == 5 and types["issueTypes"][0]["name"] == "Epic"
    subtask = next(t for t in types["issueTypes"] if t["name"] == "Subtask")
    fields = ok(await site.http.get(f"{API}/issue/createmeta/LAUNCH/issuetypes/{subtask['id']}"))
    by_id = {f["fieldId"]: f for f in fields["fields"]}
    assert by_id["parent"]["required"] is True and by_id["summary"]["required"] is True
    assert by_id["duedate"]["required"] is False and by_id["priority"]["name"] == "Priority"
    assert [v["value"] for v in by_id["customfield_10050"]["allowedValues"]] == ["Platform", "Field"]
    assert fields["total"] == len(fields["fields"])


async def test_assignable_search_lists_who_can_take_the_projects_issues(site: Site) -> None:
    found = {
        prefix: [u["displayName"] for u in ok(await site.http.get(
            f"{API}/user/assignable/search", params={"project": "LAUNCH", "query": prefix}))]
        for prefix in ("i", "lan", "quiet", "former", "automation")
    }  # fmt: skip
    assert found == {"i": ["Iris Calder"], "lan": ["Lantern Agent"], "quiet": ["Quiet Person"], "former": [],
                     "automation": []}, "a member is assignable; a deactivated account and an app are not"  # fmt: skip


async def test_project_roles_and_adding_a_member(site: Site) -> None:
    roles = ok(await site.http.get(f"{API}/project/LAUNCH/role"))
    assert set(roles) == {"Administrators", "Member", "Viewer"}
    member = roles["Member"].rsplit("/", 1)[1]
    added = ok(await site.http.post(f"{API}/project/LAUNCH/role/{member}", json={"user": [NOOR]}))
    assert added["name"] == "Member" and NOOR in [a["actorUser"]["accountId"] for a in added["actors"]]


async def test_server_info_names_a_cloud_site(site: Site) -> None:
    info = ok(await site.http.get(f"{API}/serverInfo"))
    assert (info["baseUrl"], info["deploymentType"]) == (SITE, "Cloud")
    assert info["serverTime"] == "2026-08-24T10:50:03.000+0000"


async def test_assignee_endpoint_assigns_and_unassigns(site: Site) -> None:
    assert (await site.http.put(f"{API}/issue/LAUNCH-1/assignee", json={"accountId": IRIS})).status_code == 204
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["fields"]["assignee"]["accountId"] == IRIS
    assert (await site.http.put(f"{API}/issue/LAUNCH-1/assignee", json={"accountId": None})).status_code == 204
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["fields"]["assignee"] is None


async def test_changelog_reads_seeded_history_then_each_change_with_its_author(site: Site) -> None:
    await site.http.post(f"{API}/issue/LAUNCH-1/transitions", json={"transition": {"id": "11"}})
    log = ok(await site.http.get(f"{API}/issue/LAUNCH-1/changelog"))
    assert log["total"] == 2 and log["isLast"] is True
    seeded, moved = log["values"]
    assert seeded["author"]["accountId"] == IRIS and seeded["items"][0]["toString"] == "High"
    assert moved["author"]["accountId"] == AGENT
    assert moved["items"] == [
        {
            "field": "status",
            "fieldtype": "jira",
            "fieldId": "status",
            "from": "10000",
            "fromString": "To Do",
            "to": "10001",
            "toString": "In Progress",
        }
    ]
    expanded = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"expand": "changelog", "fields": "summary"}))
    assert expanded["changelog"]["total"] == 2
