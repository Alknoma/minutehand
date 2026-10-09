"""The webhooks GitHub sends the agent for what people do: closing or reopening an issue, commenting, closing or merging
a pull request, reviewing it. They go to the target the agent declares for GitHub, with the headers the reference lists,
signed only where the world declares a secret, in bodies that hold what the description requires and what the REST
routes answer."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest

from minutehand.adapters.providers.github.app import GitHubApp
from minutehand.adapters.providers.github.hooks import DeliveryRefused
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.adapters.providers.github.transitions import GitHubTransitions
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.common import GeneratedSecret
from minutehand.domain.people import Delivery as DeliveryKind
from minutehand.domain.people import InboundTarget
from minutehand.domain.world import Actor, EntityKind, EntityRef
from tests.providers.github.github_world import IRIS as IRIS_TOKEN
from tests.providers.github.github_world import PEOPLE, SCENARIO, START, pulls_seed
from tests.providers.github.hook_receiver import Receiver, receiver
from tests.providers.github.schema import missing

IRIS, TOMAS = PEOPLE
SECRET = "the-signing-secret"
DESCRIPTION = json.loads((Path(__file__).parent / "openapi" / "api.github.com.webhooks.subset.json").read_text())


def item(number: int) -> EntityRef:
    return EntityRef(provider="github", kind=EntityKind.RECORD, external_id=f"issue/lanternworks/ledger/{number:010d}")


def world(
    tmp_path: Path, target: InboundTarget | None, secret: str | None = SECRET, run: str = "w"
) -> tuple[GitHubTransitions, SqliteStore, RunClock]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", run, clock)
    provider = build()
    provider.seed_with(pulls_seed(), SCENARIO, store)
    return provider.talking(target, secret), store, clock


def target(on: Receiver, *, signed: bool = True) -> InboundTarget:
    return InboundTarget(
        provider="github", url=on.url, secret=GeneratedSecret(env="GITHUB_WEBHOOK_SECRET") if signed else None
    )


def checked(event: str, payload: dict[str, object], name: str) -> None:
    """The body holds every field the description requires of that event."""
    schema = DESCRIPTION["x-webhooks"][name]["post"]["requestBody"]["content"]["application/json"]["schema"]
    gaps = missing(DESCRIPTION["components"], schema, payload, "$")
    assert gaps == [], f"{event}: {' '.join(gaps)}"


# ------------------------------------------------------------------------------------------------------ the headers


async def test_a_delivery_carries_the_headers_the_reference_lists_and_a_signature_that_verifies(tmp_path: Path) -> None:
    """Documented: `X-GitHub-Event`, `X-GitHub-Delivery` (a GUID), `User-Agent` with the prefix `GitHub-Hookshot/`,
    `Content-Type`, and, "if the webhook is configured with a secret", `X-Hub-Signature-256` (the HMAC hex digest of the
    body under SHA-256) and `X-Hub-Signature` (under SHA-1).
    https://docs.github.com/en/webhooks/webhook-events-and-payloads#delivery-headers"""
    with receiver() as agent:
        port, store, clock = world(tmp_path, target(agent))
        await port.apply(item(3), "close", Actor.PERSON, TOMAS, json.dumps({"comment": "Done."}), store, clock)
    assert [d.event for d in agent.pushed] == ["issue_comment", "issues"]
    seen: set[str] = set()
    for pushed in agent.pushed:
        headers = pushed.headers
        assert headers["user-agent"].startswith("GitHub-Hookshot/") and headers["content-type"] == "application/json"
        assert str(uuid.UUID(headers["x-github-delivery"])) == headers["x-github-delivery"]
        seen.add(headers["x-github-delivery"])
        assert (
            headers["x-hub-signature-256"]
            == "sha256=" + hmac.new(SECRET.encode(), pushed.body, hashlib.sha256).hexdigest()
        )
        assert headers["x-hub-signature"] == "sha1=" + hmac.new(SECRET.encode(), pushed.body, hashlib.sha1).hexdigest()
        assert "x-github-hook-id" not in headers, "no webhook is configured in the world to have an id"
    assert len(seen) == 2, "each delivery has a GUID of its own"


async def test_a_target_the_world_declares_no_secret_for_is_sent_no_signature(tmp_path: Path) -> None:
    """Documented: the signature headers are sent "if the webhook is configured with a secret"; the secret the run keeps
    for a target that names none is given to no one and signs nothing."""
    with receiver() as agent:
        port, store, clock = world(tmp_path, target(agent, signed=False), "never-given-out")
        await port.apply(item(3), "close", Actor.PERSON, TOMAS, "{}", store, clock)
    [pushed] = agent.pushed
    assert "x-hub-signature-256" not in pushed.headers and "x-hub-signature" not in pushed.headers
    assert pushed.event == "issues"


async def test_an_agent_with_no_target_for_github_is_pushed_nothing_and_hears_nothing(tmp_path: Path) -> None:
    port, store, clock = world(tmp_path, None, None)
    await port.apply(item(3), "close", Actor.PERSON, TOMAS, "{}", store, clock)
    assert port.heard_of(item(3), TOMAS, store, clock) is False
    with receiver() as agent:
        listening, _, _ = world(tmp_path / "other", target(agent))
        assert listening.heard_of(item(3), TOMAS, store, clock) is True


async def test_the_deliveries_of_a_run_have_guids_no_two_worlds_share(tmp_path: Path) -> None:
    ids: list[str] = []
    for n in range(2):
        with receiver() as agent:
            port, store, clock = world(tmp_path / str(n), target(agent), run=f"run-{n}")
            await port.apply(item(3), "close", Actor.PERSON, TOMAS, "{}", store, clock)
            ids += [d.headers["x-github-delivery"] for d in agent.pushed]
    assert len(set(ids)) == 2


async def test_a_target_taken_in_another_way_than_a_url_is_refused(tmp_path: Path) -> None:
    socket = InboundTarget(provider="github", delivery=DeliveryKind.SOCKET_MODE)
    with pytest.raises(ValueError, match="pushes a webhook to a URL"):
        build().talking(socket, SECRET)


@pytest.mark.parametrize("status", [500, 404, 302])
async def test_an_agent_that_answers_a_delivery_with_anything_but_2xx_has_failed_it(
    tmp_path: Path, status: int
) -> None:
    """Documented: GitHub expects a 2xx and "does not automatically redeliver failed deliveries"."""
    with receiver(status) as agent:
        port, store, clock = world(tmp_path, target(agent))
        with pytest.raises(DeliveryRefused, match=f"answered {status}"):
            await port.apply(item(3), "close", Actor.PERSON, TOMAS, "{}", store, clock)
        assert len(agent.pushed) == 1, "sent once"


async def test_an_agent_that_cannot_be_reached_has_failed_the_delivery(tmp_path: Path) -> None:
    with receiver() as agent:
        gone = target(agent)
    port, store, clock = world(tmp_path, gone)
    with pytest.raises(DeliveryRefused, match="could not be reached"):
        await port.apply(item(3), "close", Actor.PERSON, TOMAS, "{}", store, clock)


# ------------------------------------------------------------------------------------------------------ the events


async def test_closing_and_reopening_an_issue_are_issues_events_that_say_what_the_rest_route_says(
    tmp_path: Path,
) -> None:
    with receiver() as agent:
        port, store, clock = world(tmp_path, target(agent))
        clock.jump(START + timedelta(hours=1))
        await port.apply(
            item(3), "close", Actor.PERSON, TOMAS, json.dumps({"state_reason": "not_planned"}), store, clock
        )
        await port.apply(item(3), "reopen", Actor.PERSON, TOMAS, "{}", store, clock)
    closed, reopened = agent.pushed
    assert (closed.payload()["action"], reopened.payload()["action"]) == ("closed", "reopened")
    checked("issues closed", closed.payload(), "issues-closed")
    checked("issues reopened", reopened.payload(), "issues-reopened")
    issue = cast(dict[str, object], closed.payload()["issue"])
    assert (issue["state"], issue["state_reason"], issue["closed_at"]) == (
        "closed",
        "not_planned",
        "2026-08-24T11:50:03Z",
    )
    assert issue["performed_via_github_app"] is None and cast(dict[str, object], issue["reactions"])["total_count"] == 0
    assert cast(dict[str, object], closed.payload()["sender"])["login"] == "tomas-b"
    assert cast(dict[str, object], closed.payload()["repository"])["full_name"] == "lanternworks/ledger"
    assert cast(dict[str, object], reopened.payload()["issue"])["state"] == "open"


async def test_a_comment_is_an_issue_comment_event_before_the_close_it_comes_with(tmp_path: Path) -> None:
    with receiver() as agent:
        port, store, clock = world(tmp_path, target(agent))
        await port.apply(
            item(3), "close", Actor.PERSON, TOMAS, json.dumps({"comment": "Closing: “done” \U0001f600"}), store, clock
        )
    commented, closed = agent.pushed
    assert (commented.event, closed.event) == ("issue_comment", "issues")
    checked("issue_comment created", commented.payload(), "issue-comment-created")
    payload = commented.payload()
    comment = cast(dict[str, object], payload["comment"])
    assert payload["action"] == "created" and comment["body"] == "Closing: “done” \U0001f600"
    assert (
        cast(dict[str, object], comment["user"])["login"] == "tomas-b" and comment["performed_via_github_app"] is None
    )
    assert cast(dict[str, object], payload["issue"])["comments"] == 1


async def test_a_review_is_a_pull_request_review_event(tmp_path: Path) -> None:
    with receiver() as agent:
        port, store, clock = world(tmp_path, target(agent))
        await port.apply(
            item(4), "REQUEST_CHANGES", Actor.PERSON, IRIS, json.dumps({"body": "Needs a test."}), store, clock
        )
    [pushed] = agent.pushed
    checked("pull_request_review submitted", pushed.payload(), "pull-request-review-submitted")
    payload = pushed.payload()
    review = cast(dict[str, object], payload["review"])
    assert (pushed.event, payload["action"]) == ("pull_request_review", "submitted")
    assert (review["state"], review["body"]) == ("CHANGES_REQUESTED", "Needs a test.")
    assert cast(dict[str, object], review["user"])["login"] == "iris-calder"
    assert cast(dict[str, object], payload["pull_request"])["number"] == 4
    assert cast(dict[str, object], payload["sender"])["login"] == "iris-calder"


async def test_closing_a_pull_request_is_a_pull_request_event_that_is_not_merged(tmp_path: Path) -> None:
    with receiver() as agent:
        port, store, clock = world(tmp_path, target(agent))
        await port.apply(item(4), "close", Actor.PERSON, TOMAS, json.dumps({"comment": "Not now."}), store, clock)
    commented, closed = agent.pushed
    assert (commented.event, closed.event) == ("issue_comment", "pull_request")
    checked("pull_request closed", closed.payload(), "pull-request-closed")
    pull = cast(dict[str, object], closed.payload()["pull_request"])
    assert closed.payload()["action"] == "closed" and closed.payload()["number"] == 4
    assert (pull["state"], pull["merged"], pull["merged_at"]) == ("closed", False, None)


async def test_merging_a_pull_request_is_a_pull_request_event_that_is_merged(tmp_path: Path) -> None:
    with receiver() as agent:
        port, store, clock = world(tmp_path, target(agent))
        clock.jump(START + timedelta(hours=1))
        moved = await port.apply(
            item(4), "merge", Actor.PERSON, TOMAS, json.dumps({"commit_title": "Ship it"}), store, clock
        )
    [pushed] = agent.pushed
    checked("pull_request closed (merged)", pushed.payload(), "pull-request-closed")
    pull = cast(dict[str, object], pushed.payload()["pull_request"])
    assert (pushed.event, pushed.payload()["action"]) == ("pull_request", "closed")
    assert (pull["merged"], pull["merged_at"], pull["state"]) == (True, "2026-08-24T11:50:03Z", "closed")
    assert cast(dict[str, object], pull["merged_by"])["login"] == "tomas-b"
    assert (moved.name, moved.to_state, moved.who) == ("merge", "merged", "tomas")
    repository = GitHubWorld(store).repository("lanternworks", "ledger")
    assert repository is not None and pull["merge_commit_sha"] == repository.commits[0].sha


async def test_what_a_webhook_carries_is_what_the_rest_route_answers_of_it(tmp_path: Path) -> None:
    """A push and a read of the same thing agree: the issue in the body is the issue the route gives, beside the two keys
    the webhook's description requires and the route's does not."""
    import httpx

    from minutehand.adapters.providers.github.app import build_app

    with receiver() as agent:
        port, store, clock = world(tmp_path, target(agent))
        await port.apply(item(3), "close", Actor.PERSON, TOMAS, "{}", store, clock)
    [pushed] = agent.pushed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(store, clock)), base_url="https://api.github.com"
    ) as http:
        read = (await http.get("/repos/lanternworks/ledger/issues/3", headers={"Authorization": "Bearer x"})).json()
    carried = cast(dict[str, object], pushed.payload()["issue"])
    assert {k: v for k, v in carried.items() if k not in ("performed_via_github_app", "reactions")} == read


# ------------------------------------------------------------------------------------------------------ the agent's own


def listening(tmp_path: Path, on: InboundTarget | None, secret: str | None = SECRET) -> tuple[GitHubApp, SqliteStore]:
    """The provider told where the agent takes GitHub's webhooks (`ListensForAgent`), and its app."""
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "agent.db", "agent", clock)
    provider = build()
    provider.seed_with(pulls_seed(), SCENARIO, store)
    provider.listen(on, secret)
    app = provider.app(store, clock)
    assert isinstance(app, GitHubApp)
    return app, store


def agent_client(app: GitHubApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {IRIS_TOKEN}", "X-GitHub-Api-Version": "2022-11-28"},
    )


async def test_the_agents_own_writes_are_pushed_as_the_webhooks_github_sends_after_each_call(tmp_path: Path) -> None:
    """Documented: GitHub sends `issues` (`opened`, `closed`), `issue_comment` (`created`), `pull_request` (`opened`,
    `closed` with `merged`) and `pull_request_review` (`submitted`) for what happens on the repository, whoever does it;
    `sender` is "the user that triggered the event".
    https://docs.github.com/en/webhooks/webhook-events-and-payloads"""
    with receiver() as agent:
        app, _ = listening(tmp_path, target(agent))
        async with agent_client(app) as http:
            opened = await http.post("/repos/lanternworks/ledger/issues", json={"title": "Check the ledger"})
            number = opened.json()["number"]
            assert opened.status_code == 201
            await http.post(f"/repos/lanternworks/ledger/issues/{number}/comments", json={"body": "Looking."})
            await http.patch(f"/repos/lanternworks/ledger/issues/{number}", json={"state": "closed"})
            pulled = await http.post(
                "/repos/lanternworks/ledger/pulls", json={"title": "Notes", "head": "notes", "base": "main"}
            )
            assert pulled.status_code == 201
            await http.post("/repos/lanternworks/ledger/pulls/4/reviews", json={"event": "APPROVE"})
            assert (await http.put("/repos/lanternworks/ledger/pulls/4/merge", json={})).status_code == 200
        await app.settled()
    assert [(d.event, d.payload()["action"]) for d in agent.pushed] == [
        ("issues", "opened"),
        ("issue_comment", "created"),
        ("issues", "closed"),
        ("pull_request", "opened"),
        ("pull_request_review", "submitted"),
        ("pull_request", "closed"),
    ]
    names = [
        "issues-opened",
        "issue-comment-created",
        "issues-closed",
        "pull-request-opened",
        "pull-request-review-submitted",
        "pull-request-closed",
    ]
    for pushed, name in zip(agent.pushed, names, strict=True):
        checked(name, pushed.payload(), name)
        assert cast(dict[str, object], pushed.payload()["sender"])["login"] == "iris-calder"
        assert (
            pushed.headers["x-hub-signature-256"]
            == "sha256=" + hmac.new(SECRET.encode(), pushed.body, hashlib.sha256).hexdigest()
        )
    assert cast(dict[str, object], agent.pushed[0].payload()["issue"])["number"] == number
    assert cast(dict[str, object], agent.pushed[-1].payload()["pull_request"])["merged"] is True
    assert len({d.headers["x-github-delivery"] for d in agent.pushed}) == len(agent.pushed)


async def test_an_agent_that_declares_no_target_for_github_is_pushed_nothing_by_its_own_writes(tmp_path: Path) -> None:
    app, _ = listening(tmp_path, None, None)
    async with agent_client(app) as http:
        assert (await http.post("/repos/lanternworks/ledger/issues", json={"title": "Quiet"})).status_code == 201
    assert app.delivering() == 0 and app.background.refused == []


async def test_a_webhook_the_agent_refuses_for_its_own_write_is_kept_and_its_call_is_still_answered(
    tmp_path: Path,
) -> None:
    """The call is answered before its webhook is sent, as GitHub sends a delivery apart from the call that set it off;
    a delivery the agent does not take is logged and kept, not retried ("does not automatically redeliver")."""
    with receiver(500) as agent:
        app, _ = listening(tmp_path, target(agent))
        async with agent_client(app) as http:
            answered = await http.post("/repos/lanternworks/ledger/issues", json={"title": "Refused"})
        await app.settled()
    assert answered.status_code == 201
    assert len(agent.pushed) == 1
    [refused] = app.background.refused
    assert refused.startswith("issues ") and "answered 500" in refused


async def test_what_the_agents_calls_set_off_is_delivered_one_after_another_in_the_order_made(tmp_path: Path) -> None:
    """A delivery is sent once the one before it is answered: an agent slow to answer the first is not sent the second
    beside it."""
    with receiver(hold=0.3) as agent:
        app, _ = listening(tmp_path, target(agent))
        async with agent_client(app) as http:
            first = (await http.post("/repos/lanternworks/ledger/issues", json={"title": "First"})).json()["number"]
            second = (await http.post("/repos/lanternworks/ledger/issues", json={"title": "Second"})).json()["number"]
        await app.settled()
    assert [cast(dict[str, object], d.payload()["issue"])["number"] for d in agent.pushed] == [first, second]
