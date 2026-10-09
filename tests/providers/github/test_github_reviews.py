"""Reviews of a pull request and the comments on its diff: written as sent, answered as written, on the lines the diff
shows. The pull request is the seeded one: Tomas's, from `timeout`, which changes a line of the configuration and adds
a page."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed, number
from minutehand.domain.world import Actor, TransitionSnapshot
from tests.providers.github.github_world import (
    OUTSIDER,
    START,
    TOMAS,
    Hub,
    Json,
    body,
    listing,
    pulls_seed,
    refusal,
)

PULL = "/repos/lanternworks/ledger/pulls/4"
CONFIG = "services/billing/config.py"
SEEDED_REVIEW = number("review/lanternworks/ledger/4/0")


@pytest.fixture
def seeded() -> GitHubSeed:
    return pulls_seed()


async def head(hub: Hub) -> str:
    async with hub.client() as http:
        return str(cast(Json, body(await http.get(PULL))["head"])["sha"])


# ------------------------------------------------------------------------------------------------------ reviews


async def test_the_reviews_of_a_pull_request_list_oldest_first_as_seeded(hub: Hub) -> None:
    """Documented: "The list of reviews returns in chronological order."
    https://docs.github.com/en/rest/pulls/reviews#list-reviews-for-a-pull-request"""
    tip = await head(hub)
    async with hub.client() as http:
        await http.post(f"{PULL}/reviews", json={"event": "APPROVE"})
        found = listing(await http.get(f"{PULL}/reviews"))
        one = body(await http.get(f"{PULL}/reviews/{SEEDED_REVIEW}"))
    first, second = found
    assert (first["id"], first["state"], first["body"], first["commit_id"]) == (
        SEEDED_REVIEW,
        "COMMENTED",
        "Why sixty?",
        tip,
    )
    assert cast(Json, first["user"])["login"] == "outsider" and first["submitted_at"] == "2026-08-24T09:50:03Z"
    assert second["state"] == "APPROVED" and second["submitted_at"] == "2026-08-24T10:50:03Z"
    assert one == first
    assert first["html_url"] == f"https://github.com/lanternworks/ledger/pull/4#pullrequestreview-{SEEDED_REVIEW}"
    assert first["pull_request_url"] == "https://api.github.com/repos/lanternworks/ledger/pulls/4"
    assert first["_links"] == {
        "html": {"href": first["html_url"]},
        "pull_request": {"href": first["pull_request_url"]},
    }


@pytest.mark.parametrize(
    ("event", "state"),
    [("APPROVE", "APPROVED"), ("REQUEST_CHANGES", "CHANGES_REQUESTED"), ("COMMENT", "COMMENTED")],
)
async def test_a_review_is_submitted_with_the_state_its_event_leaves_it_in(hub: Hub, event: str, state: str) -> None:
    """Documented: create-a-review takes `event` APPROVE, REQUEST_CHANGES or COMMENT and answers 200 with the review.
    The states are GraphQL's `PullRequestReviewState`: APPROVED, CHANGES_REQUESTED, COMMENTED. Data stays as sent."""
    words = "Looks “right”  \n\n- one\n- two \U0001f600\n"
    tip = await head(hub)
    async with hub.client() as http:
        made = body(await http.post(f"{PULL}/reviews", json={"event": event, "body": words}))
        again = body(await http.get(f"{PULL}/reviews/{made['id']}"))
    assert (made["state"], made["body"], made["commit_id"]) == (state, words, tip)
    assert again == made
    assert made["submitted_at"] == "2026-08-24T10:50:03Z" and cast(Json, made["user"])["login"] == "iris-calder"
    assert made["author_association"] == "MEMBER"


async def test_an_approval_needs_no_words_and_the_other_two_do(hub: Hub) -> None:
    """Documented: `body` is "**Required** when using `REQUEST_CHANGES` or `COMMENT`"; omitting a required parameter is
    422 "Invalid request"."""
    async with hub.client() as http:
        approved = body(await http.post(f"{PULL}/reviews", json={"event": "APPROVE"}))
        refusal(await http.post(f"{PULL}/reviews", json={"event": "REQUEST_CHANGES"}), 422, "Invalid request")
        refusal(await http.post(f"{PULL}/reviews", json={"event": "COMMENT", "body": "  "}), 422, "Invalid request")
        refusal(await http.post(f"{PULL}/reviews", json={"event": "LGTM", "body": "x"}), 422, "Invalid request")
    assert approved["body"] == ""


async def test_the_author_may_review_their_own_pull_request(hub: Hub) -> None:
    """Nothing in the reference bars it, and who reviews is not judged (authorization is out of scope)."""
    async with hub.client(TOMAS) as http:
        states = [
            body(await http.post(f"{PULL}/reviews", json={"event": e, "body": "Mine."}))["state"]
            for e in ("COMMENT", "APPROVE", "REQUEST_CHANGES")
        ]
    assert states == ["COMMENTED", "APPROVED", "CHANGES_REQUESTED"]


@pytest.mark.parametrize(
    ("sent", "named"),
    [
        ({"body": "A pending review"}, "PENDING"),
        ({"event": "COMMENT", "body": "x", "comments": [{"path": "a", "body": "b", "line": 1}]}, "comments"),
        ({"event": "COMMENT", "body": "x", "commit_id": "0" * 40}, "other than the pull request's head"),
    ],
    ids=["pending", "draft-comments", "other-commit"],
)
async def test_a_review_the_world_cannot_hold_is_refused_by_name(hub: Hub, sent: Json, named: str) -> None:
    async with hub.client() as http:
        refused = await http.post(f"{PULL}/reviews", json=sent)
    assert refused.status_code == 501 and named in refused.json()["message"]


async def test_an_empty_list_of_draft_comments_is_no_comments_and_the_head_commit_may_be_named(hub: Hub) -> None:
    tip = await head(hub)
    async with hub.client() as http:
        made = body(await http.post(f"{PULL}/reviews", json={"event": "APPROVE", "comments": [], "commit_id": tip}))
    assert made["state"] == "APPROVED"


async def test_a_review_that_is_not_there_and_a_pull_request_that_is_not_one_are_404(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(await http.get(f"{PULL}/reviews/7"), 404, "Not Found")
        refusal(await http.get(f"/repos/lanternworks/ledger/pulls/1/reviews/{SEEDED_REVIEW}"), 404, "Not Found")
        refusal(await http.get("/repos/lanternworks/ledger/pulls/1/reviews"), 404, "Not Found")
        refusal(
            await http.post("/repos/lanternworks/ledger/pulls/1/reviews", json={"event": "APPROVE"}), 404, "Not Found"
        )
    async with hub.client(OUTSIDER) as http:
        refusal(await http.post(f"{PULL}/reviews", json={"event": "APPROVE"}), 404, "Not Found")


async def test_a_review_is_a_move_of_the_pull_requests_state_by_the_agent_and_is_in_the_log(hub: Hub) -> None:
    async with hub.client() as http:
        await http.post(f"{PULL}/reviews", json={"event": "REQUEST_CHANGES", "body": "Needs a test."})
    moves = [(e.actor, e.after) for e in hub.store.events() if isinstance(e.after, TransitionSnapshot)]
    [(actor, move)] = moves
    assert (actor, move.name, move.from_state, move.to_state, move.who) == (
        Actor.AGENT,
        "REQUEST_CHANGES",
        "open",
        "changes requested",
        None,
    )
    written = [e for e in hub.store.events() if e.entity.external_id.startswith("review/") and e.actor is Actor.AGENT]
    assert len(written) == 1 and written[0].after is not None and "Needs a test." in written[0].after.model_dump_json()


# ------------------------------------------------------------------------------------------------------ comments on the diff


async def comment(hub: Hub, sent: Json, status: int = 201) -> Json:
    tip = await head(hub)
    async with hub.client() as http:
        return body(await http.post(f"{PULL}/comments", json={"commit_id": tip, "body": "Why?", **sent}), status)


async def test_a_comment_on_a_line_carries_the_diff_up_to_it(hub: Hub) -> None:
    """Documented: the comment's `diff_hunk` is "The diff of the line that the comment refers to" and `position` counts
    "the number of lines down from the first "@@" hunk header". Recorded: the hunk runs from its header through the
    commented line, and the new side's line is `line` (tests/data/github_rest/)."""
    tip = await head(hub)
    made = await comment(hub, {"path": CONFIG, "line": 1, "side": "RIGHT", "body": "Why sixty?\n\n> quote \U0001f600 "})
    assert made["diff_hunk"] == "@@ -1,2 +1,2 @@\n-PAYMENT_TIMEOUT = 45\n+PAYMENT_TIMEOUT = 60"
    assert (made["path"], made["position"], made["original_position"], made["line"], made["side"]) == (
        CONFIG,
        2,
        2,
        1,
        "RIGHT",
    )
    assert made["body"] == "Why sixty?\n\n> quote \U0001f600 "
    assert (made["commit_id"], made["original_commit_id"]) == (tip, tip)
    assert made["subject_type"] == "line" and made["pull_request_review_id"] is None and made["start_line"] is None
    assert cast(Json, made["user"])["login"] == "iris-calder" and made["author_association"] == "MEMBER"
    assert made["pull_request_url"] == "https://api.github.com/repos/lanternworks/ledger/pulls/4"
    assert made["html_url"] == f"https://github.com/lanternworks/ledger/pull/4#discussion_r{made['id']}"
    assert made["created_at"] == made["updated_at"] == "2026-08-24T10:50:03Z"


async def test_a_comment_names_its_line_by_the_old_side_or_by_position(hub: Hub) -> None:
    old = await comment(hub, {"path": CONFIG, "line": 1, "side": "LEFT"})
    by_position = await comment(hub, {"path": CONFIG, "position": 3})
    added = await comment(hub, {"path": "docs/timeouts.md", "line": 3})
    assert (old["diff_hunk"], old["position"], old["line"], old["side"]) == (
        "@@ -1,2 +1,2 @@\n-PAYMENT_TIMEOUT = 45",
        1,
        1,
        "LEFT",
    )
    assert (
        str(by_position["diff_hunk"]).endswith("\n RETRY_LIMIT = 5"),
        by_position["position"],
        by_position["line"],
    ) == (
        True,
        3,
        2,
    )
    assert added["diff_hunk"] == "@@ -0,0 +1,3 @@\n+# Timeouts\n+\n+The payment timeout is 60 seconds."
    assert (added["position"], added["line"], added["side"]) == (3, 3, "RIGHT")


async def test_a_reply_sits_where_the_comment_it_answers_does(hub: Hub) -> None:
    """Documented: with `in_reply_to`, "all parameters other than `body` in the request body are ignored"."""
    first = await comment(hub, {"path": CONFIG, "line": 1})
    reply = await comment(hub, {"in_reply_to": first["id"], "path": "ignored", "line": 99, "body": "Because."})
    assert reply["in_reply_to_id"] == first["id"] and reply["body"] == "Because."
    assert (reply["path"], reply["diff_hunk"], reply["line"]) == (first["path"], first["diff_hunk"], first["line"])
    assert cast(Json, reply["user"])["login"] == "iris-calder" and reply["id"] != first["id"]
    async with hub.client() as http:
        refusal(
            await http.post(f"{PULL}/comments", json={"commit_id": "x", "path": "a", "body": "b", "in_reply_to": 7}),
            404,
            "Not Found",
        )


async def test_the_comments_of_a_pull_request_list_by_ascending_id_or_sorted(hub: Hub) -> None:
    """Documented: "By default, review comments are in ascending order by ID"; `sort` is created or updated with a
    `direction`. https://docs.github.com/en/rest/pulls/comments#list-review-comments-on-a-pull-request"""
    one = await comment(hub, {"path": CONFIG, "line": 1, "body": "one"})
    hub.clock.jump(START + timedelta(minutes=1))
    two = await comment(hub, {"path": CONFIG, "line": 2, "body": "two"})
    hub.clock.jump(START + timedelta(minutes=2))
    async with hub.client() as http:
        plain = listing(await http.get(f"{PULL}/comments"))
        newest = listing(await http.get(f"{PULL}/comments", params={"sort": "created", "direction": "desc"}))
        since = listing(await http.get(f"{PULL}/comments", params={"since": "2026-08-24T10:51:03Z"}))
        unsure = await http.get(f"{PULL}/comments", params={"sort": "created"})
        pull = body(await http.get(PULL))
    assert [c["body"] for c in plain] == ["one", "two"] and [c["id"] for c in plain] == [one["id"], two["id"]]
    assert [c["body"] for c in newest] == ["two", "one"] and [c["body"] for c in since] == ["two"]
    assert unsure.status_code == 501 and "direction" in unsure.json()["message"]
    assert pull["review_comments"] == 2


async def test_no_comment_belongs_to_a_review_here_and_a_review_lists_none(hub: Hub) -> None:
    async with hub.client() as http:
        listed = listing(await http.get(f"{PULL}/reviews/{SEEDED_REVIEW}/comments"))
        refusal(await http.get(f"{PULL}/reviews/7/comments"), 404, "Not Found")
    assert listed == []


@pytest.mark.parametrize(
    ("sent", "named"),
    [
        ({"path": CONFIG, "line": 1, "start_line": 1}, "start_line"),
        ({"path": CONFIG, "line": 1, "start_side": "RIGHT"}, "start_side"),
        ({"path": CONFIG, "subject_type": "file"}, "whole file"),
        ({"path": "README.md", "line": 1}, "does not change"),
        ({"path": CONFIG, "line": 99}, "does not show"),
        ({"path": CONFIG, "position": 99}, "does not show"),
        ({"path": CONFIG, "line": 1, "commit_id": "0" * 40}, "other than the pull request's head"),
    ],
    ids=[
        "several-lines",
        "start-side",
        "file",
        "unchanged-file",
        "line-not-shown",
        "position-not-shown",
        "other-commit",
    ],
)
async def test_a_comment_on_what_the_diff_does_not_show_or_the_world_cannot_hold_is_refused_by_name(
    hub: Hub, sent: Json, named: str
) -> None:
    tip = await head(hub)
    async with hub.client() as http:
        refused = await http.post(f"{PULL}/comments", json={"commit_id": tip, "body": "Why?", **sent})
    assert refused.status_code == 501 and named in refused.json()["message"]


@pytest.mark.parametrize("missing", ["body", "commit_id", "path"])
async def test_a_comment_missing_what_the_reference_requires_is_422_invalid_request(hub: Hub, missing: str) -> None:
    sent = {"body": "Why?", "commit_id": await head(hub), "path": CONFIG, "line": 1}
    del sent[missing]
    async with hub.client() as http:
        refusal(await http.post(f"{PULL}/comments", json=sent), 422, "Invalid request")


async def test_a_comment_that_names_no_line_is_422_invalid_request(hub: Hub) -> None:
    """Documented: `line` is "**Required unless using `subject_type:file`**", and `position` is the closing-down way."""
    async with hub.client() as http:
        sent = {"body": "Why?", "commit_id": await head(hub), "path": CONFIG}
        refusal(await http.post(f"{PULL}/comments", json=sent), 422, "Invalid request")


async def test_a_comment_on_the_diff_is_the_agents_in_the_log(hub: Hub) -> None:
    await comment(hub, {"path": CONFIG, "line": 1})
    written = [e for e in hub.store.events() if e.entity.external_id.startswith("rcomment/") and e.actor is Actor.AGENT]
    assert len(written) == 1 and written[0].after is not None and "Why?" in written[0].after.model_dump_json()
