"""A code-reading client's whole conversation, call for call as it makes them: validate the token, then answer a
question by overview, tree, listing, search, a batch of files, history and branches. Nothing it does writes; the
only changes are the world's own bookkeeping of the user's rate-limit budgets."""

from __future__ import annotations

from minutehand.domain.world import Actor, Operation
from tests.providers.github.github_world import RETRY, TOMAS, Hub, body, listing
from tests.providers.github.test_github_graphql import BRANCHES, FILE_BATCH, OVERVIEW


async def test_a_token_is_validated_and_a_question_answered_from_the_repository(hub: Hub) -> None:
    before = hub.store.head()
    async with hub.client(TOMAS) as http:
        # validate-token: who the token is, and which of the named repositories its user reaches (the token's own
        # selection of repositories is not enforced: README, "Credentials")
        assert body(await http.get("/user"))["login"] == "tomas-b"
        reached = [await http.get(f"/repos/{name}") for name in ("lanternworks/ledger", "lanternworks/plans")]
        assert [r.status_code for r in reached] == [200, 200]

        # the question
        overview = body(
            await http.post(
                "/graphql", json={"query": OVERVIEW, "variables": {"owner": "lanternworks", "repo": "ledger"}}
            )
        )
        head = listing(await http.get("/repos/lanternworks/ledger/commits", params={"per_page": 1}))[0]["sha"]
        tree = body(await http.get(f"/repos/lanternworks/ledger/git/trees/{head}", params={"recursive": "1"}))
        billing = listing(await http.get("/repos/lanternworks/ledger/contents/services/billing"))
        found = body(
            await http.get(
                "/search/code",
                params={"q": "retry_with_backoff repo:lanternworks/ledger language:python", "per_page": 30},
            )
        )
        files = body(await http.post("/graphql", json={"query": FILE_BATCH}))
        history = listing(
            await http.get(
                "/repos/lanternworks/ledger/commits",
                params={"sha": "HEAD", "per_page": 20, "path": "services/billing/retry.py"},
            )
        )
        branches = body(
            await http.post(
                "/graphql",
                json={"query": BRANCHES, "variables": {"owner": "lanternworks", "repo": "ledger", "first": 30}},
            )
        )

    assert overview["data"]["repository"]["primaryLanguage"] == {"name": "Python"}  # type: ignore[index]
    assert tree["truncated"] is False
    assert [e["name"] for e in billing] == ["config.py", "retry.py"]
    assert [i["path"] for i in found["items"]] == ["services/billing/retry.py"]  # type: ignore[union-attr]
    assert files["data"]["repository"]["file_0"]["text"] == RETRY  # type: ignore[index]
    assert [c["commit"]["message"] for c in history] == ["Retry billing calls\n\nWith a doubling wait."]  # type: ignore[index]
    assert len(branches["data"]["repository"]["refs"]["nodes"]) == 2  # type: ignore[index]

    events = hub.store.events(since=before)
    acts = [e for e in events if e.actor is Actor.AGENT]
    kept = [e for e in events if e.actor is not Actor.AGENT]
    assert {e.operation for e in acts} == {Operation.READ, Operation.SEARCH}
    assert all(e.after is None for e in acts)
    assert {e.entity.external_id for e in kept} == {
        "budget/tomas-b/core",
        "budget/tomas-b/graphql",
        "budget/tomas-b/code_search",
    }
    assert {e.actor for e in kept} == {Actor.SCENARIO}
    calls = hub.store.calls()
    assert [c.exchange.status for c in calls] == [200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200]
