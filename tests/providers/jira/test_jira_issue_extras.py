"""Comments changed and removed, watchers, remote links, edit metadata, and project components and versions, through
the proxy, each as the reference for it describes."""

from __future__ import annotations

from typing import Any

from tests.providers.jira.jira_site import AGENT, API, IRIS, IRIS_TOKEN, NOOR, Site, basic, ok, refused


def _doc(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


# --------------------------------------------------------------------------- comments


async def _comment(site: Site, issue: str = "LAUNCH-1", text: str = "first") -> dict[str, Any]:
    return ok(await site.http.post(f"{API}/issue/{issue}/comment", json={"body": _doc(text)}), 201)


async def test_a_comment_is_read_by_id(site: Site) -> None:
    made = await _comment(site)
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment/{made['id']}")) == made


async def test_a_comment_update_replaces_the_body_and_stamps_who_and_when(site: Site) -> None:
    made = await _comment(site)
    site.clock.jump(site.clock.now().replace(hour=12))
    async with site.client(basic("iris@example.com", IRIS_TOKEN)) as iris:
        changed = ok(await iris.put(f"{API}/issue/LAUNCH-1/comment/{made['id']}", json={"body": _doc("second")}))
    assert changed["body"] == _doc("second") and changed["id"] == made["id"]
    assert changed["author"]["accountId"] == AGENT and changed["updateAuthor"]["accountId"] == IRIS
    assert changed["created"] == made["created"] and changed["updated"] == "2026-08-24T12:50:03.000+0000"
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment/{made['id']}")) == changed
    listed = ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment"))
    assert [c["body"] for c in listed["comments"]][-1] == _doc("second") and listed["total"] == 2


async def test_a_comment_update_with_a_body_that_is_not_a_document_is_refused_400_and_changes_nothing(
    site: Site,
) -> None:
    made = await _comment(site)
    answer = await site.http.put(f"{API}/issue/LAUNCH-1/comment/{made['id']}", json={"body": "plain"})
    assert refused(answer, 400)["errors"] == {"comment": "Comment body is not valid!"}
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment/{made['id']}"))["body"] == _doc("first")


async def test_a_comment_update_with_no_body_is_refused_501(site: Site) -> None:
    made = await _comment(site)
    answer = await site.http.put(f"{API}/issue/LAUNCH-1/comment/{made['id']}", json={})
    assert "a comment update with no body" in refused(answer, 501)["errorMessages"][0]


async def test_a_comment_update_naming_visibility_or_expand_is_refused_501(site: Site) -> None:
    made = await _comment(site)
    url = f"{API}/issue/LAUNCH-1/comment/{made['id']}"
    visible = await site.http.put(url, json={"body": _doc("x"), "visibility": {"type": "role", "value": "Member"}})
    assert "'visibility' property" in refused(visible, 501)["errorMessages"][0]
    expanded = await site.http.get(url, params={"expand": "renderedBody"})
    assert "'expand' parameter of GET" in refused(expanded, 501)["errorMessages"][0]


async def test_deleting_a_comment_removes_it_and_a_second_delete_is_404(site: Site) -> None:
    made = await _comment(site)
    url = f"{API}/issue/LAUNCH-1/comment/{made['id']}"
    ok(await site.http.delete(url), 204)
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment"))["total"] == 1, "the seeded comment is left"
    gone = await site.http.delete(url)
    assert "Returned if the issue or comment is not found" in refused(gone, 404)["errorMessages"][0]


async def test_a_comment_of_another_issue_is_404_under_this_one(site: Site) -> None:
    made = await _comment(site, issue="LAUNCH-1")
    assert (await site.http.get(f"{API}/issue/LAUNCH-2/comment/{made['id']}")).status_code == 404
    assert (
        await site.http.put(f"{API}/issue/LAUNCH-2/comment/{made['id']}", json={"body": _doc("x")})
    ).status_code == 404


# --------------------------------------------------------------------------- watchers


async def test_adding_a_watcher_by_account_id_lists_them_and_counts_them(site: Site) -> None:
    ok(await site.http.post(f"{API}/issue/LAUNCH-1/watchers", json=NOOR), 204)
    watchers = ok(await site.http.get(f"{API}/issue/LAUNCH-1/watchers"))
    assert watchers["watchCount"] == 1 and watchers["isWatching"] is False
    assert [w["accountId"] for w in watchers["watchers"]] == [NOOR]
    assert watchers["self"] == f"{API}/issue/LAUNCH-1/watchers"
    issue = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "watches"}))
    assert issue["fields"]["watches"] == {
        "self": f"{API}/issue/LAUNCH-1/watchers",
        "watchCount": 1,
        "isWatching": False,
    }


async def test_adding_a_watcher_with_no_body_adds_the_caller(site: Site) -> None:
    ok(await site.http.post(f"{API}/issue/LAUNCH-1/watchers"), 204)
    watchers = ok(await site.http.get(f"{API}/issue/LAUNCH-1/watchers"))
    assert watchers["isWatching"] is True and [w["accountId"] for w in watchers["watchers"]] == [AGENT]


async def test_removing_a_watcher_by_account_id(site: Site) -> None:
    ok(await site.http.post(f"{API}/issue/LAUNCH-1/watchers", json=NOOR), 204)
    ok(await site.http.delete(f"{API}/issue/LAUNCH-1/watchers", params={"accountId": NOOR}), 204)
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1/watchers"))["watchCount"] == 0


async def test_removing_a_watcher_without_an_account_id_is_refused_400(site: Site) -> None:
    answer = await site.http.delete(f"{API}/issue/LAUNCH-1/watchers")
    assert refused(answer, 400)["errorMessages"] == ["Returned if `accountId` is not supplied."]


async def test_watching_as_an_account_that_is_not_there_is_404_and_a_body_that_is_not_a_string_is_400(
    site: Site,
) -> None:
    unknown = await site.http.post(f"{API}/issue/LAUNCH-1/watchers", json="not-an-account")
    assert "the issue or the user is not found" in refused(unknown, 404)["errorMessages"][0]
    wrong = await site.http.post(f"{API}/issue/LAUNCH-1/watchers", json={"accountId": NOOR})
    assert refused(wrong, 400)["errorMessages"] == ["Returned if the request is invalid."]


async def test_adding_a_watcher_twice_and_removing_a_stranger_are_refused_501(site: Site) -> None:
    ok(await site.http.post(f"{API}/issue/LAUNCH-1/watchers", json=NOOR), 204)
    twice = await site.http.post(f"{API}/issue/LAUNCH-1/watchers", json=NOOR)
    assert "already watches" in refused(twice, 501)["errorMessages"][0]
    stranger = await site.http.delete(f"{API}/issue/LAUNCH-1/watchers", params={"accountId": IRIS})
    assert "does not watch" in refused(stranger, 501)["errorMessages"][0]


async def test_the_username_parameter_of_the_watcher_delete_is_refused_501_by_name(site: Site) -> None:
    answer = await site.http.delete(f"{API}/issue/LAUNCH-1/watchers", params={"username": "noor"})
    assert "'username' parameter" in refused(answer, 501)["errorMessages"][0]


# --------------------------------------------------------------------------- remote links

REMOTE = {
    "globalId": "system=http://tracker.example/support&id=1",
    "application": {"type": "com.example.tracker", "name": "Tracker"},
    "relationship": "causes",
    "object": {
        "url": "http://tracker.example/support?id=1",
        "title": "TSTSUP-111",
        "summary": "Customer support issue",
        "icon": {"url16x16": "http://tracker.example/icon.png", "title": "Support Ticket"},
        "status": {"resolved": True, "icon": {"url16x16": "http://tracker.example/ok.png", "title": "Case Closed"}},
    },
}
LINKS = f"{API}/issue/LAUNCH-1/remotelink"


async def test_a_remote_link_is_created_and_read_back_exactly_as_sent(site: Site) -> None:
    made = ok(await site.http.post(LINKS, json=REMOTE), 201)
    assert set(made) == {"id", "self"} and made["self"] == f"{LINKS.replace('LAUNCH-1', '1000')}/{made['id']}"
    read = ok(await site.http.get(f"{LINKS}/{made['id']}"))
    assert read == {"id": made["id"], "self": made["self"]} | REMOTE
    assert ok(await site.http.get(LINKS)) == [read]


async def test_a_remote_link_is_found_by_its_global_id(site: Site) -> None:
    ok(await site.http.post(LINKS, json=REMOTE), 201)
    other = REMOTE | {"globalId": "other"}
    ok(await site.http.post(LINKS, json=other), 201)
    one = ok(await site.http.get(LINKS, params={"globalId": other["globalId"]}))
    assert one["globalId"] == "other" and isinstance(one["id"], int)
    assert len(ok(await site.http.get(LINKS))) == 2
    assert (await site.http.get(LINKS, params={"globalId": "none"})).status_code == 404


async def test_posting_a_global_id_a_link_has_updates_it_and_nulls_what_is_left_out(site: Site) -> None:
    made = ok(await site.http.post(LINKS, json=REMOTE), 201)
    updated = ok(
        await site.http.post(LINKS, json={"globalId": REMOTE["globalId"], "object": {"title": "T", "url": "http://x"}})
    )
    assert updated["id"] == made["id"]
    [link] = ok(await site.http.get(LINKS))
    assert (
        link["object"] == {"title": "T", "url": "http://x"} and "relationship" not in link and "application" not in link
    )


async def test_a_remote_link_put_replaces_and_204(site: Site) -> None:
    made = ok(await site.http.post(LINKS, json=REMOTE), 201)
    ok(await site.http.put(f"{LINKS}/{made['id']}", json={"object": {"title": "New", "url": "http://new"}}), 204)
    read = ok(await site.http.get(f"{LINKS}/{made['id']}"))
    assert read["object"]["title"] == "New" and "globalId" not in read


async def test_remote_links_are_deleted_by_id_and_by_global_id(site: Site) -> None:
    first = ok(await site.http.post(LINKS, json=REMOTE), 201)
    ok(await site.http.post(LINKS, json=REMOTE | {"globalId": "second"}), 201)
    ok(await site.http.delete(f"{LINKS}/{first['id']}"), 204)
    ok(await site.http.delete(LINKS, params={"globalId": "second"}), 204)
    assert ok(await site.http.get(LINKS)) == []
    assert (await site.http.delete(LINKS, params={"globalId": "second"})).status_code == 404


async def test_deleting_by_global_id_without_one_is_refused_400(site: Site) -> None:
    assert refused(await site.http.delete(LINKS), 400)["errorMessages"] == ["Returned if a global ID isn't provided."]


async def test_a_remote_link_without_an_object_title_or_url_is_refused_400(site: Site) -> None:
    for body in (
        {},
        {"object": {"title": "t"}},
        {"object": {"url": "u"}},
        {"object": "x"},
        {"globalId": "g" * 256, "object": REMOTE["object"]},
    ):
        answer = await site.http.post(LINKS, json=body)
        assert answer.status_code == 400, body
    assert ok(await site.http.get(LINKS)) == []


async def test_a_remote_link_of_another_issue_is_400_and_one_that_is_not_there_is_404(site: Site) -> None:
    made = ok(await site.http.post(LINKS, json=REMOTE), 201)
    other = await site.http.get(f"{API}/issue/LAUNCH-2/remotelink/{made['id']}")
    assert refused(other, 400)["errorMessages"] == ["the remote issue link does not belong to the issue."]
    assert (await site.http.get(f"{LINKS}/99999")).status_code == 404
    assert refused(await site.http.get(f"{LINKS}/abc"), 400)["errorMessages"] == ["the link ID is invalid."]


# --------------------------------------------------------------------------- edit metadata


async def test_edit_metadata_lists_the_screens_fields_an_edit_can_set(site: Site) -> None:
    meta = ok(await site.http.get(f"{API}/issue/LAUNCH-1/editmeta"))
    fields = meta["fields"]
    assert "summary" in fields and "labels" in fields
    assert "project" not in fields and "issuetype" not in fields
    assert fields["labels"]["operations"] == ["set", "add", "remove"] and fields["summary"]["key"] == "summary"
    assert fields["priority"]["allowedValues"][0]["name"] == "Highest"


async def test_edit_metadata_overrides_are_refused_501_when_true(site: Site) -> None:
    answer = await site.http.get(f"{API}/issue/LAUNCH-1/editmeta", params={"overrideScreenSecurity": "true"})
    assert "overrideScreenSecurity and overrideEditableFlag" in refused(answer, 501)["errorMessages"][0]


# --------------------------------------------------------------------------- components

COMPONENTS = f"{API}/component"


async def test_a_component_is_created_with_the_assignee_its_type_names(site: Site) -> None:
    made = ok(
        await site.http.post(
            COMPONENTS,
            json={
                "name": "Backend",
                "project": "LAUNCH",
                "description": "Services",
                "leadAccountId": NOOR,
                "assigneeType": "COMPONENT_LEAD",
            },
        ),
        201,
    )
    assert made["name"] == "Backend" and made["description"] == "Services" and made["project"] == "LAUNCH"
    assert made["projectId"] == 100 and made["self"] == f"{COMPONENTS}/{made['id']}"
    assert made["lead"]["accountId"] == NOOR and "leadAccountId" not in made
    assert made["assigneeType"] == made["realAssigneeType"] == "COMPONENT_LEAD"
    assert made["assignee"]["accountId"] == made["realAssignee"]["accountId"] == NOOR
    assert made["isAssigneeTypeValid"] is True
    assert ok(await site.http.get(f"{COMPONENTS}/{made['id']}")) == made


async def test_a_component_defaults_to_the_project_default_assignee(site: Site) -> None:
    made = ok(await site.http.post(COMPONENTS, json={"name": "Docs", "project": "LAUNCH"}), 201)
    assert made["assigneeType"] == made["realAssigneeType"] == "PROJECT_DEFAULT"
    assert "assignee" not in made and "lead" not in made and made["isAssigneeTypeValid"] is True


async def test_a_component_led_by_nobody_falls_back_and_says_its_type_is_not_valid(site: Site) -> None:
    made = ok(
        await site.http.post(COMPONENTS, json={"name": "Docs", "project": "LAUNCH", "assigneeType": "COMPONENT_LEAD"}),
        201,
    )
    assert made["assigneeType"] == "COMPONENT_LEAD" and made["realAssigneeType"] == "PROJECT_DEFAULT"
    assert made["isAssigneeTypeValid"] is False and "assignee" not in made


async def test_a_component_set_to_the_project_lead_is_assigned_to_them(site: Site) -> None:
    made = ok(
        await site.http.post(COMPONENTS, json={"name": "Docs", "project": "LAUNCH", "assigneeType": "PROJECT_LEAD"}),
        201,
    )
    assert made["realAssignee"]["accountId"] == IRIS and made["assignee"]["accountId"] == IRIS


async def test_a_component_create_missing_what_is_required_or_wrong_is_refused_400(site: Site) -> None:
    cases = {
        "`projectId` is not provided.": {"name": "x"},
        "`name` is not provided.": {"project": "LAUNCH"},
        "`name` is over 255 characters in length.": {"name": "x" * 256, "project": "LAUNCH"},
        "`assigneeType` is an invalid value.": {"name": "x", "project": "LAUNCH", "assigneeType": "EVERYONE"},
        "the user is not found.": {"name": "x", "project": "LAUNCH", "leadAccountId": "nobody"},
    }
    for message, body in cases.items():
        assert refused(await site.http.post(COMPONENTS, json=body), 400)["errorMessages"] == [message]
    assert ok(await site.http.get(f"{API}/project/LAUNCH/components")) == []


async def test_a_component_in_a_project_that_is_not_there_is_404_and_one_by_id_that_is_not_there_is_404(
    site: Site,
) -> None:
    answer = await site.http.post(COMPONENTS, json={"name": "x", "project": "NOPE"})
    assert "Returned if the project is not found" in refused(answer, 404)["errorMessages"][0]
    assert (await site.http.get(f"{COMPONENTS}/99999")).status_code == 404


async def test_a_component_name_a_project_holds_already_and_the_unlisted_properties_are_refused_501(site: Site) -> None:
    ok(await site.http.post(COMPONENTS, json={"name": "Docs", "project": "LAUNCH"}), 201)
    twice = await site.http.post(COMPONENTS, json={"name": "Docs", "project": "LAUNCH"})
    assert "a component whose name another component" in refused(twice, 501)["errorMessages"][0]
    named = await site.http.post(COMPONENTS, json={"name": "Other", "project": "LAUNCH", "leadUserName": "noor"})
    assert "'leadUserName' property" in refused(named, 501)["errorMessages"][0]


async def test_a_property_the_closed_component_schema_has_not_got_is_refused_400(site: Site) -> None:
    answer = await site.http.post(COMPONENTS, json={"name": "x", "project": "LAUNCH", "colour": "red"})
    assert answer.status_code == 400


async def test_components_list_whole_and_paged_and_appear_on_the_project(site: Site) -> None:
    for name in ("Beta", "Alpha", "Gamma"):
        ok(
            await site.http.post(
                COMPONENTS, json={"name": name, "project": "LAUNCH", "description": f"about {name.lower()}"}
            ),
            201,
        )
    whole = ok(await site.http.get(f"{API}/project/LAUNCH/components"))
    assert [c["name"] for c in whole] == ["Beta", "Alpha", "Gamma"]
    page = ok(await site.http.get(f"{API}/project/LAUNCH/component", params={"maxResults": "2", "orderBy": "name"}))
    assert [c["name"] for c in page["values"]] == ["Alpha", "Beta"]
    assert (page["total"], page["isLast"], page["startAt"]) == (3, False, 0) and "startAt=2" in page["nextPage"]
    last = ok(
        await site.http.get(
            f"{API}/project/LAUNCH/component", params={"maxResults": "2", "startAt": "2", "orderBy": "-name"}
        )
    )
    assert [c["name"] for c in last["values"]] == ["Alpha"] and last["isLast"] is True and "nextPage" not in last
    found = ok(await site.http.get(f"{API}/project/LAUNCH/component", params={"query": "ABOUT g"}))
    assert [c["name"] for c in found["values"]] == ["Gamma"]
    project = ok(await site.http.get(f"{API}/project/LAUNCH"))
    assert [c["name"] for c in project["components"]] == ["Beta", "Alpha", "Gamma"]
    assert ok(await site.http.get(f"{API}/project/FIELD/components")) == []


async def test_component_order_by_other_than_the_four_and_compass_are_refused(site: Site) -> None:
    bad = await site.http.get(f"{API}/project/LAUNCH/component", params={"orderBy": "colour"})
    assert "should be one of [description, issueCount, lead, name]" in refused(bad, 400)["errorMessages"][0]
    compass = await site.http.get(f"{API}/project/LAUNCH/components", params={"componentSource": "compass"})
    assert "componentSource=compass" in refused(compass, 501)["errorMessages"][0]


# --------------------------------------------------------------------------- versions

VERSIONS = f"{API}/version"


async def test_a_version_is_created_and_read_back_as_sent(site: Site) -> None:
    made = ok(
        await site.http.post(
            VERSIONS,
            json={
                "name": "1.0",
                "projectId": 100,
                "description": "First",
                "startDate": "2026-09-01",
                "releaseDate": "2026-10-01",
            },
        ),
        201,
    )
    assert made == {
        "self": f"{VERSIONS}/{made['id']}",
        "id": made["id"],
        "name": "1.0",
        "description": "First",
        "archived": False,
        "released": False,
        "startDate": "2026-09-01",
        "releaseDate": "2026-10-01",
        "projectId": 100,
    }
    assert ok(await site.http.get(f"{VERSIONS}/{made['id']}")) == made


async def test_a_version_create_that_is_not_valid_or_names_no_project_is_refused(site: Site) -> None:
    for body in (
        {"projectId": 100},
        {"name": "x"},
        {"name": "x" * 256, "projectId": 100},
        {"name": "x", "projectId": 100, "releaseDate": "soon"},
    ):
        assert refused(await site.http.post(VERSIONS, json=body), 400)["errorMessages"] == [
            "Returned if the request is invalid."
        ]
    missing = await site.http.post(VERSIONS, json={"name": "x", "projectId": 9999})
    assert refused(missing, 404)["errorMessages"] == ["the project is not found."]
    assert ok(await site.http.get(f"{API}/project/LAUNCH/versions")) == []


async def test_a_version_created_released_a_repeated_name_and_the_unlisted_properties_are_refused_501(
    site: Site,
) -> None:
    released = await site.http.post(VERSIONS, json={"name": "x", "projectId": 100, "released": True})
    assert "already released" in refused(released, 501)["errorMessages"][0]
    ok(await site.http.post(VERSIONS, json={"name": "1.0", "projectId": 100}), 201)
    twice = await site.http.post(VERSIONS, json={"name": "1.0", "projectId": 100})
    assert "a version whose name another version" in refused(twice, 501)["errorMessages"][0]
    driven = await site.http.post(VERSIONS, json={"name": "2.0", "projectId": 100, "driver": IRIS})
    assert "'driver' property" in refused(driven, 501)["errorMessages"][0]


async def test_a_version_that_is_not_there_or_read_with_an_expansion_is_404_or_501(site: Site) -> None:
    assert (
        "Returned if the version is not found"
        in refused(await site.http.get(f"{VERSIONS}/99999"), 404)["errorMessages"][0]
    )
    made = ok(await site.http.post(VERSIONS, json={"name": "1.0", "projectId": 100}), 201)
    expanded = await site.http.get(f"{VERSIONS}/{made['id']}", params={"expand": "operations"})
    assert "'expand' parameter" in refused(expanded, 501)["errorMessages"][0]


async def test_versions_list_whole_and_paged_ordered_and_filtered(site: Site) -> None:
    sent = [
        {"name": "b-beta", "startDate": "2026-09-05", "releaseDate": "2026-10-20", "archived": True},
        {"name": "a-alpha", "startDate": "2026-09-01"},
        {"name": "c-final", "releaseDate": "2026-10-01", "description": "the Final one"},
    ]
    made = [ok(await site.http.post(VERSIONS, json=v | {"projectId": 100}), 201) for v in sent]
    whole = ok(await site.http.get(f"{API}/project/LAUNCH/versions"))
    assert [v["name"] for v in whole] == ["b-beta", "a-alpha", "c-final"]
    names = lambda page: [v["name"] for v in page["values"]]  # noqa: E731
    url = f"{API}/project/LAUNCH/version"
    assert names(ok(await site.http.get(url, params={"orderBy": "name"}))) == ["a-alpha", "b-beta", "c-final"]
    assert names(ok(await site.http.get(url, params={"orderBy": "-sequence"}))) == ["c-final", "a-alpha", "b-beta"]
    assert names(ok(await site.http.get(url, params={"orderBy": "releaseDate"}))) == ["c-final", "b-beta", "a-alpha"]
    assert names(ok(await site.http.get(url, params={"orderBy": "-releaseDate"}))) == ["b-beta", "c-final", "a-alpha"]
    assert names(ok(await site.http.get(url, params={"orderBy": "startDate"}))) == ["a-alpha", "b-beta", "c-final"]
    assert names(ok(await site.http.get(url, params={"orderBy": "description"}))) == ["b-beta", "a-alpha", "c-final"]
    assert names(ok(await site.http.get(url, params={"status": "archived"}))) == ["b-beta"]
    assert names(ok(await site.http.get(url, params={"status": "unreleased"}))) == ["b-beta", "a-alpha", "c-final"]
    assert names(ok(await site.http.get(url, params={"query": "FINAL"}))) == ["c-final"]
    page = ok(await site.http.get(url, params={"maxResults": "2"}))
    assert (page["total"], page["isLast"]) == (3, False) and "startAt=2" in page["nextPage"]
    assert [v["id"] for v in ok(await site.http.get(f"{API}/project/LAUNCH"))["versions"]] == [m["id"] for m in made]


async def test_version_listing_refuses_an_unknown_status_and_order_and_an_expansion(site: Site) -> None:
    url = f"{API}/project/LAUNCH/version"
    assert (await site.http.get(url, params={"status": "retired"})).status_code == 400
    assert (await site.http.get(url, params={"orderBy": "colour"})).status_code == 400
    assert (
        "'expand' parameter"
        in refused(await site.http.get(url, params={"expand": "operations"}), 501)["errorMessages"][0]
    )
    assert (
        "'expand' parameter"
        in refused(await site.http.get(f"{API}/project/LAUNCH/versions", params={"expand": "operations"}), 501)[
            "errorMessages"
        ][0]
    )
