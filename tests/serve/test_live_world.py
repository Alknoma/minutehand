"""A world under `minutehand serve` is seeded with each provider's own seed fragment exactly as seeding writes it,
refuses a malformed one with the provider's own words, grows while open (`FurtherSeed`), goes back to its seed in
place (`reset`), and shows its raw state and what each provider can be asked to do."""

from __future__ import annotations

import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld, FurtherSeed
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.provider import PersonChange
from minutehand.domain.scenario import PersonEdits, PersonPosts, Seed
from minutehand.domain.world import Actor, EntityKind, Operation
from minutehand.testing.client import Refused, Unsupported
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, answer, dm, event_receiver, spec

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]


def _seed(provider: str, body: object, **more: object) -> Seed:
    return Seed.model_validate(
        {"starts_at": START.isoformat(), "people": PEOPLE, "provider_seeds": [{"provider": provider, "body": body}]}
        | more
    )


def _http(served: Served) -> httpx.Client:
    trust = ssl.create_default_context(cafile=served.bundle)
    return httpx.Client(proxy=served.proxy, verify=trust, trust_env=False, timeout=30)


class Fragment:
    """One provider's seed fragment, the claims that make a call this world's, and a read that shows the fragment
    reached the service: (method, url, headers) and a phrase the answer holds."""

    def __init__(
        self,
        provider: str,
        body: object,
        claims: Claims,
        probe: tuple[str, str, dict[str, str]] | None,
        holds: str = "",
    ) -> None:
        self.provider = provider
        self.body = body
        self.claims = claims
        self.probe = probe
        self.holds = holds


FRAGMENTS = [
    Fragment(
        "github",
        {
            "users": [{"login": "octo", "name": "Octo Cat"}],
            "tokens": [{"token": "ghp_seedfragment", "kind": "classic", "login": "octo"}],
            "repositories": [{"owner": "octo", "name": "notes", "files": [{"path": "README.md", "text": "hi"}]}],
        },
        Claims(tokens=["ghp_seedfragment"]),
        ("GET", "https://api.github.com/repos/octo/notes", {"Authorization": "Bearer ghp_seedfragment"}),
        '"full_name":"octo/notes"',
    ),
    Fragment(
        "jira",
        {
            "site": "seedfrag",
            "agent_email": "agent@x.example",
            "credentials": [{"account": "agent", "api_token": "sf"}],
        },
        Claims(keys=["seedfrag"]),
        (
            "GET",
            "https://seedfrag.atlassian.net/rest/api/3/myself",
            {"Authorization": "Basic YWdlbnRAeC5leGFtcGxlOnNm"},
        ),
        "agent@x.example",
    ),
    Fragment(
        "youtrack",
        {
            "users": [{"login": "helper", "name": "Helper Bot"}],
            "tokens": [{"token": "perm:seedfrag", "login": "helper"}],
        },
        Claims(tokens=["perm:seedfrag"]),
        ("GET", "https://seedfrag.youtrack.cloud/api/users/me?fields=login", {"Authorization": "Bearer perm:seedfrag"}),
        '"helper"',
    ),
    Fragment(
        "notion",
        {
            "workspaces": [
                {
                    "key": "acme",
                    "name": "Acme",
                    "integrations": [{"key": "agent", "name": "Planning bot", "tokens": ["secret_seedfrag"]}],
                }
            ]
        },
        Claims(tokens=["secret_seedfrag"]),
        (
            "GET",
            "https://api.notion.com/v1/users/me",
            {"Authorization": "Bearer secret_seedfrag", "Notion-Version": "2022-06-28"},
        ),
        "Planning bot",
    ),
    Fragment(
        "asana",
        {"tokens": [{"token": "asana-seedfrag"}], "tags": ["urgent"]},
        Claims(tokens=["asana-seedfrag"]),
        (
            "GET",
            "https://app.asana.com/api/1.0/tags?workspace=" + "{workspace}",
            {"Authorization": "Bearer asana-seedfrag"},
        ),
        "urgent",
    ),
    Fragment(
        "slack",
        {"faults": [{"call": "auth.test", "answer": {"kind": "refused", "error": "account_inactive"}}]},
        Claims(tokens=["xoxb-seedfrag"]),
        ("POST", "https://slack.com/api/auth.test", {"Authorization": "Bearer xoxb-seedfrag"}),
        "account_inactive",
    ),
    Fragment(
        "google_drive",
        {"faults": [{"operation": "files.list", "kind": "rate_limited", "times": 2}]},
        Claims(tokens=["ya29.seedfrag"]),
        None,
    ),
    Fragment("microsoft", {"not_installed_for": ["sofia"]}, Claims(keys=["seedfrag.example"]), None),
]


def _directly_seeded(provider: str, seed: Seed, tmp_path: Path) -> dict[tuple[str, str], str]:
    """What the provider's own seeding writes for `seed`, outside any server: each entity's latest body."""
    scenario = seed.starting(START)
    store = SqliteStore(tmp_path / "direct.db", "direct", RunClock(scenario.starts_at))
    registry = Registry.installed()
    manifest = next(m for m in registry.manifests if m.key == provider)
    registry.provider(manifest).seed(scenario, store)
    found: dict[tuple[str, str], str] = {}
    for event in store.events():
        stored = store.get(event.entity)
        if stored is not None:
            found[(event.entity.kind.value, event.entity.external_id)] = stored.body
    store.close()
    return found


@pytest.mark.parametrize("fragment", FRAGMENTS, ids=[f.provider for f in FRAGMENTS])
def test_a_world_opened_with_a_providers_seed_fragment_holds_exactly_what_its_seeding_writes(
    served: Served, fragment: Fragment, tmp_path: Path
) -> None:
    seed = _seed(fragment.provider, fragment.body)
    world = OpenWorld(served.client, served.client.create_world(CreateWorld(seed=seed, claims=fragment.claims)))
    try:
        held = {(s.entity.kind.value, s.entity.external_id): s.body for s in world.entities(provider=fragment.provider)}
        assert held == _directly_seeded(fragment.provider, seed, tmp_path)
        assert held, f"{fragment.provider} seeded nothing from its fragment"
        if fragment.probe is not None:
            method, url, headers = fragment.probe
            if "{workspace}" in url:
                workspace = next(
                    s.entity.external_id
                    for s in world.entities(provider="asana", kind=EntityKind.RECORD)
                    if s.parent == "workspaces"
                )
                url = url.replace("{workspace}", workspace)
            with _http(served) as http:
                answered = http.request(method, url, headers=headers)
            assert fragment.holds in answered.text, answered.text
    finally:
        served.client.close_world(world.world_id)


@pytest.mark.parametrize("fragment", FRAGMENTS, ids=[f.provider for f in FRAGMENTS])
def test_a_malformed_seed_fragment_is_refused_with_the_providers_own_words(served: Served, fragment: Fragment) -> None:
    seed = _seed(fragment.provider, {"nonsense": True})
    with pytest.raises(Refused) as refused:
        served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=[f"refused-{fragment.provider}"])))
    assert refused.value.status == 422
    assert "nonsense" in refused.value.error and "Seed" in refused.value.error
    assert not any(w.claims.tokens == [f"refused-{fragment.provider}"] for w in served.client.worlds())


def test_a_seed_fragment_for_a_provider_with_no_seed_of_its_own_is_refused(served: Served) -> None:
    with pytest.raises(Unsupported) as refused:
        served.client.create_world(CreateWorld(seed=_seed("aws", {"queues": []}), claims=Claims(tokens=["aws-x"])))
    assert "aws has no seed of its own" in refused.value.error


def _slack_users(served: Served, token: str) -> list[str]:
    with _http(served) as http:
        answered = http.post("https://slack.com/api/users.list", headers={"Authorization": f"Bearer {token}"})
    return [m["profile"]["email"] for m in answered.json()["members"] if "email" in m["profile"]]


def test_a_person_added_to_an_open_world_is_a_slack_member_and_an_asana_user(served: Served) -> None:
    seed = Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "people": PEOPLE,
            "tickets": [{"provider": "asana", "project": "Launch", "title": "Book the venue", "assignee": "sofia"}],
            "provider_seeds": [{"provider": "asana", "body": {"tokens": [{"token": "asana-grow"}]}}],
        }
    )
    world = OpenWorld(
        served.client,
        served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=["xoxb-grow", "asana-grow"]))),
    )
    try:
        assert "ivy@example.com" not in _slack_users(served, "xoxb-grow")
        head = served.client.world(world.world_id).head
        grown = world.add_person({"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com"})
        assert grown.written["slack"] > 0 and grown.written["asana"] > 0
        assert "ivy@example.com" in _slack_users(served, "xoxb-grow")
        with _http(served) as http:
            users = http.get("https://app.asana.com/api/1.0/users?opt_fields=email",
                             headers={"Authorization": "Bearer asana-grow"}).json()["data"]  # fmt: skip
        assert "ivy@example.com" in [u["email"] for u in users]
        added = [e for e in world.events(since=head) if e.operation not in (Operation.READ, Operation.SEARCH)]
        assert added and all(e.actor is Actor.SCENARIO for e in added)
        assert all(e.sim_time == served.client.world(world.world_id).now for e in added)
    finally:
        served.client.close_world(world.world_id)


def test_a_ticket_and_a_provider_fragment_added_while_open_reach_the_service(served: Served) -> None:
    seed = _seed(
        "github",
        {
            "users": [{"login": "octo", "name": "Octo"}],
            "tokens": [{"token": "ghp_grow", "kind": "classic", "login": "octo"}],
        },
    )
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=["ghp_grow"])))
    )
    try:
        with _http(served) as http:
            assert (
                http.get(
                    "https://api.github.com/repos/octo/later", headers={"Authorization": "Bearer ghp_grow"}
                ).status_code
                == 404
            )
            world.seed(
                {
                    "provider_seeds": [
                        {"provider": "github", "body": {"repositories": [{"owner": "octo", "name": "later"}]}}
                    ]
                }
            )
            found = http.get("https://api.github.com/repos/octo/later", headers={"Authorization": "Bearer ghp_grow"})
        assert found.status_code == 200 and found.json()["full_name"] == "octo/later"
    finally:
        served.client.close_world(world.world_id)  # fmt: skip


def test_an_addition_that_takes_a_persons_key_is_refused_and_nothing_is_written(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-taken")))
    try:
        head = served.client.world(world.world_id).head
        with pytest.raises(Refused) as refused:
            world.add_person({"key": "sofia", "name": "Another Sofia", "email": "sofia2@example.com"})
        assert refused.value.status == 409 and "two people share a key" in refused.value.error
        assert served.client.world(world.world_id).head == head
    finally:
        served.client.close_world(world.world_id)


def test_a_fragment_contradicting_the_worlds_seed_is_refused(served: Served) -> None:
    seed = _seed("jira", {"site": "contra", "agent_email": "agent@x.example"})
    world = OpenWorld(served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(keys=["contra"]))))
    try:
        with pytest.raises(Refused) as refused:
            world.seed({"provider_seeds": [{"provider": "jira", "body": {"site": "elsewhere"}}]})
        assert (
            refused.value.status == 409 and "'contra'" in refused.value.error and "'elsewhere'" in refused.value.error
        )
    finally:
        served.client.close_world(world.world_id)


def test_an_addition_over_something_changed_since_seeding_is_refused(served: Served) -> None:
    """A repository's limits live on its own record: once the test has lowered them on the open world, a further seed
    that would set them again finds the record no longer what the seed wrote."""
    seed = _seed(
        "github",
        {
            "users": [{"login": "octo", "name": "Octo"}],
            "tokens": [{"token": "ghp_changed", "kind": "classic", "login": "octo"}],
            "repositories": [{"owner": "octo", "name": "notes"}],
        },
    )
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=["ghp_changed"])))
    )
    try:
        world.declare_faults("github", {"limits": [{"repository": "octo/notes", "tree_entry_limit": 2}]})
        head = served.client.world(world.world_id).head
        with pytest.raises(Refused) as refused:
            world.seed(
                {
                    "provider_seeds": [
                        {
                            "provider": "github",
                            "body": {"limits": [{"repository": "octo/notes", "directory_entry_limit": 3}]},
                        }
                    ]
                }
            )
        assert refused.value.status == 409 and "changed since it was seeded" in refused.value.error
        assert served.client.world(world.world_id).head == head
    finally:
        served.client.close_world(world.world_id)  # fmt: skip


def test_a_world_reset_in_place_keeps_its_id_and_claims_and_holds_only_its_seed(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-reset")))
    try:
        seeded = {(s.entity.kind, s.entity.external_id) for s in world.entities(provider="slack")}
        opened = served.client.world(world.world_id)
        posted = answer(
            served.slack("xoxb-reset").chat_postMessage(
                channel=dm(served, "xoxb-reset", "sofia@example.com"), text="Hello"
            )
        )
        message = posted["ts"]
        world.advance(to=START.replace(day=3))
        world.add_person({"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com"})
        assert any(s.entity.external_id == message for s in world.entities(provider="slack"))
        view = world.reset()
        assert (view.world_id, view.claims, view.now) == (opened.world_id, opened.claims, opened.now)
        assert {(s.entity.kind, s.entity.external_id) for s in world.entities(provider="slack")} == seeded
        assert "ivy@example.com" not in _slack_users(served, "xoxb-reset")
        assert all(e.operation in (Operation.READ, Operation.SEARCH) for e in world.events(actor=Actor.AGENT))
    finally:
        served.client.close_world(world.world_id)


def test_raw_state_shows_every_version_of_an_entity_and_one_deleted(served: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(served.client, served.client.create_world(spec("xoxb-raw", inbound=receiver.url)))
        try:
            world.happen(PersonPosts(provider="slack", person="sofia", text="first", key="note"))
            world.happen(PersonEdits(provider="slack", person="sofia", post="note", text="second"))
            raw = world.raw_state("slack")
            assert raw.provider == "slack"
            note = next(e for e in raw.entities if e.entity.kind is EntityKind.MESSAGE and len(e.versions) == 2)
            assert "first" in note.versions[0].body and "second" in note.versions[1].body and not note.deleted
            with pytest.raises(Refused) as unnamed:
                served.client._get(f"/worlds/{world.world_id}/state", type(raw))
            assert unnamed.value.status == 422
        finally:
            served.client.close_world(world.world_id)


def test_a_post_happened_through_the_control_api_can_be_edited_by_a_later_act(served: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(served.client, served.client.create_world(spec("xoxb-chain", inbound=receiver.url)))
        try:
            world.happen(PersonPosts(provider="slack", person="sofia", text="The venue is booked.", key="venue"))
            edited = world.happen(PersonEdits(provider="slack", person="sofia", post="venue", text="Booked: hall B."))
            assert edited.operation is Operation.UPDATE and edited.actor is Actor.PERSON
            assert len(receiver.pushed) == 2
        finally:
            served.client.close_world(world.world_id)


def test_each_provider_says_what_it_can_be_asked_to_do_while_open(served: Served) -> None:
    found = {p.key: p for p in served.client.providers()}
    assert set(found) >= {"slack", "asana", "jira", "youtrack", "google_drive", "notion", "microsoft", "github", "aws"}
    assert found["github"].faults and found["github"].seed_model
    assert not found["aws"].seed_model and not found["aws"].people_changes and not found["aws"].faults


def test_a_person_change_a_provider_cannot_show_is_refused_as_unsupported(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-unsupported")))
    try:
        with pytest.raises(Unsupported) as refused:
            world.remove_person("github", "sofia")
        assert refused.value.status == 409 and "github cannot show a person removed" in refused.value.error
        assert (
            PersonChange.REMOVED not in next(p for p in served.client.providers() if p.key == "github").people_changes
        )
    finally:
        served.client.close_world(world.world_id)


def test_a_further_seed_that_adds_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="adds nothing"):
        FurtherSeed()


def test_a_notion_webhook_subscription_added_to_an_open_world_is_held_and_watched(served: Served) -> None:
    """The old emulator's `/webhooks/register` has no Notion counterpart: a subscription is set up in an
    integration's settings. Here it is seeded, at creation or, as here, while the world is open."""
    body = {"workspaces": [{"key": "acme", "name": "Acme", "integrations": [
        {"key": "agent", "name": "Planning bot", "tokens": ["secret_hooked"]}]}]}  # fmt: skip
    world = OpenWorld(
        served.client,
        served.client.create_world(CreateWorld(seed=_seed("notion", body), claims=Claims(tokens=["secret_hooked"]))),
    )
    try:
        before = {(s.entity.kind, s.entity.external_id) for s in world.entities(provider="notion")}
        hook = {"integration": "agent", "url": "http://127.0.0.1:9/hooks", "verification_token": "secret_" + "v" * 43}
        grown = world.seed({"provider_seeds": [{"provider": "notion", "body": {"webhooks": [hook]}}]})
        assert grown.written["notion"] >= 1
        added = [s for s in world.entities(provider="notion") if (s.entity.kind, s.entity.external_id) not in before]
        assert any("http://127.0.0.1:9/hooks" in s.body for s in added)
    finally:
        served.client.close_world(world.world_id)


def test_a_press_goes_to_the_worlds_interactivity_url_when_it_declares_one(served: Served) -> None:
    from minutehand.adapters.control.wire import Inbound
    from minutehand.domain.people import Press
    from minutehand.domain.world import MessageSnapshot

    with event_receiver() as events, event_receiver() as interactions:
        opened = spec("xoxb-interactive")
        targets = [Inbound(provider="slack", url=events.url, interactivity_url=interactions.url, secret="s")]
        world = OpenWorld(served.client, served.client.create_world(opened.model_copy(update={"inbound": targets})))
        try:
            blocks = [{"type": "actions", "elements": [{"type": "button", "action_id": "go", "value": "1",
                                                        "text": {"type": "plain_text", "text": "Go"}}]}]  # fmt: skip
            channel = dm(served, "xoxb-interactive", "sofia@example.com")
            served.slack("xoxb-interactive").chat_postMessage(channel=channel, text="Go?", blocks=blocks)
            message = next(
                e.entity
                for e in world.events(provider="slack", actor=Actor.AGENT)
                if isinstance(e.after, MessageSnapshot) and e.after.actions
            )
            world.press("sofia", message, Press(action_id="go", label="Go", value="1"))
            assert len(interactions.pushed) == 1 and not events.pushed
        finally:
            served.client.close_world(world.world_id)


ADDITIONS = [
    ("youtrack", {"tickets": [{"provider": "youtrack", "project": "Launch", "title": "Second", "assignee": "owen"}]}),
    ("jira", {"tickets": [{"provider": "jira", "project": "Launch", "title": "Second", "assignee": "owen"}]}),
    ("google_drive", {"documents": [{"provider": "google_drive", "title": "Second doc", "text": "x"}]}),
    ("notion", {"documents": [{"provider": "notion", "title": "Second doc", "text": "x"}]}),
    ("notion", {"people": [{"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com"}]}),
    ("jira", {"people": [{"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com"}]}),
    ("youtrack", {"people": [{"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com"}]}),
    ("google_drive", {"people": [{"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com"}]}),
]


@pytest.mark.parametrize(("provider", "added"), ADDITIONS, ids=[f"{a[0]}-{next(iter(a[1]))}" for a in ADDITIONS])
def test_an_addition_lands_beside_what_a_provider_seeded_and_moves_none_of_it(
    served: Served, provider: str, added: dict[str, object]
) -> None:
    """Every provider names what it seeds by what it is, so a person seeded ahead of an issue, a file or a page
    in the seed's order no longer moves it: the addition lands, and every id the world held is still there."""
    seed = Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "people": PEOPLE,
            "tickets": [{"provider": p, "project": "Launch", "title": "Book the venue", "assignee": "sofia"}
                        for p in ("jira", "youtrack") if p == provider],
            "documents": [{"provider": p, "title": "Plan", "text": "hello"}
                          for p in ("google_drive", "notion") if p == provider],
        }
    )  # fmt: skip
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=[f"add-{provider}"])))
    )
    try:
        held = {(s.entity.kind, s.entity.external_id): s.body for s in world.entities(provider=provider)}
        assert world.seed(added).written[provider] > 0
        now = {(s.entity.kind, s.entity.external_id) for s in world.entities(provider=provider)}
        assert set(held) <= now
    finally:
        served.client.close_world(world.world_id)
