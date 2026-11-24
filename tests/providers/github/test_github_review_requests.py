"""Asking people to review a pull request, as an agent's coding helper does through the REST API: the review is asked
of a collaborator, never of the pull request's author; it makes the pull request wait on that person until they give
it, and GitHub tells the agent of each request as a `pull_request` webhook. The pull request is Tomas's, from
`timeout`, asking nobody's review at the start."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import httpx
import pytest

from minutehand.adapters.providers.github import state
from minutehand.adapters.providers.github.app import GitHubApp
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.common import GeneratedSecret
from minutehand.domain.people import InboundTarget
from tests.providers.github.github_world import (
    IRIS,
    OUTSIDER,
    PEOPLE,
    SCENARIO,
    START,
    TOMAS,
    Hub,
    Json,
    body,
    pulls_seed,
    refusal,
    validated,
)
from tests.providers.github.hook_receiver import receiver
from tests.providers.github.schema import missing

PULL = "/repos/lanternworks/ledger/pulls/4"
ASKED = f"{PULL}/requested_reviewers"
IRIS_PERSON = PEOPLE[0]
AUTHOR_ASKED = "Review cannot be requested from pull request author."
REQUEST_DOCS = "https://docs.github.com/rest/pulls/review-requests#request-reviewers-for-a-pull-request"
HOOKS = json.loads((Path(__file__).parent / "openapi" / "api.github.com.webhooks.subset.json").read_text())


def unasked() -> GitHubSeed:
    """The pulls' GitHub with Tomas's pull request asking nobody's review and assigned to nobody."""
    seed = pulls_seed()
    ledger = seed.repositories[0]
    pull = ledger.pulls[0].model_copy(update={"requested_reviewers": [], "assignees": []})
    mine = ledger.model_copy(update={"pulls": [pull]})
    return validated(seed.model_copy(update={"repositories": [mine, *seed.repositories[1:]]}))


@pytest.fixture
def seeded() -> GitHubSeed:
    return unasked()


def logins(found: object) -> list[object]:
    return [u["login"] for u in cast(list[Json], found)]


def waits_on_iris(hub: Hub) -> str | None:
    """What the pull request waits on Iris for, through the port people act through; None when it does not."""
    port = hub.provider.talking(None, None)
    pull = state.issue_ref("lanternworks", "ledger", 4)
    found = [w for w in port.items_for(IRIS_PERSON, hub.store) if w.item == pull]
    return found[0].state if found else None


# ------------------------------------------------------------------------------------------------------ asking


async def test_a_review_asked_of_a_collaborator_is_listed_and_answered_with_the_pull_request(hub: Hub) -> None:
    """Documented: request-reviewers answers 201 with the pull request (the description's `pull-request-simple`), and
    the requested reviewers list `users` and `teams`. https://docs.github.com/en/rest/pulls/review-requests"""
    async with hub.client(TOMAS) as http:
        before = body(await http.get(ASKED))
        asked = body(await http.post(ASKED, json={"reviewers": ["iris-calder"]}), 201)
        again = body(await http.post(ASKED, json={"reviewers": ["iris-calder"], "team_reviewers": []}), 201)
        after = body(await http.get(ASKED))
        pull = body(await http.get(PULL))
    assert before == {"users": [], "teams": []}
    assert logins(asked["requested_reviewers"]) == ["iris-calder"] and "mergeable" not in asked, "the simple form"
    assert logins(again["requested_reviewers"]) == ["iris-calder"], "asked twice, asked once"
    assert logins(after["users"]) == ["iris-calder"] and after["teams"] == []
    assert logins(pull["requested_reviewers"]) == ["iris-calder"]


async def test_a_review_asked_of_the_pull_requests_author_is_refused_422(hub: Hub) -> None:
    """Observed: "Review cannot be requested from pull request author." (422).
    https://github.com/github/github-mcp-server/issues/3756"""
    async with hub.client(TOMAS) as http:
        refused = refusal(await http.post(ASKED, json={"reviewers": ["tomas-b"]}), 422, AUTHOR_ASKED)
        after = body(await http.get(ASKED))
    assert refused["documentation_url"] == REQUEST_DOCS
    assert after["users"] == []


async def test_a_review_asked_of_someone_who_is_no_collaborator_is_refused_422(hub: Hub) -> None:
    """Documented: 422 "if user is not a collaborator"; observed, in these words
    (https://github.com/Automattic/jetpack/issues/37058). The visiting contractor has no role on the ledger."""
    async with hub.client(TOMAS) as http:
        refusal(
            await http.post(ASKED, json={"reviewers": ["iris-calder", "outsider"]}),
            422,
            "Reviews may only be requested from collaborators. One or more of the users or teams you specified is not "
            "a collaborator of the lanternworks/ledger repository.",
        )
        after = body(await http.get(ASKED))
    assert after["users"] == [], "nobody is asked when one of them is refused"


async def test_a_request_naming_nobody_is_422_and_a_team_or_a_login_of_nobody_is_refused_by_name(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        refusal(await http.post(ASKED, json={}), 422, "Invalid request")
        refusal(await http.request("DELETE", ASKED, json={"team_reviewers": []}), 422, "Invalid request")
        team = await http.post(ASKED, json={"team_reviewers": ["billing"]})
        nobody = await http.post(ASKED, json={"reviewers": ["nobody-at-all"]})
        not_asked = await http.request("DELETE", ASKED, json={"reviewers": ["iris-calder"]})
    for refused, named in ((team, "team_reviewers"), (nobody, "nobody-at-all"), (not_asked, "iris-calder")):
        assert refused.status_code == 501 and named in refused.json()["message"], refused.text


# ------------------------------------------------------------------------------------------------------ waiting


async def test_a_review_asked_makes_the_pull_request_wait_on_that_person_until_they_give_it(hub: Hub) -> None:
    """Documented: "Once a requested reviewer submits a review, they are no longer considered a requested reviewer";
    after changes, a review can be asked of the same reviewer again.
    https://docs.github.com/en/rest/pulls/review-requests#get-all-requested-reviewers-for-a-pull-request,
    https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/requesting-a-pull-request-review"""
    assert waits_on_iris(hub) is None
    async with hub.client(TOMAS) as http:
        body(await http.post(ASKED, json={"reviewers": ["iris-calder"]}), 201)
    asked = waits_on_iris(hub)
    async with hub.client(IRIS) as http:
        body(await http.post(f"{PULL}/reviews", json={"event": "APPROVE"}))
        reviewed = body(await http.get(ASKED))
    answered = waits_on_iris(hub)
    async with hub.client(TOMAS) as http:
        body(await http.post(ASKED, json={"reviewers": ["iris-calder"]}), 201)
    asked_again = waits_on_iris(hub)
    assert (asked, answered, asked_again) == ("review requested", None, "review requested")
    assert reviewed["users"] == [], "her review answered the request"


async def test_a_request_taken_back_asks_the_reviewer_no_more(hub: Hub) -> None:
    """Documented: remove-requested-reviewers takes `reviewers` and answers 200 with the pull request."""
    async with hub.client(TOMAS) as http:
        body(await http.post(ASKED, json={"reviewers": ["iris-calder"]}), 201)
        removed = body(await http.request("DELETE", ASKED, json={"reviewers": ["iris-calder"]}))
        after = body(await http.get(ASKED))
    assert removed["requested_reviewers"] == [] and after["users"] == []
    assert waits_on_iris(hub) is None


# ------------------------------------------------------------------------------------------------------ webhooks


def checked(payload: dict[str, object], name: str) -> None:
    """The body holds every field the description requires of that event."""
    schema = HOOKS["x-webhooks"][name]["post"]["requestBody"]["content"]["application/json"]["schema"]
    gaps = missing(HOOKS["components"], schema, payload, "$")
    assert gaps == [], f"{name}: {' '.join(gaps)}"


def listening(tmp_path: Path, url: str) -> GitHubApp:
    """The app of a GitHub whose agent takes its webhooks at `url`, signed with a secret the world declares."""
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "agent.db", "agent", clock)
    provider = build()
    provider.seed_with(unasked(), SCENARIO, store)
    target = InboundTarget(provider="github", url=url, secret=GeneratedSecret(env="GITHUB_WEBHOOK_SECRET"))
    provider.listen(target, "the-signing-secret")
    app = provider.app(store, clock)
    assert isinstance(app, GitHubApp)
    return app


async def test_asking_and_taking_back_a_review_are_pull_request_events_naming_the_reviewer(tmp_path: Path) -> None:
    """Documented: `pull_request` with `review_requested` ("Review by a person or team was requested for a pull
    request") and `review_request_removed`, each naming the `requested_reviewer`. A user asked already is not asked
    again, and nothing is sent for it. https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request"""
    with receiver() as agent:
        app = listening(tmp_path, agent.url)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {TOMAS}", "X-GitHub-Api-Version": "2022-11-28"},
        ) as http:
            assert (await http.post(ASKED, json={"reviewers": ["iris-calder"]})).status_code == 201
            assert (await http.post(ASKED, json={"reviewers": ["iris-calder"]})).status_code == 201
            assert (await http.request("DELETE", ASKED, json={"reviewers": ["iris-calder"]})).status_code == 200
        await app.settled()
    assert [(d.event, d.payload()["action"]) for d in agent.pushed] == [
        ("pull_request", "review_requested"),
        ("pull_request", "review_request_removed"),
    ]
    asked, removed = [d.payload() for d in agent.pushed]
    checked(asked, "pull-request-review-requested")
    checked(removed, "pull-request-review-request-removed")
    for payload in (asked, removed):
        assert payload["number"] == 4
        assert cast(Json, payload["requested_reviewer"])["login"] == "iris-calder"
        assert cast(Json, payload["sender"])["login"] == "tomas-b"
    assert logins(cast(Json, asked["pull_request"])["requested_reviewers"]) == ["iris-calder"]
    assert logins(cast(Json, removed["pull_request"])["requested_reviewers"]) == []


async def test_a_stranger_cannot_ask_a_review_of_a_repository_it_cannot_see(hub: Hub) -> None:
    async with hub.client(OUTSIDER) as http:
        refusal(await http.post(ASKED, json={"reviewers": ["iris-calder"]}), 404, "Not Found")
        refusal(await http.get(ASKED), 404, "Not Found")
