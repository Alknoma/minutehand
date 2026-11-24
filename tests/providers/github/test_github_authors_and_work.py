"""People carrying their work forward on GitHub, through the one port people act through
(`docs/design-transitions.md`): an issue assigned to a person is work they carry; a pull request is an ask, and waits
on its author while it is a draft (marked ready for review only where a take has them do it) and once someone else
approves it, nobody asks for changes and it merges cleanly, never while it only waits for review. The pull request is
Tomas's, from `timeout`, asking Iris's review."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.github import state, wire
from minutehand.adapters.providers.github.app import build_app
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.adapters.providers.github.transitions import GitHubTransitions
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.common import GeneratedSecret
from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Person, Scenario, Take, TicketState
from minutehand.domain.world import Actor, EntityRef
from minutehand.ports.transitions import CarriesWork
from tests.providers.github.github_world import IRIS as IRIS_TOKEN
from tests.providers.github.github_world import PEOPLE, SCENARIO, START, pulls_seed, validated
from tests.providers.github.hook_receiver import receiver
from tests.providers.github.schema import missing
from tests.support.people import people_engine, people_model

IRIS, TOMAS = PEOPLE
HOOKS = json.loads((Path(__file__).parent / "openapi" / "api.github.com.webhooks.subset.json").read_text())


def item(number: int) -> EntityRef:
    return state.issue_ref("lanternworks", "ledger", number)


def with_ledger(pull: dict[str, object], issue_3: dict[str, object] | None = None) -> GitHubSeed:
    """The pulls' GitHub with Tomas's pull request changed by `pull` and issue 3, Tomas's, by `issue_3`."""
    seed = pulls_seed()
    ledger = seed.repositories[0]
    issues = [i.model_copy(update=issue_3) if i.number == 3 and issue_3 else i for i in ledger.issues]
    changed = ledger.model_copy(update={"pulls": [ledger.pulls[0].model_copy(update=pull)], "issues": issues})
    return validated(seed.model_copy(update={"repositories": [changed, *seed.repositories[1:]]}))


def world(
    tmp_path: Path, seed: GitHubSeed, scenario: Scenario = SCENARIO, target: InboundTarget | None = None
) -> tuple[GitHubTransitions, SqliteStore, RunClock]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = build()
    provider.seed_with(seed, scenario, store)
    return provider.talking(target, None if target is None else "the-signing-secret"), store, clock


def waits(port: GitHubTransitions, person: Person, store: SqliteStore) -> dict[EntityRef, str]:
    """What waits on the person, each item with the state it waits in."""
    return {w.item: w.state for w in port.items_for(person, store)}


def pull_of(store: SqliteStore) -> wire.StoredPull:
    github = GitHubWorld(store)
    repository = github.repository("lanternworks", "ledger")
    issue = None if repository is None else github.issue(repository, 4)
    assert issue is not None and issue.pull is not None
    return issue.pull


# ------------------------------------------------------------------------------------------------------ a draft


async def test_a_draft_waits_on_its_author_who_marks_it_ready_for_review_only_where_a_take_has_them(
    tmp_path: Path,
) -> None:
    """Documented: "Ready for review" in a draft's merge box marks it ready for review; GitHub does it by GraphQL's
    `markPullRequestReadyForReview`, not by REST, and sends `pull_request` with `ready_for_review`.
    https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/changing-the-stage-of-a-pull-request,
    https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request"""
    with receiver() as agent:
        target = InboundTarget(provider="github", url=agent.url, secret=GeneratedSecret(env="GITHUB_WEBHOOK_SECRET"))
        port, store, clock = world(tmp_path, with_ledger({"draft": True}), target=target)
        before = waits(port, TOMAS, store)
        offers = {o.name: o for o in port.legal(item(4), Actor.PERSON, TOMAS, store)}
        moved = await port.apply(item(4), "ready_for_review", Actor.PERSON, TOMAS, "{}", store, clock)
    assert before == dict([(item(3), "open"), (item(4), "draft")])
    assert "merge" not in offers, "a draft is not merged"
    ready = offers["ready_for_review"]
    assert (ready.label, ready.to_state, ready.unprompted) == ("Ready for review", "open", False), (
        "work in progress: a model never marks it ready"
    )
    assert (moved.name, moved.from_state, moved.to_state, moved.by, moved.who) == (
        "ready_for_review",
        "draft",
        "open",
        Actor.PERSON,
        "tomas",
    )
    assert pull_of(store).draft is False
    assert waits(port, TOMAS, store) == dict([(item(3), "open")]), "ready, it waits for its review, not on its author"
    assert waits(port, IRIS, store)[item(4)] == "review requested"
    [pushed] = agent.pushed
    payload = pushed.payload()
    assert (pushed.event, payload["action"], payload["number"]) == ("pull_request", "ready_for_review", 4)
    schema = HOOKS["x-webhooks"]["pull-request-ready-for-review"]["post"]["requestBody"]["content"]["application/json"]
    assert missing(HOOKS["components"], schema["schema"], payload, "$") == []
    with pytest.raises(ValueError, match="offers tomas no 'ready_for_review'"):
        await port.apply(item(4), "ready_for_review", Actor.PERSON, TOMAS, "{}", store, clock)


async def test_a_take_has_the_author_mark_their_draft_ready_for_review(tmp_path: Path) -> None:
    """A take `{provider: github, take: ready_for_review}` pins the move a model never picks; nothing else of Tomas's
    waits on him here (issue 3 is nobody's)."""
    pinned = Take(provider="github", take="ready_for_review", after=timedelta(hours=2))
    people = [p.model_copy(update={"takes": [pinned]}) if p.key == "tomas" else p for p in SCENARIO.people]
    scenario = SCENARIO.model_copy(update={"transitions_on": ["github"], "people": people})
    port, store, clock = world(tmp_path, with_ledger({"draft": True}, {"assignees": []}), scenario)
    engine = people_engine(scenario, {"github": port}, people_model())

    [mine] = [b for b in (await engine.look(store, clock)).booked if b.person == "tomas"]
    assert mine.item == item(4) and mine.at == START + timedelta(hours=2)
    clock.jump(START + timedelta(hours=2))
    acted = await engine.act(mine.pending, store, clock)

    assert acted.transition is not None
    assert (acted.transition.name, acted.transition.who, acted.transition.by) == (
        "ready_for_review",
        "tomas",
        Actor.PERSON,
    )
    assert pull_of(store).draft is False


async def test_with_no_take_a_draft_is_held_on_its_author_with_no_moment_and_never_moved(tmp_path: Path) -> None:
    """Nothing of their own draft is theirs to do unprompted: its review and close are their own only where a take
    has them, and marking it ready is the scenario's. So the people engine holds it on Tomas with no moment, never
    books him to be dropped and booked again, and nothing moves it."""
    scenario = SCENARIO.model_copy(update={"transitions_on": ["github"]})
    port, store, clock = world(tmp_path, with_ledger({"draft": True}, {"assignees": []}), scenario)
    engine = people_engine(scenario, {"github": port}, people_model())

    offers = port.legal(item(4), Actor.PERSON, TOMAS, store)
    assert [o.name for o in offers if o.unprompted] == [], "his own review, his own close: only by a take"
    assert [o.name for o in port.legal(item(4), Actor.PERSON, IRIS, store) if o.unprompted][:1] == ["APPROVE"]
    [mine] = [b for b in (await engine.look(store, clock)).booked if b.person == "tomas"]
    assert mine.item == item(4) and mine.at is None
    assert engine.pending(mine.pending, store).held is not None, "held on purpose, saying why"
    clock.jump(START + timedelta(days=3))
    assert [b for b in (await engine.look(store, clock)).booked if b.person == "tomas"] == [], "held, not booked again"
    assert pull_of(store).draft is True


# ------------------------------------------------------------------------------------------------------ approved


async def test_a_pull_request_someone_else_approves_that_merges_cleanly_waits_on_its_author_to_merge_it(
    tmp_path: Path,
) -> None:
    """Documented: an approval is "a review allowing the pull request to merge"; one that merges cleanly is merged by
    the merge route. https://docs.github.com/en/graphql/reference/pulls,
    https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request"""
    port, store, clock = world(tmp_path, pulls_seed())
    assert item(4) not in waits(port, TOMAS, store), "it only waits for review"
    await port.apply(item(4), "APPROVE", Actor.PERSON, IRIS, "{}", store, clock)
    approved = waits(port, TOMAS, store)
    merge = next(o for o in port.legal(item(4), Actor.PERSON, TOMAS, store) if o.name == "merge")
    moved = await port.apply(item(4), "merge", Actor.PERSON, TOMAS, "{}", store, clock)
    assert approved[item(4)] == "approved" and merge.unprompted
    assert (moved.name, moved.to_state, moved.who) == ("merge", "merged", "tomas")
    assert pull_of(store).merged and item(4) not in waits(port, TOMAS, store)


async def test_a_pull_request_waits_on_its_author_while_the_latest_word_of_every_reviewer_lets_it_merge(
    tmp_path: Path,
) -> None:
    """Documented: each reviewer counts by their latest review that approves or asks for changes (CHANGES_REQUESTED is
    "a review blocking the pull request from merging", COMMENTED "an informational review"); the author's own
    approval is nobody else's. https://docs.github.com/en/graphql/reference/pulls"""
    port, store, clock = world(tmp_path, pulls_seed())
    await port.apply(item(4), "APPROVE", Actor.PERSON, TOMAS, "{}", store, clock)
    own = waits(port, TOMAS, store)
    found: list[str | None] = []
    for event, words in (
        ("APPROVE", None),
        ("REQUEST_CHANGES", "It needs a test."),
        ("COMMENT", "Still needs the test."),
        ("APPROVE", None),
    ):
        content = "{}" if words is None else json.dumps({"body": words})
        await port.apply(item(4), event, Actor.PERSON, IRIS, content, store, clock)
        held = waits(port, TOMAS, store)
        found.append(held[item(4)] if item(4) in held else None)
    assert item(4) not in own, "its author's own approval lets nothing merge"
    assert found == ["approved", None, None, "approved"]


async def test_an_approved_pull_request_that_no_longer_merges_cleanly_does_not_wait_on_its_author(
    tmp_path: Path,
) -> None:
    """A commit to the default branch changes the file the pull request changes: whether it merges is not known
    (`mergeable: null`), so its author has nothing to merge."""
    port, store, clock = world(tmp_path, pulls_seed())
    await port.apply(item(4), "APPROVE", Actor.PERSON, IRIS, "{}", store, clock)
    approved = waits(port, TOMAS, store)
    config = "/repos/lanternworks/ledger/contents/services/billing/config.py"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(store, clock)),
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {IRIS_TOKEN}"},
    ) as http:
        held = (await http.get(config)).json()["sha"]
        raw = base64.b64encode(b"PAYMENT_TIMEOUT = 45\nRETRY_LIMIT = 3\n").decode()
        put = await http.put(config, json={"message": "Retry less", "content": raw, "sha": held})
    assert put.status_code == 200, put.text
    assert approved[item(4)] == "approved"
    assert item(4) not in waits(port, TOMAS, store)
    assert "merge" not in [o.name for o in port.legal(item(4), Actor.PERSON, TOMAS, store)]


# ------------------------------------------------------------------------------------------------------ work


async def test_an_issue_assigned_to_a_person_is_work_nothing_holds_back_and_no_estimate_sizes(tmp_path: Path) -> None:
    """An open issue assigned to a person is work they carry (`Waiting.work`); GitHub holds neither its due day nor a
    priority, nothing links it to an issue blocking it here (issue dependencies are not served), and it has no
    estimate. A pull request is no work: an ask. Closing an issue finishes it; a GitHub issue has no started state.
    https://docs.github.com/en/rest/issues/issue-dependencies"""
    port, store, clock = world(tmp_path, pulls_seed())
    assert isinstance(port, CarriesWork)
    issue = next(w for w in port.items_for(TOMAS, store) if w.item == item(3))
    asked = next(w for w in port.items_for(IRIS, store) if w.item == item(4))
    assert (issue.work, issue.state, issue.due, issue.rank) == (True, "open", None, None)
    assert asked.work is False
    assert port.holding(item(3), store) == [] and port.estimate(item(3), store) is None
    meant = {
        number: [(o.name, o.means) for o in port.legal(item(number), Actor.PERSON, TOMAS, store)] for number in (2, 3)
    }
    assert meant == {2: [("reopen", TicketState.OPEN)], 3: [("close", TicketState.DONE)]}
    with pytest.raises(LookupError):
        port.holding(item(99), store)
    await port.apply(item(3), "close", Actor.PERSON, TOMAS, "{}", store, clock)
    assert item(3) not in waits(port, TOMAS, store), "closed: finished"
