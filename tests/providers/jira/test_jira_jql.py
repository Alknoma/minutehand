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


async def test_a_project_the_caller_cannot_see_is_refused_as_unknown(site: Site) -> None:
    body = refused(await site.http.post(f"{API}/search/jql", json={"jql": "project = VAULT"}), 400)
    assert body["errorMessages"] == ["The value 'VAULT' does not exist for the field 'project'."]
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
        ("", "A query with no restriction cannot be run here: add a condition to the JQL."),
        ("ORDER BY duedate ASC", "A query with no restriction cannot be run here: add a condition to the JQL."),
        ("colour = red", "Field 'colour' does not exist, or you are not allowed to see it."),
        ('status = "Shipped"', "The value 'Shipped' does not exist for the field 'status'."),
        ('assignee = "nobody-at-all"', "The value 'nobody-at-all' does not exist for the field 'assignee'."),
        (
            "project = LAUNCH AND",
            "Error in the JQL query: expected a field name at character 20, but found the end of the query.",
        ),
        ("assignee = noSuchFunction()", "Unable to find JQL function 'noSuchFunction()'."),
        ("status = currentUser()", "The function 'currentUser()' cannot be used with the field 'status'."),
        ("updated >= yesterday", "Date value 'yesterday' for field 'updated' is invalid: write 'yyyy/MM/dd HH:mm', "
         "'yyyy-MM-dd HH:mm', 'yyyy/MM/dd', 'yyyy-MM-dd', or a period such as '-5d' or '4w 2d'."),
        ('text = "export"', "The operator '=' is not supported by the 'text' field."),
    ],
)  # fmt: skip
async def test_a_query_that_cannot_be_read_is_refused_with_400(site: Site, jql: str, message: str) -> None:
    body = refused(await site.http.post(f"{API}/search/jql", json={"jql": jql}), 400)
    assert body == {"errorMessages": [message], "errors": {}}
