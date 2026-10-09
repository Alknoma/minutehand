"""Issues, as an agent works them through the REST API: listed, opened, edited, closed, locked. What the agent writes is
kept and returned as it was sent; what GitHub assigns is made by the provider."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.domain.world import Actor, Operation, TransitionSnapshot
from tests.providers.github.github_world import (
    OUTSIDER,
    START,
    TOMAS,
    Hub,
    Json,
    body,
    listing,
    refusal,
    tracker_seed,
)

ISSUES = "/repos/lanternworks/ledger/issues"


@pytest.fixture
def seeded() -> GitHubSeed:
    return tracker_seed()


def numbers(found: list[Json]) -> list[object]:
    return [i["number"] for i in found]


# ------------------------------------------------------------------------------------------------------ listing


async def test_issues_list_the_open_ones_newest_first_and_mark_the_pull_request(hub: Hub) -> None:
    """Documented: "Only open issues will be listed" by default, and a pull request is an issue, marked by its
    `pull_request` key. https://docs.github.com/en/rest/issues/issues#list-repository-issues"""
    async with hub.client() as http:
        found = listing(await http.get(ISSUES))
    assert numbers(found) == [4, 3, 1]
    marked = {i["number"]: "pull_request" in i for i in found}
    assert marked == {4: True, 3: False, 1: False}
    assert found[0]["draft"] is False and "draft" not in found[1]


async def test_issues_filter_by_state(hub: Hub) -> None:
    """Documented: `state` is open (the default), closed or all."""
    async with hub.client() as http:
        closed = listing(await http.get(ISSUES, params={"state": "closed"}))
        everything = listing(await http.get(ISSUES, params={"state": "all"}))
    assert numbers(closed) == [2]
    assert numbers(everything) == [4, 3, 1, 2]
    assert closed[0]["state"] == "closed" and closed[0]["state_reason"] == "completed"


async def test_issues_filter_by_labels_assignee_and_creator(hub: Hub) -> None:
    """Documented: `labels` are comma separated and all must be on the issue; `assignee` takes a login, `none` or `*`;
    `creator` a login."""
    async with hub.client() as http:
        bug = listing(await http.get(ISSUES, params={"labels": "bug", "state": "all"}))
        both = listing(await http.get(ISSUES, params={"labels": "bug,docs", "state": "all"}))
        iris = listing(await http.get(ISSUES, params={"assignee": "iris-calder"}))
        unassigned = listing(await http.get(ISSUES, params={"assignee": "none"}))
        assigned = listing(await http.get(ISSUES, params={"assignee": "*"}))
        tomas = listing(await http.get(ISSUES, params={"creator": "tomas-b", "state": "all"}))
    assert numbers(bug) == [1] and both == []
    assert numbers(iris) == [4, 1] and numbers(unassigned) == [] and numbers(assigned) == [4, 3, 1]
    assert sorted(cast(list[int], numbers(tomas))) == [1, 4]


async def test_issues_sort_by_created_updated_or_comments_in_either_direction(hub: Hub) -> None:
    """Documented: `sort` is created (the default), updated or comments, `direction` asc or desc (desc by default)."""
    async with hub.client() as http:
        oldest = listing(await http.get(ISSUES, params={"direction": "asc"}))
        by_comments = listing(await http.get(ISSUES, params={"sort": "comments", "direction": "desc"}))
        by_update = listing(await http.get(ISSUES, params={"sort": "updated", "direction": "asc"}))
    assert numbers(oldest) == [1, 3, 4]
    assert numbers(by_comments)[:2] == [1, 4]
    assert numbers(by_update) == [1, 3, 4]


async def test_issues_since_keeps_those_updated_at_or_after_it(hub: Hub) -> None:
    """Recorded of the real service: `since` is inclusive (tests/data/github_rest/): an issue updated at `since` is
    listed, and one updated a second before it is not.
    https://docs.github.com/en/rest/issues/issues#list-repository-issues"""

    def stamp(at: datetime) -> str:
        return at.strftime("%Y-%m-%dT%H:%M:%SZ")

    async with hub.client() as http:
        three = str(body(await http.get(f"{ISSUES}/3"))["updated_at"])
        at = listing(await http.get(ISSUES, params={"since": three}))
        a_second_on = listing(await http.get(ISSUES, params={"since": stamp(START - timedelta(days=1, seconds=-1))}))
        later = listing(await http.get(ISSUES, params={"since": stamp(START + timedelta(seconds=1))}))
    assert three == stamp(START - timedelta(days=1))
    assert numbers(at) == [4, 3] and numbers(a_second_on) == [4] and later == []


async def test_issues_are_paged_and_say_where_the_next_page_is(hub: Hub) -> None:
    """Documented: lists page by `per_page` and `page` and say so in `Link`.
    https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api"""
    async with hub.client() as http:
        first = await http.get(ISSUES, params={"per_page": 2})
        second = await http.get(ISSUES, params={"per_page": 2, "page": 2})
    assert numbers(listing(first)) == [4, 3] and numbers(listing(second)) == [1]
    assert 'page=2>; rel="next"' in first.headers["Link"] and 'rel="last"' in first.headers["Link"]
    assert 'rel="prev"' in second.headers["Link"] and "next" not in second.headers["Link"]


async def test_the_milestone_and_type_filters_follow_from_a_world_with_neither(hub: Hub) -> None:
    """Documented: `milestone=none` and `type=none` keep issues with none; `*` and a name or number keep issues that
    have one. No issue here has either, so the first keeps every issue and the others none."""
    async with hub.client() as http:
        none = listing(await http.get(ISSUES, params={"milestone": "none", "type": "none"}))
        any_milestone = listing(await http.get(ISSUES, params={"milestone": "*"}))
        numbered = listing(await http.get(ISSUES, params={"milestone": "1"}))
        typed = listing(await http.get(ISSUES, params={"type": "Bug"}))
    assert numbers(none) == [4, 3, 1] and any_milestone == [] and numbered == [] and typed == []


async def test_the_mentioned_filter_is_refused_by_name(hub: Hub) -> None:
    async with hub.client() as http:
        refused = await http.get(ISSUES, params={"mentioned": "iris-calder"})
    assert refused.status_code == 501 and "mentioned" in refused.json()["message"]


async def test_an_issue_reads_with_the_urls_and_counts_github_assigns(hub: Hub) -> None:
    async with hub.client() as http:
        one = body(await http.get(f"{ISSUES}/1"))
    assert one["url"] == "https://api.github.com/repos/lanternworks/ledger/issues/1"
    assert one["html_url"] == "https://github.com/lanternworks/ledger/issues/1"
    assert one["comments"] == 2 and one["locked"] is False and one["closed_at"] is None
    assert one["body"] == "The doubling wait reaches a minute.\n\n- seen in `retry_with_backoff`"
    assert [label["name"] for label in cast(list[Json], one["labels"])] == ["bug"]
    assert cast(Json, one["assignee"])["login"] == "iris-calder"
    assert cast(Json, one["user"])["login"] == "tomas-b"
    assert one["author_association"] == "COLLABORATOR"


async def test_an_issue_that_is_not_there_and_a_repository_the_caller_cannot_see_are_both_404(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(await http.get(f"{ISSUES}/99"), 404, "Not Found")
    async with hub.client(OUTSIDER) as http:
        refusal(await http.get(f"{ISSUES}/1"), 404, "Not Found")
        refusal(await http.post(ISSUES, json={"title": "Hello"}), 404, "Not Found")


# ------------------------------------------------------------------------------------------------------ creating


TITLE = "  Checkout: “Pay now” → 結帳  "
BODY = "Line one  \n\n```py\nprint('x')\n```\n\ttabbed \U0001f600\n"


async def test_an_issue_the_agent_opens_is_kept_and_returned_as_sent(hub: Hub) -> None:
    """Documented: create-an-issue answers 201 with the issue. Data stays as sent: title, body and label names come back
    byte for byte. https://docs.github.com/en/rest/issues/issues#create-an-issue"""
    async with hub.client(TOMAS) as http:
        made = await http.post(ISSUES, json={"title": TITLE, "body": BODY, "labels": ["bug", "docs"]})
        again = body(await http.get(f"{ISSUES}/5"))
    created = body(made, 201)
    assert created["title"] == TITLE and created["body"] == BODY
    assert [label["name"] for label in cast(list[Json], created["labels"])] == ["bug", "docs"]
    assert again["title"] == TITLE and again["body"] == BODY
    assert (created["number"], created["state"], created["comments"]) == (5, "open", 0)
    assert created["created_at"] == created["updated_at"] == "2026-08-24T10:50:03Z"
    assert cast(Json, created["user"])["login"] == "tomas-b"
    assert created["closed_at"] is None and created["assignee"] is None and created["assignees"] == []


async def test_a_body_left_out_is_null_and_an_empty_one_is_kept(hub: Hub) -> None:
    async with hub.client() as http:
        none = body(await http.post(ISSUES, json={"title": "No body"}), 201)
        empty = body(await http.post(ISSUES, json={"title": "Empty", "body": ""}), 201)
    assert none["body"] is None and empty["body"] == ""


async def test_a_whole_number_title_is_kept_as_its_text(hub: Hub) -> None:
    """Documented: `title` is a string or an integer. https://docs.github.com/en/rest/issues/issues#create-an-issue"""
    async with hub.client() as http:
        made = body(await http.post(ISSUES, json={"title": 404}), 201)
    assert made["title"] == "404"


async def test_labels_and_assignees_are_kept_whoever_sets_them(hub: Hub) -> None:
    """Authorization is out of scope: the labels and assignees of a new issue are kept as sent, by a reader as by a
    collaborator."""
    sent = {"title": "T", "labels": ["bug"], "assignees": ["tomas-b"]}
    async with hub.client() as http:
        reader = body(await http.post(ISSUES, json=sent), 201)
    async with hub.client(TOMAS) as http:
        pusher = body(await http.post(ISSUES, json=sent), 201)
    for made in (reader, pusher):
        assert [label["name"] for label in cast(list[Json], made["labels"])] == ["bug"]
        assert [a["login"] for a in cast(list[Json], made["assignees"])] == ["tomas-b"]
        assert cast(Json, made["assignee"])["login"] == "tomas-b"


async def test_labels_may_be_objects_with_a_name_and_an_assignee_may_stand_for_assignees(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        made = body(
            await http.post(ISSUES, json={"title": "T", "labels": [{"name": "docs"}], "assignee": "tomas-b"}), 201
        )
    assert [label["name"] for label in cast(list[Json], made["labels"])] == ["docs"]
    assert [a["login"] for a in cast(list[Json], made["assignees"])] == ["tomas-b"]


@pytest.mark.parametrize(
    "sent",
    [{}, {"title": None}, {"title": ""}, {"title": ["x"]}, {"title": "T", "body": 5}, {"title": "T", "labels": "bug"}],
    ids=["no-title", "null-title", "blank-title", "list-title", "numeric-body", "labels-a-string"],
)
async def test_a_request_without_a_title_or_with_the_wrong_type_is_422_invalid_request(hub: Hub, sent: Json) -> None:
    """Documented: omitting a required parameter or giving one the wrong type is 422 "Invalid request".
    https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api#invalid-request"""
    async with hub.client(TOMAS) as http:
        refusal(await http.post(ISSUES, json=sent), 422, "Invalid request")


async def test_a_body_that_is_not_json_or_not_an_object_is_400(hub: Hub) -> None:
    """Documented: "Problems parsing JSON" and "Body should be a JSON object", both 400.
    https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api#problems-parsing-json"""
    async with hub.client(TOMAS) as http:
        refusal(await http.post(ISSUES, content=b"{not json"), 400, "Problems parsing JSON")
        refusal(await http.post(ISSUES, json=["title"]), 400, "Body should be a JSON object")


@pytest.mark.parametrize(
    ("sent", "named"),
    [
        ({"title": "T", "milestone": 1}, "milestone"),
        ({"title": "T", "type": "Bug"}, "type"),
        ({"title": "T", "parent_issue_id": 5}, "parent_issue_id"),
        ({"title": "T", "issue_field_values": []}, "issue_field_values"),
        ({"title": "T", "labels": ["no-such-label"]}, "no-such-label"),
        ({"title": "T", "assignees": ["nobody-at-all"]}, "nobody-at-all"),
    ],
)
async def test_what_the_world_cannot_hold_is_refused_by_name(hub: Hub, sent: Json, named: str) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.post(ISSUES, json=sent)
    assert refused.status_code == 501 and named in refused.json()["message"]


async def test_an_issue_is_written_to_the_log_as_the_agents_with_its_title_and_body(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        await http.post(ISSUES, json={"title": "Logged", "body": "in the log"})
    written = [
        e
        for e in hub.store.events()
        if e.entity.external_id.startswith("issue/lanternworks/ledger/") and e.actor is Actor.AGENT
    ]
    assert [e.operation for e in written] == [Operation.CREATE]
    assert written[0].after is not None and "Logged" in written[0].after.model_dump_json()


# ------------------------------------------------------------------------------------------------------ updating


async def test_an_issue_closed_and_reopened_records_who_when_and_why(hub: Hub) -> None:
    """Documented: update-an-issue takes `state` (open, closed) and `state_reason`; the issue carries `closed_at` and
    `closed_by`. https://docs.github.com/en/rest/issues/issues#update-an-issue"""
    async with hub.client(TOMAS) as http:
        hub.clock.jump(START + timedelta(hours=1))
        closed = body(await http.patch(f"{ISSUES}/1", json={"state": "closed", "state_reason": "not_planned"}))
        hub.clock.jump(START + timedelta(hours=2))
        reopened = body(await http.patch(f"{ISSUES}/1", json={"state": "open"}))
    assert (closed["state"], closed["state_reason"], closed["closed_at"]) == (
        "closed",
        "not_planned",
        "2026-08-24T11:50:03Z",
    )
    assert cast(Json, closed["closed_by"])["login"] == "tomas-b" and closed["updated_at"] == "2026-08-24T11:50:03Z"
    assert (reopened["state"], reopened["state_reason"], reopened["closed_at"], reopened["closed_by"]) == (
        "open",
        None,
        None,
        None,
    )


async def test_a_state_reason_is_ignored_unless_the_state_changes(hub: Hub) -> None:
    """Documented: `state_reason` is "Ignored unless `state` is changed"."""
    async with hub.client(TOMAS) as http:
        same = body(await http.patch(f"{ISSUES}/1", json={"state": "open", "state_reason": "completed"}))
        alone = body(await http.patch(f"{ISSUES}/1", json={"state_reason": "completed"}))
    assert same["state_reason"] is None and alone["state_reason"] is None


async def test_title_and_body_are_replaced_as_sent_and_a_null_body_clears_it(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        edited = body(await http.patch(f"{ISSUES}/1", json={"title": TITLE, "body": BODY}))
        cleared = body(await http.patch(f"{ISSUES}/1", json={"body": None}))
        read = body(await http.get(f"{ISSUES}/1"))
    assert edited["title"] == TITLE and edited["body"] == BODY
    assert cleared["body"] is None and read["body"] is None and read["title"] == TITLE


async def test_labels_and_assignees_replace_the_set_and_an_empty_array_clears_it(hub: Hub) -> None:
    """Documented: pass labels or logins to _replace_ the set, `[]` to clear it.
    https://docs.github.com/en/rest/issues/issues#update-an-issue"""
    async with hub.client(TOMAS) as http:
        replaced = body(await http.patch(f"{ISSUES}/1", json={"labels": ["docs"], "assignees": ["tomas-b"]}))
        cleared = body(await http.patch(f"{ISSUES}/1", json={"labels": [], "assignees": []}))
    assert [label["name"] for label in cast(list[Json], replaced["labels"])] == ["docs"]
    assert [a["login"] for a in cast(list[Json], replaced["assignees"])] == ["tomas-b"]
    assert cleared["labels"] == [] and cleared["assignees"] == [] and cleared["assignee"] is None


async def test_anyone_who_sees_an_issue_may_edit_it(hub: Hub) -> None:
    """Authorization is out of scope: who edits an issue is not asked. Iris reads the ledger and edits Tomas's issue."""
    async with hub.client() as http:
        changed = body(await http.patch(f"{ISSUES}/1", json={"title": "Mine now", "labels": ["docs"], "assignees": []}))
    assert changed["title"] == "Mine now"
    assert [label["name"] for label in cast(list[Json], changed["labels"])] == ["docs"] and changed["assignees"] == []


async def test_an_edit_that_changes_nothing_leaves_the_update_time(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        before = body(await http.get(f"{ISSUES}/1"))["updated_at"]
        hub.clock.jump(START + timedelta(hours=1))
        same = body(await http.patch(f"{ISSUES}/1", json={"title": "Retries wait too long"}))
        moved = body(await http.patch(f"{ISSUES}/1", json={"title": "Retries wait far too long"}))
    assert same["updated_at"] == before and moved["updated_at"] == "2026-08-24T11:50:03Z"


@pytest.mark.parametrize(
    ("sent", "named"),
    [
        ({"milestone": 1}, "milestone"),
        ({"type": "Bug"}, "type"),
        ({"duplicate_issue_id": 5}, "duplicate_issue_id"),
        ({"labels": ["no-such-label"]}, "no-such-label"),
    ],
)
async def test_an_update_naming_what_the_world_cannot_hold_is_refused_by_name(hub: Hub, sent: Json, named: str) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.patch(f"{ISSUES}/1", json=sent)
    assert refused.status_code == 501 and named in refused.json()["message"]


async def test_a_null_milestone_is_no_milestone(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        assert body(await http.patch(f"{ISSUES}/1", json={"milestone": None}))["milestone"] is None


@pytest.mark.parametrize("sent", [{"state": "merged"}, {"state_reason": "because"}, {"title": ["x"]}, {"title": ""}])
async def test_an_update_with_a_value_outside_the_reference_is_422_invalid_request(hub: Hub, sent: Json) -> None:
    async with hub.client(TOMAS) as http:
        refusal(await http.patch(f"{ISSUES}/1", json=sent), 422, "Invalid request")


# ------------------------------------------------------------------------------------------------------ locking


async def test_a_conversation_is_locked_with_a_reason_and_unlocked(hub: Hub) -> None:
    """Documented: lock answers 204 and takes `lock_reason` (off-topic, too heated, resolved, spam); unlock answers 204.
    https://docs.github.com/en/rest/issues/issues#lock-an-issue"""
    async with hub.client(TOMAS) as http:
        locked = await http.put(f"{ISSUES}/1/lock", json={"lock_reason": "too heated"})
        read = body(await http.get(f"{ISSUES}/1"))
        bare = await http.put(f"{ISSUES}/3/lock")
        unlocked = await http.delete(f"{ISSUES}/1/lock")
        after = body(await http.get(f"{ISSUES}/1"))
    assert (locked.status_code, bare.status_code, unlocked.status_code) == (204, 204, 204)
    assert (read["locked"], read["active_lock_reason"]) == (True, "too heated")
    assert (after["locked"], after["active_lock_reason"]) == (False, None)


async def test_locking_is_not_judged_by_role_and_takes_a_reason_from_the_reference(hub: Hub) -> None:
    """Documented: 422 refuses a reason outside the four. Who may lock is not asked (authorization is out of scope)."""
    async with hub.client() as http:
        assert (await http.put(f"{ISSUES}/1/lock")).status_code == 204
        assert (await http.delete(f"{ISSUES}/1/lock")).status_code == 204
    async with hub.client(TOMAS) as http:
        refusal(await http.put(f"{ISSUES}/1/lock", json={"lock_reason": "boring"}), 422, "Invalid request")


async def test_a_locked_conversation_still_takes_a_comment(hub: Hub) -> None:
    """Authorization is out of scope: who may comment on a locked conversation is not asked."""
    async with hub.client(TOMAS) as http:
        await http.put(f"{ISSUES}/1/lock")
    async with hub.client() as http:
        assert (await http.post(f"{ISSUES}/1/comments", json={"body": "heard"})).status_code == 201


async def test_an_issue_opened_closed_and_reopened_by_the_agent_is_three_moves_of_its_state(hub: Hub) -> None:
    """Every flip by anyone is one transition in the log (`docs/design-transitions.md`); an edit that leaves the state
    alone is none."""
    async with hub.client(TOMAS) as http:
        made = body(await http.post(ISSUES, json={"title": "Moves"}), 201)
        await http.patch(f"{ISSUES}/{made['number']}", json={"title": "Moves, retitled"})
        await http.patch(f"{ISSUES}/{made['number']}", json={"state": "closed"})
        await http.patch(f"{ISSUES}/{made['number']}", json={"state": "open"})
    moves = [(e.actor, e.after) for e in hub.store.events() if isinstance(e.after, TransitionSnapshot)]
    assert [(a, m.name, m.from_state, m.to_state, m.who) for a, m in moves] == [
        (Actor.AGENT, "open", None, "open", None),
        (Actor.AGENT, "close", "open", "closed", None),
        (Actor.AGENT, "reopen", "closed", "open", None),
    ]


async def test_an_assignee_is_any_user_of_this_github_and_no_login_that_is_none(hub: Hub) -> None:
    """Authorization is out of scope: a user with no role on the repository may be assigned; a login naming nobody is
    refused by name."""
    async with hub.client(TOMAS) as http:
        made = body(await http.post(ISSUES, json={"title": "T", "assignees": ["outsider"]}), 201)
        refused = await http.post(ISSUES, json={"title": "T", "assignees": ["nobody-at-all"]})
    assert [a["login"] for a in cast(list[Json], made["assignees"])] == ["outsider"]
    assert refused.status_code == 501
