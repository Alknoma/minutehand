"""The fake held to what a public Jira Cloud site answered (`data/observed/`, recorded 2026-10-08 with no
credentials, see `data/README.md`): for each recorded request, the same request to the fake, its identifiers
swapped for this world's, answers the same status and body."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.providers.jira.jira_site import API, Site

OBSERVED = Path(__file__).parent / "data" / "observed"
SITE = "https://lanternworks.atlassian.net"


def recorded(name: str) -> tuple[int, Any]:
    head, _, body = (OBSERVED / f"{name}.http").read_bytes().decode("utf-8").partition("\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


def swapped(tree: Any, swaps: dict[str, str]) -> Any:
    text = json.dumps(tree)
    for old, new in swaps.items():
        text = text.replace(old, new)
    return json.loads(text)


Case = tuple[str, str, str, object | None, dict[str, str]]
CASES: list[Case] = [
    ("unknown_issue", "GET", "/issue/NOPE-999999", None, {}),
    ("unknown_project", "GET", "/project/NOPEZZQ", None, {}),
    ("issue_link_unknown", "GET", "/issueLink/99999999", None, {}),
    ("jql_unbounded", "GET", "/search/jql?jql=order%20by%20key", None, {}),
    ("jql_expecting_field", "GET", "/search/jql?jql=project%20%3D%20LAUNCH%20AND", None, {}),
    ("jql_quote_unclosed", "GET", "/search/jql?jql=summary%20~%20%22unclosed", None, {}),
    ("jql_parenthesis_unclosed", "GET", "/search/jql?jql=project%20%3D%20LAUNCH%20AND%20(status%20%3D%20Done", None, {}),
    ("jql_value_missing", "GET", "/search/jql?jql=project%20%3D", None, {}),
    ("jql_and_or_expected", "GET", "/search/jql?jql=project%20%3D%20PUB%20status", None, {}),
    ("jql_value_unexpected", "GET", "/search/jql?jql=project%20%3D%20PUB%20AND%20status%20%3D%20%3D%20Open", None, {}),
    ("jql_field_unknown", "GET", "/search/jql?jql=colour%20%3D%20red", None, {}),
    ("jql_value_unknown", "GET", "/search/jql?jql=status%20%3D%20%22Zzqq%22", None, {}),
    ("jql_function_unknown", "GET", "/search/jql?jql=assignee%20%3D%20noSuchFunction()", None, {}),
    ("jql_function_wrong_field", "GET", "/search/jql?jql=status%20%3D%20currentUser()", None, {}),
    ("jql_operator_unsupported", "GET", "/search/jql?jql=text%20%3D%20%22export%22", None, {}),
    ("jql_date_invalid", "GET", "/search/jql?jql=updated%20%3E%3D%20yesterday", None, {}),
    ("jql_key_unknown", "GET", "/search/jql?jql=key%20%3D%20PUB-99999999", None, {}),
    ("jql_project_unknown", "GET", "/search/jql?jql=project%20%3D%20NOPEZZQ", None, {}),
    ("jql_is_not_empty_value", "GET", "/search/jql?jql=status%20is%20Open", None, {}),
    ("search_max_results_zero", "GET", "/search/jql?jql=project%20%3D%20LAUNCH&maxResults=0", None, {}),
    ("search_max_results_over", "GET", "/search/jql?jql=project%20%3D%20LAUNCH&maxResults=6000&fields=id", None, {}),
    ("search_page_token_invalid", "GET", "/search/jql?jql=project%20%3D%20LAUNCH&nextPageToken=garbage", None, {}),
    ("search_retired", "GET", "/search", None, {}),
    ("mypermissions_no_keys", "GET", "/mypermissions", None, {}),
    ("mypermissions_unknown_key", "GET", "/mypermissions?permissions=LAUNCH_ROCKETS", None, {}),
    ("mypermissions_unknown_project", "GET", "/mypermissions?permissions=BROWSE_PROJECTS&projectKey=orb_it", None, {}),
    ("comments_order_unknown", "GET", "/issue/LAUNCH-1/comment?orderBy=author", None, {}),
    ("comments_max_results_not_a_number", "GET", "/issue/LAUNCH-1/comment?maxResults=x", None, {"PUB-1": "LAUNCH-1"}),
    ("user_search_no_query", "GET", "/user/search", None, {}),
    ("user_search_query_and_account", "GET", "/user/search?query=a&accountId=x", None, {}),
    ("assignable_no_project", "GET", "/user/assignable/search", None, {}),
    ("project_search_order_unknown", "GET", "/project/search?orderBy=zz", None, {}),
    ("approximate_count_get", "GET", "/search/approximate-count", None, {}),
    ("transitions_unknown_id", "GET", "/issue/LAUNCH-1/transitions?transitionId=999", None, {}),
    ("unknown_path", "GET", "/issue/LAUNCH-1/no-such-thing", None, {"PUB-1": "LAUNCH-1"}),
    ("issue_create_empty", "POST", "/issue", "", {}),
    ("issue_create_not_json", "POST", "/issue", "not json", {}),
    ("issue_create_not_object", "POST", "/issue", "[1]", {}),
    ("issue_create_no_project", "POST", "/issue", '{"fields":{}}', {}),
    ("issue_create_no_type", "POST", "/issue", '{"fields":{"project":{"key":"LAUNCH"}}}', {}),
    ("issue_edit_not_on_screen", "PUT", "/issue/LAUNCH-1", '{"fields":{"environment":"x"}}', {"summary": "environment"}),
    ("transition_missing", "POST", "/issue/LAUNCH-1/transitions", "{}", {}),
    ("link_type_unknown", "POST", "/issueLink",
     '{"type":{"name":"Haunts"},"inwardIssue":{"key":"LAUNCH-1"},"outwardIssue":{"key":"LAUNCH-2"}}', {}),
    ("search_body_unknown_property", "POST", "/search/jql", '{"jql":"project = LAUNCH","colour":1}', {}),
    ("search_body_wrong_type", "POST", "/search/jql", '{"jql":"project = LAUNCH","maxResults":"x"}', {}),
    ("count_unbounded", "POST", "/search/approximate-count", '{"jql":"order by key"}', {}),
]  # fmt: skip
"""Each recording, the request that matches it here, and the identifiers swapped from the public site's to this
world's. `comment_body_not_a_document.http` is held separately: the site refused the anonymous caller beside the
body."""


@pytest.mark.parametrize(("name", "method", "path", "body", "swaps"), CASES, ids=[c[0] for c in CASES])
async def test_the_fake_answers_as_the_public_site_did(
    site: Site, name: str, method: str, path: str, body: str | None, swaps: dict[str, str]
) -> None:
    status, expected = recorded(name)
    if method == "GET":
        answer = await site.http.request(method, f"{API}{path}")
    else:
        answer = await site.http.request(method, f"{API}{path}", content=body or b"",
                                         headers={"Content-Type": "application/json"})  # fmt: skip
    got = answer.json()
    if "nextPageToken" in got:
        got.pop("nextPageToken")
    expected = swapped(expected, swaps)
    if isinstance(expected, dict) and "issues" in expected and expected["issues"]:
        pytest.fail(f"{name}: a recording with issues cannot be compared")
    assert (answer.status_code, got) == (status, expected), name


async def test_a_comment_body_that_is_not_a_document_is_refused_in_the_sites_words(site: Site) -> None:
    _, expected = recorded("comment_body_not_a_document")
    answer = await site.http.post(f"{API}/issue/LAUNCH-1/comment", json={"body": "plain"})
    assert (answer.status_code, answer.json()["errors"]) == (400, expected["errors"])


async def test_the_gateway_answers_an_unknown_cloud_id_as_it_did(site: Site) -> None:
    _, expected = recorded("gateway_unknown_cloud_id")
    path = "/ex/jira/00000000-0000-0000-0000-000000000000/rest/api/3/myself"
    answer = await site.http.get(f"https://api.atlassian.com{path}")
    got = answer.json()
    assert answer.status_code == 404
    assert {k: v for k, v in got.items() if k != "timestamp"} == {k: v for k, v in expected.items() if k != "timestamp"}


async def test_the_token_endpoint_answers_an_unreadable_body_as_it_did(site: Site) -> None:
    status, expected = recorded("token_unreadable")
    answer = await site.http.post("https://auth.atlassian.com/oauth/token", content=b"nope",
                                  headers={"Content-Type": "application/json"})  # fmt: skip
    assert (answer.status_code, answer.json()) == (status, expected)
