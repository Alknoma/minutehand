"""Issue comments and labels: written as sent, answered as written, listed in the order GitHub lists them."""

from __future__ import annotations

from datetime import timedelta
from typing import cast
from urllib.parse import quote

import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.domain.world import Actor, Operation
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
LABELS = "/repos/lanternworks/ledger/labels"


@pytest.fixture
def seeded() -> GitHubSeed:
    return tracker_seed()


def names(found: list[Json]) -> list[object]:
    return [label["name"] for label in found]


COMMENT = "Fixed in `retry.py` — see\n\n> quoted\n\n  indented \U0001f600 éè \n"


# ------------------------------------------------------------------------------------------------------ comments


async def test_a_comment_is_kept_and_returned_as_sent(hub: Hub) -> None:
    """Documented: create-an-issue-comment answers 201 with the comment. Data stays as sent.
    https://docs.github.com/en/rest/issues/comments#create-an-issue-comment"""
    async with hub.client() as http:
        made = body(await http.post(f"{ISSUES}/1/comments", json={"body": COMMENT}), 201)
        got = body(await http.get(f"/repos/lanternworks/ledger/issues/comments/{made['id']}"))
        listed = listing(await http.get(f"{ISSUES}/1/comments"))
    assert made["body"] == COMMENT and got == made and listed[-1] == made
    assert cast(Json, made["user"])["login"] == "iris-calder"
    assert made["created_at"] == made["updated_at"] == "2026-08-24T10:50:03Z"
    assert made["issue_url"] == "https://api.github.com/repos/lanternworks/ledger/issues/1"
    assert made["url"] == f"https://api.github.com/repos/lanternworks/ledger/issues/comments/{made['id']}"
    assert made["html_url"] == f"https://github.com/lanternworks/ledger/issues/1#issuecomment-{made['id']}"
    assert made["author_association"] == "MEMBER"


async def test_comments_list_by_ascending_id_and_the_issue_counts_them(hub: Hub) -> None:
    """Documented: "Issue comments are ordered by ascending ID" (list-issue-comments)."""
    async with hub.client() as http:
        first = body(await http.post(f"{ISSUES}/3/comments", json={"body": "one"}), 201)
        second = body(await http.post(f"{ISSUES}/3/comments", json={"body": "two"}), 201)
        listed = listing(await http.get(f"{ISSUES}/3/comments"))
        issue = body(await http.get(f"{ISSUES}/3"))
        seeded = listing(await http.get(f"{ISSUES}/1/comments"))
    assert [c["body"] for c in listed] == ["one", "two"] and cast(int, first["id"]) < cast(int, second["id"])
    assert issue["comments"] == 2
    assert [c["body"] for c in seeded] == ["Looking at it.", "Thanks!"]
    assert [c["id"] for c in seeded] == sorted(cast(list[int], [c["id"] for c in seeded]))


async def test_comments_since_keeps_those_updated_at_or_after_it(hub: Hub) -> None:
    async with hub.client() as http:
        stamp = (START - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = listing(await http.get(f"{ISSUES}/1/comments", params={"since": stamp}))
    assert [c["body"] for c in recent] == ["Thanks!"]


async def test_a_comment_is_edited_and_deleted_and_the_edit_moves_its_update_time(hub: Hub) -> None:
    """Documented: update answers 200 with the comment, delete 204."""
    async with hub.client() as http:
        made = body(await http.post(f"{ISSUES}/1/comments", json={"body": "first"}), 201)
        hub.clock.jump(START + timedelta(minutes=5))
        edited = body(
            await http.patch(f"/repos/lanternworks/ledger/issues/comments/{made['id']}", json={"body": COMMENT})
        )
        gone = await http.delete(f"/repos/lanternworks/ledger/issues/comments/{made['id']}")
        after = await http.get(f"/repos/lanternworks/ledger/issues/comments/{made['id']}")
        count = body(await http.get(f"{ISSUES}/1"))["comments"]
    assert edited["body"] == COMMENT and edited["created_at"] == made["created_at"]
    assert edited["updated_at"] == "2026-08-24T10:55:03Z"
    assert gone.status_code == 204 and gone.content == b""
    refusal(after, 404, "Not Found")
    assert count == 2


@pytest.mark.parametrize(
    "sent", [{}, {"body": None}, {"body": 5}, {"body": ["x"]}], ids=["none", "null", "number", "list"]
)
async def test_a_comment_without_a_text_body_is_422_invalid_request(hub: Hub, sent: Json) -> None:
    async with hub.client() as http:
        refusal(await http.post(f"{ISSUES}/1/comments", json=sent), 422, "Invalid request")


async def test_an_empty_comment_is_kept_as_sent(hub: Hub) -> None:
    async with hub.client() as http:
        assert body(await http.post(f"{ISSUES}/1/comments", json={"body": ""}), 201)["body"] == ""


async def test_a_comment_on_a_missing_issue_and_one_that_is_missing_are_404(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(await http.post(f"{ISSUES}/99/comments", json={"body": "x"}), 404, "Not Found")
        refusal(await http.get(f"{ISSUES}/99/comments"), 404, "Not Found")
        refusal(await http.get("/repos/lanternworks/ledger/issues/comments/7"), 404, "Not Found")
        refusal(await http.patch("/repos/lanternworks/ledger/issues/comments/7", json={"body": "x"}), 404, "Not Found")
        refusal(await http.delete("/repos/lanternworks/ledger/issues/comments/7"), 404, "Not Found")
    async with hub.client(OUTSIDER) as http:
        refusal(await http.post(f"{ISSUES}/1/comments", json={"body": "x"}), 404, "Not Found")


async def test_a_repositorys_comments_list_by_id_or_sorted_with_a_direction(hub: Hub) -> None:
    """Documented: by default ascending ID; `sort` is created or updated and `direction` asc or desc.
    https://docs.github.com/en/rest/issues/comments#list-issue-comments-for-a-repository"""
    async with hub.client() as http:
        await http.post(f"{ISSUES}/3/comments", json={"body": "newest"})
        plain = listing(await http.get("/repos/lanternworks/ledger/issues/comments"))
        newest = listing(
            await http.get(
                "/repos/lanternworks/ledger/issues/comments", params={"sort": "created", "direction": "desc"}
            )
        )
        unsure = await http.get("/repos/lanternworks/ledger/issues/comments", params={"sort": "created"})
    assert [c["body"] for c in plain][-1] == "newest"
    assert newest[0]["body"] == "newest"
    assert unsure.status_code == 501 and "direction" in unsure.json()["message"]


async def test_a_pull_requests_conversation_is_commented_on_through_the_issue_routes(hub: Hub) -> None:
    """Documented: "Every pull request is an issue" (create-an-issue-comment)."""
    async with hub.client() as http:
        made = body(await http.post(f"{ISSUES}/4/comments", json={"body": "looks right"}), 201)
        pull = body(await http.get(f"{ISSUES}/4"))
    assert made["html_url"] == f"https://github.com/lanternworks/ledger/pull/4#issuecomment-{made['id']}"
    assert pull["comments"] == 2 and "pull_request" in pull


async def test_commenting_moves_the_issue_update_time(hub: Hub) -> None:
    """Recorded of the real service: an issue's `updated_at` moves when it is commented on (tests/data/github_rest/)."""
    async with hub.client() as http:
        before = body(await http.get(f"{ISSUES}/3"))["updated_at"]
        hub.clock.jump(START + timedelta(hours=2))
        await http.post(f"{ISSUES}/3/comments", json={"body": "ping"})
        after = body(await http.get(f"{ISSUES}/3"))["updated_at"]
    assert before == "2026-08-23T10:50:03Z" and after == "2026-08-24T12:50:03Z"


async def test_comment_writes_are_the_agents_in_the_log(hub: Hub) -> None:
    async with hub.client() as http:
        made = body(await http.post(f"{ISSUES}/1/comments", json={"body": "logged"}), 201)
        await http.patch(f"/repos/lanternworks/ledger/issues/comments/{made['id']}", json={"body": "logged again"})
        await http.delete(f"/repos/lanternworks/ledger/issues/comments/{made['id']}")
    written = [e for e in hub.store.events() if e.entity.external_id.startswith("comment/") and e.actor is Actor.AGENT]
    assert [e.operation for e in written] == [Operation.CREATE, Operation.UPDATE, Operation.DELETE]


# ------------------------------------------------------------------------------------------------------ labels


async def test_labels_list_alphabetically_by_name_case_aside(hub: Hub) -> None:
    """Recorded of the real service: labels list by name, upper and lower case together (tests/data/github_rest/).
    https://docs.github.com/en/rest/issues/labels#list-labels-for-a-repository"""
    async with hub.client() as http:
        found = listing(await http.get(LABELS))
    assert names(found) == ["alpha", "bug", "docs", "Needs triage"]
    bug = found[1]
    assert (bug["color"], bug["description"], bug["default"]) == ("d73a4a", "Something isn't working", False)
    assert found[0]["default"] is True and found[2]["description"] is None
    assert bug["url"] == "https://api.github.com/repos/lanternworks/ledger/labels/bug"
    assert str(found[3]["url"]).endswith("/labels/Needs%20triage"), "recorded: a space is %20 (tests/data/github_rest/)"


async def test_a_label_is_created_kept_as_sent_and_found_by_name(hub: Hub) -> None:
    """Documented: create-a-label answers 201; the name may hold emoji and spaces. Data stays as sent.
    https://docs.github.com/en/rest/issues/labels#create-a-label"""
    async with hub.client(TOMAS) as http:
        made = body(
            await http.post(
                LABELS, json={"name": "Needs :strawberry: review", "color": "F29513", "description": "Ask Tomas"}
            ),
            201,
        )
        got = body(await http.get(f"{LABELS}/{quote('Needs :strawberry: review')}"))
        listed = names(listing(await http.get(LABELS)))
    assert got == made
    assert (made["name"], made["color"], made["description"], made["default"]) == (
        "Needs :strawberry: review",
        "F29513",
        "Ask Tomas",
        False,
    )
    assert listed == ["alpha", "bug", "docs", "Needs :strawberry: review", "Needs triage"]
    assert str(made["url"]).endswith("/labels/Needs%20:strawberry:%20review"), "recorded: a colon is left as it is"


async def test_a_label_without_a_description_has_none(hub: Hub) -> None:
    async with hub.client() as http:
        assert body(await http.post(LABELS, json={"name": "plain", "color": "ffffff"}), 201)["description"] is None


async def test_a_label_name_that_is_taken_is_422_already_exists(hub: Hub) -> None:
    """Documented: `already_exists` is "another resource has the same value as one of your parameters ... such as label
    names". https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api#validation-failed"""
    async with hub.client() as http:
        refused = refusal(await http.post(LABELS, json={"name": "bug", "color": "ffffff"}), 422, "Validation Failed")
    assert refused["errors"] == [{"resource": "Label", "field": "name", "code": "already_exists"}]


@pytest.mark.parametrize(
    ("sent", "field"),
    [
        ({"name": "x", "color": "zzzzzz"}, "color"),
        ({"name": "x", "color": "fff"}, "color"),
        ({"name": "x", "color": "ffffff", "description": "d" * 101}, "description"),
    ],
)
async def test_a_label_with_a_color_or_description_outside_the_reference_is_422_invalid(
    hub: Hub, sent: Json, field: str
) -> None:
    """Documented: the color is a six digit hexadecimal code, the description 100 characters or fewer."""
    async with hub.client() as http:
        refused = refusal(await http.post(LABELS, json=sent), 422, "Validation Failed")
    assert refused["errors"] == [{"resource": "Label", "field": field, "code": "invalid"}]


async def test_a_label_without_a_name_is_422_invalid_request_and_without_a_color_is_refused_by_name(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(await http.post(LABELS, json={"color": "ffffff"}), 422, "Invalid request")
        refused = await http.post(LABELS, json={"name": "colorless"})
        refusal(await http.get(f"{LABELS}/missing"), 404, "Not Found")
    assert refused.status_code == 501 and "color" in refused.json()["message"]


async def test_labels_are_added_set_listed_and_removed_on_an_issue(hub: Hub) -> None:
    """Documented: add returns the issue's labels, set replaces them, remove-one returns what remains, remove-all is
    204. https://docs.github.com/en/rest/issues/labels"""
    on = f"{ISSUES}/3/labels"
    async with hub.client() as http:
        added = listing(await http.post(on, json={"labels": ["bug"]}))
        more = listing(await http.post(on, json=["docs", "bug"]))
        objects = listing(await http.post(on, json=[{"name": "alpha"}]))
        listed = listing(await http.get(on))
        removed = listing(await http.delete(f"{on}/bug"))
        replaced = listing(await http.put(on, json={"labels": ["Needs triage"]}))
        bare = listing(await http.put(on, json="docs"))
        cleared = await http.delete(on)
        empty = listing(await http.get(on))
    assert names(added) == ["bug"] and names(more) == ["bug", "docs"] and names(objects) == ["bug", "docs", "alpha"]
    assert names(listed) == ["bug", "docs", "alpha"] and names(removed) == ["docs", "alpha"]
    assert names(replaced) == ["Needs triage"] and names(bare) == ["docs"]
    assert cleared.status_code == 204 and empty == []


async def test_a_label_added_to_an_issue_shows_on_the_issue_and_in_the_issue_list_filter(hub: Hub) -> None:
    async with hub.client() as http:
        await http.post(f"{ISSUES}/3/labels", json={"labels": ["docs"]})
        issue = body(await http.get(f"{ISSUES}/3"))
        filtered = listing(await http.get(ISSUES, params={"labels": "docs"}))
    assert names(cast(list[Json], issue["labels"])) == ["docs"]
    assert [i["number"] for i in filtered] == [3]


async def test_removing_a_label_the_issue_does_not_carry_is_404(hub: Hub) -> None:
    """Documented: remove-a-label "returns a `404 Not Found` status if the label does not exist"."""
    async with hub.client() as http:
        refusal(await http.delete(f"{ISSUES}/3/labels/bug"), 404, "Not Found")
        refusal(await http.delete(f"{ISSUES}/99/labels/bug"), 404, "Not Found")


async def test_a_label_the_repository_has_not_defined_is_refused_by_name(hub: Hub) -> None:
    async with hub.client() as http:
        refused = await http.post(f"{ISSUES}/3/labels", json={"labels": ["invented"]})
        suggested = await http.post(f"{ISSUES}/3/labels", json={"labels": [{"name": "bug", "suggest": True}]})
    assert refused.status_code == 501 and "invented" in refused.json()["message"]
    assert suggested.status_code == 501 and "suggest" in suggested.json()["message"]


async def test_label_writes_are_the_agents_in_the_log(hub: Hub) -> None:
    async with hub.client() as http:
        await http.post(LABELS, json={"name": "logged", "color": "ffffff"})
        await http.post(f"{ISSUES}/3/labels", json={"labels": ["logged"]})
    kinds = {
        e.entity.external_id.split("/")[0]
        for e in hub.store.events()
        if e.actor is Actor.AGENT and e.operation is Operation.CREATE
    }
    assert {"label", "counter"} <= kinds
    updated = [e for e in hub.store.events() if e.actor is Actor.AGENT and e.operation is Operation.UPDATE]
    assert any(e.entity.external_id.startswith("issue/") for e in updated)
