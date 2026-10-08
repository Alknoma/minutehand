"""Behaviour held to Jira Cloud's REST reference (platform v3 and Agile 1.0, as published on 2026-10-08), one
test per behaviour that an earlier version of this fake chose for itself instead. Each docstring cites the
operation's reference page."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from tests.providers.jira.jira_site import AGENT, AGILE, API, IRIS, START, Site, basic, ok, refused


def _doc(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


# --------------------------------------------------------------------------- comments


async def test_comments_come_100_a_page_unless_max_results_says_otherwise(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/#api-rest-api-3-issue-issueidorkey-comment-get — `maxResults` defaults
    to 100."""
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment"))["maxResults"] == 100


async def test_comments_ordered_by_anything_but_created_are_refused_400(site: Site) -> None:
    """Same page: a 400 when `orderBy` is set to a value other than `created`; `-created` is newest first."""
    refused(await site.http.get(f"{API}/issue/LAUNCH-1/comment", params={"orderBy": "author"}), 400)
    ok(await site.http.post(f"{API}/issue/LAUNCH-1/comment", json={"body": _doc("Later")}), 201)
    newest = ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment", params={"orderBy": "-created"}))
    assert newest["comments"][0]["body"] == _doc("Later")


# --------------------------------------------------------------------------- users


async def test_a_user_search_with_both_query_and_account_id_is_refused_400(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-user-search/#api-rest-api-3-user-search-get — a 400 when `query` and `accountId` are
    both provided."""
    refused(await site.http.get(f"{API}/user/search", params={"query": "iris", "accountId": IRIS}), 400)


async def test_an_assignable_search_with_neither_query_nor_account_id_is_refused_400(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-user-search/#api-rest-api-3-user-assignable-search-get — a 400 when `query` or `accountId`
    is missing; `accountId` alone finds that account."""
    refused(await site.http.get(f"{API}/user/assignable/search", params={"project": "LAUNCH"}), 400)
    found = ok(await site.http.get(f"{API}/user/assignable/search", params={"project": "LAUNCH", "accountId": IRIS}))
    assert [u["accountId"] for u in found] == [IRIS]


# --------------------------------------------------------------------------- projects


async def test_project_search_lists_without_description_or_lead_until_expanded(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-search-get — description, lead and issue types are
    expansions here, unlike `GET /project/{key}`, where they are always included."""
    plain = ok(await site.http.get(f"{API}/project/search", params={"keys": "LAUNCH"}))["values"][0]
    assert {"description", "lead", "issueTypes"} & set(plain) == set()
    expanded = ok(await site.http.get(f"{API}/project/search", params={"keys": "LAUNCH", "expand": "lead,issueTypes"}))
    assert expanded["values"][0]["lead"]["accountId"] == IRIS and expanded["values"][0]["issueTypes"]
    assert {"description", "lead", "issueTypes"} <= set(ok(await site.http.get(f"{API}/project/LAUNCH")))


async def test_project_search_orders_filters_and_links_the_next_page(site: Site) -> None:
    """Same page: ordered by `key` by default, `orderBy=-name` reverses by name, `keys` and `id` filter, and a
    page that is not the last links the next."""
    by_name = ok(await site.http.get(f"{API}/project/search", params={"orderBy": "-name"}))
    assert [p["name"] for p in by_name["values"]] == ["Launch", "Field Ops"]
    first = ok(await site.http.get(f"{API}/project/search", params={"maxResults": "1"}))
    assert first["nextPage"].endswith("/rest/api/3/project/search?startAt=1&maxResults=1")
    launch = first["values"][0] if first["values"][0]["key"] == "LAUNCH" else None
    by_id = ok(await site.http.get(f"{API}/project/search", params={"id": by_name["values"][0]["id"]}))
    assert [p["key"] for p in by_id["values"]] == ["LAUNCH"]
    assert launch is None, "FIELD sorts before LAUNCH"


async def test_project_search_ordered_by_something_it_cannot_order_by_is_refused_501(site: Site) -> None:
    refused(await site.http.get(f"{API}/project/search", params={"orderBy": "issueCount"}), 501)


async def test_a_created_projects_default_assignee_is_kept_as_sent(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post — `assigneeType` is `PROJECT_LEAD` or
    `UNASSIGNED`; what is sent is what the project reads back."""
    body = {"key": "KEPT", "name": "Kept", "projectTypeKey": "software", "leadAccountId": AGENT}
    ok(await site.http.post(f"{API}/project", json=body | {"assigneeType": "PROJECT_LEAD"}), 201)
    assert ok(await site.http.get(f"{API}/project/KEPT"))["assigneeType"] == "PROJECT_LEAD"
    bad = refused(await site.http.post(f"{API}/project", json=body | {"key": "BAD", "name": "Bad",
                                                                       "assigneeType": "WHOEVER"}), 400)  # fmt: skip
    assert set(bad["errors"]) == {"assigneeType"}


async def test_a_project_property_the_fake_does_not_keep_is_refused_501_and_creates_nothing(site: Site) -> None:
    body = {"key": "CAT", "name": "Cat", "projectTypeKey": "software", "leadAccountId": AGENT, "categoryId": 10000}
    refused(await site.http.post(f"{API}/project", json=body), 501)
    refused(await site.http.get(f"{API}/project/CAT"), 404)


async def test_a_role_describes_itself_with_the_worlds_words_not_the_fakes(site: Site) -> None:
    roles = ok(await site.http.get(f"{API}/project/LAUNCH/role"))
    role = ok(await site.http.get(roles["Member"]))
    assert role["description"] == ""


async def test_a_role_read_with_exclude_inactive_users_leaves_them_out(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-roles/#api-rest-api-3-project-projectidorkey-role-id-get — `excludeInactiveUsers`."""
    roles = ok(await site.http.get(f"{API}/project/LAUNCH/role"))
    member = roles["Member"].rsplit("/", 1)[1]
    former = next(u for u in site.jira.users() if u.displayName == "Former Colleague")
    ok(await site.http.post(f"{API}/project/LAUNCH/role/{member}", json={"user": [former.accountId]}))
    everyone = ok(await site.http.get(f"{API}/project/LAUNCH/role/{member}"))
    active = ok(await site.http.get(f"{API}/project/LAUNCH/role/{member}", params={"excludeInactiveUsers": "true"}))
    assert former.accountId in [a["actorUser"]["accountId"] for a in everyone["actors"]]
    assert former.accountId not in [a["actorUser"]["accountId"] for a in active["actors"]]


async def test_a_group_added_to_a_role_is_refused_501_naming_it(site: Site) -> None:
    roles = ok(await site.http.get(f"{API}/project/LAUNCH/role"))
    body = refused(await site.http.post(roles["Member"], json={"groupId": ["g-1"]}), 501)
    assert "'groupId' property" in body["errorMessages"][0]


# --------------------------------------------------------------------------- search


async def test_search_fields_of_exclusions_only_start_from_the_navigable_fields(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/#api-rest-api-3-search-jql-get — `-description` returns all navigable
    fields except description."""
    found = ok(await site.http.get(f"{API}/search/jql", params={"jql": "key = LAUNCH-1", "fields": "-description"}))
    fields = found["issues"][0]["fields"]
    assert "description" not in fields and {"summary", "status", "assignee"} <= set(fields)


async def test_search_fields_given_more_than_once_are_all_answered(site: Site) -> None:
    """Same page: multiple `fields` parameters can be included in a request."""
    answer = await site.http.get(f"{API}/search/jql?jql=key%20%3D%20LAUNCH-1&fields=summary&fields=status")
    assert set(ok(answer)["issues"][0]["fields"]) == {"summary", "status"}


async def test_search_names_are_the_results_not_each_issues(site: Site) -> None:
    """Same page (`SearchAndReconcileResults`): `names` is a property of the results."""
    found = ok(await site.http.post(f"{API}/search/jql",
                                    json={"jql": "project = LAUNCH", "fields": ["summary"], "expand": "names"}))  # fmt: skip
    assert found["names"] == {"summary": "Summary"}
    assert all("names" not in issue for issue in found["issues"])


async def test_search_pages_past_100_issues_when_asked_for_more(site: Site) -> None:
    """Same page: at most 5000 issues a page; the fake held a full-field page to 100 of its own accord."""
    for n in range(101):
        ok(await site.http.post(f"{API}/issue", json={"fields": {"project": {"key": "FIELD"}, "summary": f"Kit {n}",
                                                                  "issuetype": {"name": "Task"}}}), 201)  # fmt: skip
    found = ok(await site.http.post(f"{API}/search/jql",
                                    json={"jql": "project = FIELD", "fields": ["summary"], "maxResults": 200}))  # fmt: skip
    assert (len(found["issues"]), found["isLast"]) == (102, True)


async def test_search_with_no_room_on_the_page_is_refused_501(site: Site) -> None:
    refused(await site.http.post(f"{API}/search/jql", json={"jql": "project = LAUNCH", "maxResults": 0}), 501)


async def test_an_expansion_the_fake_does_not_serve_is_refused_501_naming_it(site: Site) -> None:
    body = refused(await site.http.get(f"{API}/issue/LAUNCH-1", params={"expand": "renderedFields"}), 501)
    assert "expand=renderedFields" in body["errorMessages"][0]


# --------------------------------------------------------------------------- JQL


async def _keys(site: Site, jql: str) -> list[str]:
    return [
        i["key"] for i in ok(await site.http.post(f"{API}/search/jql", json={"jql": jql, "fields": ["key"]}))["issues"]
    ]


async def test_jql_month_increments_are_calendar_months_and_a_bare_increment_is_the_functions_own(site: Site) -> None:
    """https://support.atlassian.com/jira-software-cloud/docs/jql-functions/ — `startOfMonth(-1)` is the start
    of last month (an increment without a unit is in the function's own period), and `-1M` is a calendar month,
    not 30 days. LAUNCH-2 was created on 2026-08-21 at 10:50:03; the clock is moved to 2026-09-21 at 10:50:03."""
    site.clock.jump(START + timedelta(days=28))
    assert "LAUNCH-2" not in await _keys(site, "project = LAUNCH AND created >= startOfMonth()")
    assert "LAUNCH-2" in await _keys(site, "project = LAUNCH AND created >= startOfMonth(-1)")
    assert "LAUNCH-2" in await _keys(site, "project = LAUNCH AND created <= endOfMonth(-1)")
    assert "LAUNCH-2" in await _keys(site, "project = LAUNCH AND created >= -1M")
    assert "LAUNCH-2" in await _keys(site, "project = LAUNCH AND created >= startOfYear() AND created <= endOfWeek()")


async def test_jql_a_function_jira_documents_and_the_fake_does_not_serve_is_refused_501_naming_it(site: Site) -> None:
    body = refused(await site.http.post(f"{API}/search/jql", json={"jql": 'assignee in membersOf("devs")'}), 501)
    assert "membersOf()" in body["errorMessages"][0]


async def test_jql_history_operators_are_refused_501_naming_them(site: Site) -> None:
    body = refused(await site.http.post(f"{API}/search/jql", json={"jql": "status WAS Done"}), 501)
    assert "WAS" in body["errorMessages"][0]


async def test_jql_a_field_jira_documents_and_the_fake_does_not_serve_is_refused_501_naming_it(site: Site) -> None:
    body = refused(await site.http.post(f"{API}/search/jql", json={"jql": "watcher = currentUser()"}), 501)
    assert "'watcher'" in body["errorMessages"][0]


# --------------------------------------------------------------------------- issues


async def test_an_issues_changelog_expansion_is_most_recent_first(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get — `changelog` is sorted by date, starting
    from the most recent; `GET /changelog` stays oldest first."""
    site.clock.jump(START + timedelta(minutes=5))
    ok(await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Write the notes"}}), 204)
    expanded = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"expand": "changelog"}))["changelog"]
    paged = ok(await site.http.get(f"{API}/issue/LAUNCH-1/changelog"))["values"]
    assert [h["id"] for h in expanded["histories"]] == [h["id"] for h in reversed(paged)]
    assert expanded["histories"][0]["items"][0]["field"] == "summary"


async def test_an_issue_read_with_only_exclusions_answers_every_other_field(site: Site) -> None:
    """Same page: `-description` returns all (default) fields except description."""
    fields = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "-description"}))["fields"]
    assert "description" not in fields and {"summary", "comment", "labels"} <= set(fields)


async def test_status_category_change_date_moves_only_when_the_category_does(site: Site) -> None:
    """`statuscategorychangedate` is the time the status last entered another category; an edit, or a move
    between two statuses of one category, leaves it."""
    created = ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["fields"]["created"]
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["fields"]["statuscategorychangedate"] == created
    site.clock.jump(START + timedelta(minutes=10))
    ok(await site.http.post(f"{API}/issue/LAUNCH-1/transitions", json={"transition": {"id": "11"}}), 204)
    started = ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["fields"]["updated"]
    site.clock.jump(START + timedelta(minutes=20))
    ok(await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Edited"}}), 204)
    ok(await site.http.post(f"{API}/issue/LAUNCH-1/transitions", json={"transition": {"id": "21"}}), 204)
    fields = ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["fields"]
    assert fields["statuscategorychangedate"] == started != fields["updated"]


async def test_an_edit_asked_to_return_the_issue_answers_200_with_it(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-put — `returnIssue=true` answers 200 with the
    issue as `GET /issue` gives it."""
    edited = ok(await site.http.put(f"{API}/issue/LAUNCH-1", params={"returnIssue": "true"},
                                    json={"fields": {"summary": "Returned"}}))  # fmt: skip
    assert (edited["key"], edited["fields"]["summary"]) == ("LAUNCH-1", "Returned")


async def test_an_update_operation_the_fake_does_not_serve_is_refused_501_not_400(site: Site) -> None:
    body = {"update": {"comment": [{"add": {"body": _doc("Via update")}}]}}
    refused(await site.http.put(f"{API}/issue/LAUNCH-1", json=body), 501)


async def test_transitions_read_for_one_transition_id_answer_that_one_alone(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-transitions-get — `transitionId`."""
    found = ok(await site.http.get(f"{API}/issue/LAUNCH-1/transitions", params={"transitionId": "51"}))
    assert [t["id"] for t in found["transitions"]] == ["51"]


async def test_a_link_whose_comment_is_not_a_document_is_refused_400_and_links_nothing(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-links/#api-rest-api-3-issuelink-post — the link's comment is a `Comment`, whose body
    is an Atlassian document in v3; the fake used to drop a plain-text one and link anyway."""
    body = {"type": {"name": "Relates"}, "inwardIssue": {"key": "LAUNCH-2"}, "outwardIssue": {"key": "LAUNCH-1"},
            "comment": {"body": "plain text"}}  # fmt: skip
    refused(await site.http.post(f"{API}/issueLink", json=body), 400)
    links = ok(await site.http.get(f"{API}/issue/LAUNCH-1", params={"fields": "issuelinks"}))["fields"]["issuelinks"]
    assert [link["type"]["name"] for link in links] == ["Blocks"]


# --------------------------------------------------------------------------- agile and the site


async def test_boards_filter_by_name(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/#api-rest-agile-1-0-board-get —
    `name` filters to boards that match or partially match it."""
    assert [b["name"] for b in ok(await site.http.get(f"{AGILE}/board", params={"name": "launch"}))["values"]] == [
        "Launch board"
    ]
    assert ok(await site.http.get(f"{AGILE}/board", params={"name": "nothing like it"}))["values"] == []


async def test_server_info_carries_no_name_of_the_fakes_own(site: Site) -> None:
    info = ok(await site.http.get(f"{API}/serverInfo"))
    assert "minutehand" not in str(info).lower()


async def test_the_app_account_is_answered_as_itself_for_its_email(site: Site) -> None:
    async with site.client(basic("automation@lanternworks.invalid", "x")) as app:
        assert ok(await app.get(f"{API}/myself"))["accountType"] == "app"


async def test_a_service_desk_template_the_enum_lists_builds_a_service_desk_project(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post
    — `projectTemplateKey`'s enum lists `simplified-internal-service-desk`; the fake used to refuse it."""
    body = {"key": "DESK", "name": "Desk", "leadAccountId": AGENT,
            "projectTemplateKey": "com.atlassian.servicedesk:simplified-internal-service-desk"}  # fmt: skip
    ok(await site.http.post(f"{API}/project", json=body), 201)
    assert ok(await site.http.get(f"{API}/project/DESK"))["projectTypeKey"] == "service_desk"


async def test_assigning_minus_one_gives_the_default_assignee_and_no_account_id_is_refused_400(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-assignee-put
    — `"-1"` assigns the project's default assignee (LAUNCH's is nobody), `null` unassigns, and a body without
    `accountId` is a 400."""
    ok(await site.http.put(f"{API}/issue/LAUNCH-1/assignee", json={"accountId": "-1"}), 204)
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1"))["fields"]["assignee"] is None
    refused(await site.http.put(f"{API}/issue/LAUNCH-1/assignee", json={}), 400)


async def test_a_link_of_a_type_the_site_has_not_got_is_refused_404(site: Site) -> None:
    """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-links/#api-rest-api-3-issuelink-post
    — a 404 when the issue link type is not found."""
    body = {"type": {"name": "Haunts"}, "inwardIssue": {"key": "LAUNCH-2"}, "outwardIssue": {"key": "LAUNCH-1"}}
    refused(await site.http.post(f"{API}/issueLink", json=body), 404)
