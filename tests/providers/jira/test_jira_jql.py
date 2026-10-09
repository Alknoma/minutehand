"""One test per JQL shape: those a tracker client builds, and those an agent writing JQL by hand reaches for.

The site holds LAUNCH-1 (Story, High, To Do, Tomas, labels docs and beta, due 28 August), LAUNCH-2 (Task,
Medium, Done, Noor, created three days before the start), FIELD-1 (Bug, Lowest, To Do, nobody's) and VAULT-1,
which the agent cannot see. Every refusal is Jira's 400, never an answer of everything.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.providers.jira.jira_site import API, NOOR, START, TOMAS, Site, ok, refused


async def keys(site: Site, jql: str) -> list[str]:
    found = ok(await site.http.post(f"{API}/search/jql", json={"jql": jql, "fields": ["summary"]}))
    return [i["key"] for i in found["issues"]]


async def test_project_equals_an_unquoted_key(site: Site) -> None:
    assert sorted(await keys(site, "project = LAUNCH")) == ["LAUNCH-1", "LAUNCH-2"]


async def test_project_equals_a_quoted_key_or_a_name(site: Site) -> None:
    assert sorted(await keys(site, 'project = "LAUNCH"')) == ["LAUNCH-1", "LAUNCH-2"]
    assert await keys(site, 'project = "Field Ops"') == ["FIELD-1"]


async def test_a_project_the_caller_cannot_see_is_refused_400_as_one_the_site_has_not_got(site: Site) -> None:
    """A signed-in caller is told the value does not exist, whether the project is missing or hidden from them."""
    body = refused(await site.http.post(f"{API}/search/jql", json={"jql": "project = VAULT"}), 400)
    assert body == {"errorMessages": ["The value 'VAULT' does not exist for the field 'project'."], "errors": {}}
    assert "VAULT-1" not in await keys(site, "statusCategory = new")


async def test_assignee_equals_a_quoted_account_id(site: Site) -> None:
    assert await keys(site, f'assignee = "{TOMAS}"') == ["LAUNCH-1"]


async def test_assignee_is_empty(site: Site) -> None:
    assert await keys(site, "assignee is EMPTY") == ["FIELD-1"]
    assert sorted(await keys(site, "assignee is not EMPTY")) == ["LAUNCH-1", "LAUNCH-2"]


async def test_not_equals_never_matches_an_empty_field(site: Site) -> None:
    assert await keys(site, f'assignee != "{TOMAS}"') == ["LAUNCH-2"], "FIELD-1 has no assignee, so it is left out"
    assert await keys(site, "labels not in (docs)") == []


async def test_assignee_equals_current_user(site: Site) -> None:
    assert await keys(site, "assignee = currentUser()") == []
    await site.http.put(f"{API}/issue/FIELD-1/assignee", json={"accountId": site.jira.site().agent})
    assert await keys(site, "assignee = currentUser()") == ["FIELD-1"]


async def test_priority_equals_a_name(site: Site) -> None:
    assert await keys(site, 'priority = "High"') == ["LAUNCH-1"]
    assert await keys(site, "priority >= High") == ["LAUNCH-1"], "High and above"


async def test_status_equals_a_quoted_name(site: Site) -> None:
    await site.http.post(f"{API}/issue/LAUNCH-1/transitions", json={"transition": {"id": "11"}})
    assert await keys(site, 'status = "In Progress"') == ["LAUNCH-1"]


async def test_status_in_a_list(site: Site) -> None:
    assert sorted(await keys(site, 'status in ("To Do", Done)')) == ["FIELD-1", "LAUNCH-1", "LAUNCH-2"]
    assert await keys(site, 'status not in ("To Do")') == ["LAUNCH-2"]


async def test_labels_one_clause_per_label_all_must_hold(site: Site) -> None:
    assert await keys(site, 'labels = "docs" AND labels = "beta"') == ["LAUNCH-1"]
    assert await keys(site, 'labels = "docs" AND labels = "nothing"') == []


async def test_issuetype_equals_a_name(site: Site) -> None:
    assert await keys(site, 'issuetype = "Bug"') == ["FIELD-1"]
    assert await keys(site, "type = Story") == ["LAUNCH-1"]


async def test_status_category_not_done(site: Site) -> None:
    assert sorted(await keys(site, "statusCategory != Done")) == ["FIELD-1", "LAUNCH-1"]


async def test_duedate_is_empty(site: Site) -> None:
    assert sorted(await keys(site, "duedate is EMPTY")) == ["FIELD-1", "LAUNCH-2"]


async def test_duedate_before_and_after_a_quoted_date(site: Site) -> None:
    assert await keys(site, 'duedate <= "2026-08-28"') == ["LAUNCH-1"]
    assert await keys(site, 'duedate >= "2026-08-29"') == []
    assert await keys(site, 'duedate >= "2026-08-28" AND duedate <= "2026-08-28"') == ["LAUNCH-1"]


async def test_text_contains_words_from_summary_description_and_comments(site: Site) -> None:
    assert await keys(site, 'text ~ "changelog"') == ["LAUNCH-1"], "the description"
    assert await keys(site, 'text ~ "shared folder"') == ["LAUNCH-1"], "a comment"
    assert await keys(site, 'text ~ "demo kits"') == ["FIELD-1"], "the summary"
    assert await keys(site, 'text ~ "chang"') == [], "a word, not a substring"
    assert await keys(site, 'text ~ "chang*"') == ["LAUNCH-1"], "unless it says so"


async def test_text_with_an_escaped_quote_is_a_phrase(site: Site) -> None:
    assert await keys(site, 'text ~ "\\"new export\\""') == ["LAUNCH-1"]
    assert await keys(site, 'text ~ "\\"export new\\""') == []


async def test_order_by_duedate_ascending_puts_empty_last_and_descending_first(site: Site) -> None:
    ascending = await keys(site, "project in (LAUNCH, FIELD) ORDER BY duedate ASC")
    descending = await keys(site, "project in (LAUNCH, FIELD) ORDER BY duedate DESC")
    assert ascending[0] == "LAUNCH-1" and descending[-1] == "LAUNCH-1"


async def test_order_by_priority_and_then_key(site: Site) -> None:
    assert await keys(site, "project in (LAUNCH, FIELD) ORDER BY priority DESC, key ASC") == [
        "LAUNCH-1",
        "LAUNCH-2",
        "FIELD-1",
    ]


async def test_resolution_unresolved(site: Site) -> None:
    assert sorted(await keys(site, "resolution = Unresolved")) == ["FIELD-1", "LAUNCH-1"]
    assert await keys(site, "resolution = Done") == ["LAUNCH-2"]


async def test_updated_within_a_relative_period(site: Site) -> None:
    site.clock.jump(START + timedelta(days=5))
    assert sorted(await keys(site, "updated >= -7d")) == ["FIELD-1", "LAUNCH-1"]
    assert await keys(site, "created < -7d") == ["LAUNCH-2"]
    assert sorted(await keys(site, "updated >= startOfDay(-10d)")) == ["FIELD-1", "LAUNCH-1", "LAUNCH-2"]


async def test_or_not_and_parentheses(site: Site) -> None:
    jql = f'(assignee = "{NOOR}" OR priority = Lowest) AND NOT issuetype = Bug'
    assert await keys(site, jql) == ["LAUNCH-2"]
    assert sorted(await keys(site, "project = FIELD OR labels = beta")) == ["FIELD-1", "LAUNCH-1"]


async def test_a_custom_field_by_cf_number_and_by_name(site: Site) -> None:
    assert await keys(site, "cf[10016] > 3") == ["LAUNCH-1"]
    assert await keys(site, 'Team = "Platform"') == ["LAUNCH-1"]
    assert await keys(site, "sprint in openSprints()") == ["LAUNCH-1"]


async def test_key_in_a_list(site: Site) -> None:
    assert sorted(await keys(site, "key in (LAUNCH-2, FIELD-1)")) == ["FIELD-1", "LAUNCH-2"]


@pytest.mark.parametrize(
    ("jql", "message"),
    [
        ("", "Unbounded JQL queries are not allowed here. Please add a search restriction to your query."),
        ("ORDER BY duedate ASC", "Unbounded JQL queries are not allowed here. Please add a search restriction to your query."),
        ("project = LAUNCH AND", "Error in the JQL Query: Expecting a field name at the end of the query."),
        ("project =", "Error in JQL Query: Expecting either a value, list or function before the end of the query."),
        ("project = LAUNCH AND status = = Done",
         "Error in JQL Query: Expecting either a value, list or function but got '='. You must surround '=' in "
         "quotation marks to use it as a value. (line 1, character 31)"),
        ("project = LAUNCH status", "Error in the JQL Query: Expecting either 'OR' or 'AND' but got 'status'. (line 1, character 18)"),
        ("project = LAUNCH AND (status = Done", "Error in the JQL Query: Expecting ')' before the end of the query."),
        ('summary ~ "unclosed', "Error in the JQL Query: The quoted string 'unclosed' has not been completed. (line 1, character 11)"),
    ],
)  # fmt: skip
async def test_a_query_that_cannot_be_read_is_refused_with_400(site: Site, jql: str, message: str) -> None:
    """Each sentence as a public Jira Cloud site answers it (`data/observed/jql_*.http`)."""
    body = refused(await site.http.post(f"{API}/search/jql", json={"jql": jql}), 400)
    assert body == {"errorMessages": [message], "errors": {}}


DATE_FORMATS = (
    "Valid formats include: 'yyyy/MM/dd HH:mm', 'yyyy-MM-dd HH:mm', 'yyyy/MM/dd', 'yyyy-MM-dd', or a period format "
    "e.g. '-5d', '4w 2d'."
)
SEARCH_INVALID = "Returned if the search request is invalid"


@pytest.mark.parametrize(
    ("jql", "message"),
    [
        ("colour = red", "Field 'colour' does not exist or you do not have permission to view it."),
        ('status = "Shipped"', "The value 'Shipped' does not exist for the field 'status'."),
        ('assignee = "nobody-at-all"', "The value 'nobody-at-all' does not exist for the field 'assignee'."),
        ("project = NOPE", "The value 'NOPE' does not exist for the field 'project'."),
        ("assignee = noSuchFunction()", "Unable to find JQL function 'noSuchFunction()'."),
        ("status = currentUser()", "A value provided by the function 'currentUser' is invalid for the field 'status'."),
        ('text = "export"', "The operator '=' is not supported by the 'text' field."),
        ("updated >= yesterday", f"Date value 'yesterday' for field 'updated' is invalid. {DATE_FORMATS}"),
        ("due = 2026-02-30", f"Date value '2026-02-30' for field 'due' is invalid. {DATE_FORMATS}"),
        ("key = LAUNCH-999", "Issue does not exist or you do not have permission to see it."),
        ("status is Done", SEARCH_INVALID),
        ('summary ~ "?"', SEARCH_INVALID),
        ("created >= startOfDay(-1x)", SEARCH_INVALID),
        ("project = LAUNCH ORDER BY zzfield", "Field 'zzfield' does not exist or you do not have permission to view it."),
    ],
)  # fmt: skip
async def test_a_query_naming_what_the_site_has_not_got_is_refused_400_as_for_a_signed_in_caller(
    site: Site, jql: str, message: str
) -> None:
    """Every caller is signed in (any credential acts), so each is Jira's 400 for a signed-in caller, not the 200 with
    no issues an anonymous caller gets (`data/observed/jql_*_unknown.http` and the rest); CLAIMS.md cites each
    sentence, and marks the reference's words observed-pending. GET and the approximate count answer the same."""
    expected = {"errorMessages": [message], "errors": {}}
    assert refused(await site.http.post(f"{API}/search/jql", json={"jql": jql}), 400) == expected
    assert refused(await site.http.get(f"{API}/search/jql", params={"jql": jql}), 400) == expected
    assert refused(await site.http.post(f"{API}/search/approximate-count", json={"jql": jql}), 400) == expected


async def test_keys_in_a_list_answer_those_that_exist(site: Site) -> None:
    """`issuekey in (...)` answers the issues it finds rather than failing the search on a missing one."""
    assert await keys(site, "key in (LAUNCH-1, LAUNCH-999)") == ["LAUNCH-1"]


async def test_ordering_by_a_field_searched_but_not_sorted_here_is_refused_501_naming_it(site: Site) -> None:
    answer = await site.http.post(f"{API}/search/jql", json={"jql": "project = LAUNCH ORDER BY labels"})
    assert answer.status_code == 501 and "labels" in answer.text
