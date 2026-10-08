"""People and tickets change in a tracker world already open under `minutehand serve`: a person deletes a ticket
(now, or as a ticket's fate), an administrator removes, deactivates or reactivates an account, a YouTrack
permission is granted or withheld, Asana's threshold for a read without `limit` is lowered; each seen through the
tracker's own API, and a change a tracker cannot show refused as unsupported."""

from __future__ import annotations

import base64
import ssl
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import timedelta

import httpx
import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.adapters.providers.asana import state as asana_state
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation
from minutehand.testing.client import Unsupported
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served

PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]
ASANA = "https://app.asana.com/api/1.0"
JIRA_BASIC = "Basic " + base64.b64encode(b"agent@x.example:tl").decode()


def _seed(**more: object) -> Seed:
    return Seed.model_validate({"starts_at": "2026-09-01T09:00:00Z", "people": PEOPLE} | more)


@contextmanager
def _world(served: Served, seed: Seed, claims: Claims, *, scripted: bool = False) -> Iterator[OpenWorld]:
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=seed, claims=claims, scripted_people=scripted))
    )
    try:
        yield world
    finally:
        served.client.close_world(world.world_id)


@contextmanager
def _http(served: Served, headers: dict[str, str]) -> Iterator[httpx.Client]:
    trust = ssl.create_default_context(cafile=served.bundle)
    with httpx.Client(proxy=served.proxy, verify=trust, trust_env=False, timeout=30, headers=headers) as http:
        yield http


def _ticket(world: OpenWorld, provider: str) -> EntityRef:
    return next(s.entity for s in world.entities(provider=provider, kind=EntityKind.TICKET))


def _asana(served: Served, token: str) -> AbstractContextManager[httpx.Client]:
    return _http(served, {"Authorization": f"Bearer {token}"})


def _asana_world(token: str, **more: object) -> tuple[Seed, Claims]:
    seed = _seed(
        tickets=[{"provider": "asana", "project": "Launch", "title": "Book the venue", "assignee": "sofia"}],
        provider_seeds=[{"provider": "asana", "body": {"tokens": [{"token": token}], **more}}],
    )
    return seed, Claims(tokens=[token])


def _jira_world(site: str) -> tuple[Seed, Claims]:
    seed = _seed(
        tickets=[{"provider": "jira", "project": "Ops", "title": "Book the venue", "assignee": "sofia"}],
        provider_seeds=[
            {
                "provider": "jira",
                "body": {"site": site, "agent_email": "agent@x.example",
                         "credentials": [{"account": "agent", "api_token": "tl"}]},
            }
        ],
    )  # fmt: skip
    return seed, Claims(keys=[site])


def _youtrack_world(token: str) -> tuple[Seed, Claims]:
    seed = _seed(
        tickets=[{"provider": "youtrack", "project": "Launch", "title": "Book the venue", "assignee": "sofia"}],
        provider_seeds=[{"provider": "youtrack", "body": {"tokens": [{"token": token, "login": "sofia"}]}}],
    )
    return seed, Claims(tokens=[token])


# ------------------------------------------------------------------ a person deletes a ticket


def test_a_person_deletes_an_asana_task_now_and_asana_answers_404(served: Served) -> None:
    seed, claims = _asana_world("asana-delete")
    with _world(served, seed, claims) as world, _asana(served, "asana-delete") as http:
        task = _ticket(world, "asana")
        assert http.get(f"{ASANA}/tasks/{task.external_id}").status_code == 200
        deleted = world.delete_ticket(task)
        assert (deleted.actor, deleted.operation) == (Actor.PERSON, Operation.DELETE)
        gone = http.get(f"{ASANA}/tasks/{task.external_id}")
        assert gone.status_code == 404, gone.text


def test_a_jira_issue_deleted_now_is_404_through_the_api(served: Served) -> None:
    seed, claims = _jira_world("trackerdel")
    with _world(served, seed, claims) as world, _http(served, {"Authorization": JIRA_BASIC}) as http:
        issue = _ticket(world, "jira")
        url = f"https://trackerdel.atlassian.net/rest/api/3/issue/{issue.external_id}"
        assert http.get(url).status_code == 200
        world.delete_ticket(issue)
        assert http.get(url).status_code == 404


def test_a_youtrack_issue_deleted_now_is_404_through_the_api(served: Served) -> None:
    seed, claims = _youtrack_world("perm:trackerdel")
    with _world(served, seed, claims) as world, _http(served, {"Authorization": "Bearer perm:trackerdel"}) as http:
        issue = _ticket(world, "youtrack")
        url = f"https://trackerdel.youtrack.cloud/api/issues/{issue.external_id}?fields=idReadable"
        assert http.get(url).status_code == 200
        world.delete_ticket(issue)
        assert http.get(url).status_code == 404


def test_deleting_a_ticket_already_gone_is_refused(served: Served) -> None:
    seed, claims = _asana_world("asana-twice")
    with _world(served, seed, claims) as world:
        task = _ticket(world, "asana")
        world.delete_ticket(task)
        with pytest.raises(Exception) as refused:
            world.delete_ticket(task)
        assert "no asana task" in str(refused.value)


def test_a_task_the_agent_hands_to_a_person_whose_fate_is_deletion_is_gone_once_the_clock_passes_it(
    served: Served,
) -> None:
    seed, claims = _asana_world("asana-fate")
    seed = Seed.model_validate(
        {**seed.model_dump(mode="json"), "ticket_fates": [{"assignee": "sofia", "deleted": True, "after": "PT1H"}]}
    )
    with _world(served, seed, claims, scripted=True) as world, _asana(served, "asana-fate") as http:
        project = next(
            s.entity.external_id
            for s in world.entities(provider="asana", kind=EntityKind.RECORD)
            if s.parent == asana_state.PROJECTS
        )
        made = http.post(
            f"{ASANA}/tasks",
            json={"data": {"name": "Order the badges", "assignee": "sofia@example.com", "projects": [project]}},
        )
        assert made.status_code == 201, made.text
        gid = made.json()["data"]["gid"]
        assert http.get(f"{ASANA}/tasks/{gid}").status_code == 200
        advanced = world.advance(timedelta(hours=2))
        assert any("deletes" in f.what for f in advanced.fired), advanced
        assert http.get(f"{ASANA}/tasks/{gid}").status_code == 404
        deleted = [e for e in world.events(provider="asana", operation=Operation.DELETE) if e.entity.external_id == gid]
        assert [e.actor for e in deleted] == [Actor.PERSON]


# ------------------------------------------------------------------ an account changes


def test_a_person_removed_from_asana_is_unlisted_unassignable_and_not_found(served: Served) -> None:
    seed, claims = _asana_world("asana-remove")
    with _world(served, seed, claims) as world, _asana(served, "asana-remove") as http:
        sofia = asana_state.user_gid("sofia")
        before = [u["gid"] for u in http.get(f"{ASANA}/users").json()["data"]]
        world.remove_person("asana", "sofia")
        after = [u["gid"] for u in http.get(f"{ASANA}/users").json()["data"]]
        assert sofia in before and sofia not in after
        assert http.get(f"{ASANA}/users/{sofia}").status_code == 404
        task = _ticket(world, "asana")
        refused = http.put(f"{ASANA}/tasks/{task.external_id}", json={"data": {"assignee": sofia}})
        assert refused.status_code == 400, refused.text


def test_a_jira_account_deactivated_reads_inactive_and_is_reactivated(served: Served) -> None:
    seed, claims = _jira_world("trackeract")
    with _world(served, seed, claims) as world, _http(served, {"Authorization": JIRA_BASIC}) as http:
        everyone = "https://trackeract.atlassian.net/rest/api/3/users/search"
        sofia = next(u for u in http.get(everyone).json() if u.get("displayName") == "Sofia Romano")
        account = f"https://trackeract.atlassian.net/rest/api/3/user?accountId={sofia['accountId']}"
        assert http.get(account).json()["active"] is True
        changed = world.deactivate_person("jira", "sofia")
        assert changed.actor is Actor.SCENARIO
        assert http.get(account).json()["active"] is False
        issue = _ticket(world, "jira")
        assigned = http.put(
            f"https://trackeract.atlassian.net/rest/api/3/issue/{issue.external_id}/assignee",
            json={"accountId": sofia["accountId"]},
        )
        assert assigned.status_code == 400, assigned.text
        world.reactivate_person("jira", "sofia")
        assert http.get(account).json()["active"] is True


def test_a_youtrack_account_banned_reads_banned_and_its_token_still_acts_as_it(served: Served) -> None:
    """Minutehand checks no credential: a banned account's token still reaches the API, as that account."""
    seed, claims = _youtrack_world("perm:trackerban")
    with _world(served, seed, claims) as world, _http(served, {"Authorization": "Bearer perm:trackerban"}) as http:
        me = "https://trackerban.youtrack.cloud/api/users/me?fields=login,banned"
        assert http.get(me).json() == {"login": "sofia", "banned": False, "$type": "Me"}
        world.deactivate_person("youtrack", "sofia")
        assert http.get(me).json() == {"login": "sofia", "banned": True, "$type": "Me"}
        world.reactivate_person("youtrack", "sofia")
        assert http.get(me).json()["banned"] is False


def test_a_notion_member_removed_is_unlisted_and_not_found(served: Served) -> None:
    seed = _seed(
        provider_seeds=[
            {
                "provider": "notion",
                "body": {"workspaces": [{"key": "acme", "name": "Acme", "integrations": [
                    {"key": "agent", "name": "Planning bot", "tokens": ["secret_trackerrm"],
                     "capabilities": ["read_content", "read_users_with_email"]}]}]},
            }
        ]
    )  # fmt: skip
    headers = {"Authorization": "Bearer secret_trackerrm", "Notion-Version": "2022-06-28"}
    with _world(served, seed, Claims(tokens=["secret_trackerrm"])) as world, _http(served, headers) as http:

        def people() -> dict[str, str]:
            listed = http.get("https://api.notion.com/v1/users").json()["results"]
            return {u["person"]["email"]: u["id"] for u in listed if u["type"] == "person"}

        sofia = people()["sofia@example.com"]
        world.remove_person("notion", "sofia")
        assert "sofia@example.com" not in people()
        gone = http.get(f"https://api.notion.com/v1/users/{sofia}")
        assert gone.status_code == 404 and gone.json()["code"] == "object_not_found"


@pytest.mark.parametrize(
    ("provider", "change"),
    [("asana", "deactivate"), ("jira", "remove"), ("google_workspace", "remove"), ("github", "deactivate")],
)
def test_a_person_change_a_provider_cannot_show_is_refused_as_unsupported(
    served: Served, provider: str, change: str
) -> None:
    with _world(served, _seed(), Claims(tokens=[f"unsupported-{provider}-{change}"])) as world:
        with pytest.raises(Unsupported) as refused:
            getattr(world, f"{change}_person")(provider, "sofia")
        assert refused.value.status == 409 and provider in refused.value.error
        changed = [e for e in world.events() if e.operation is not Operation.CREATE]
        assert changed == [], "a refused change writes nothing; seeding the provider on first use writes only creates"


# ------------------------------------------------------------------ YouTrack permissions


def test_a_youtrack_permission_withheld_and_granted_again_mid_test_changes_what_the_api_reports(
    served: Served,
) -> None:
    """Minutehand enforces no permission: a withheld one leaves the read answered and is reported by Hub's
    permissions cache."""
    seed, claims = _youtrack_world("perm:trackergrant")
    with _world(served, seed, claims) as world, _http(served, {"Authorization": "Bearer perm:trackergrant"}) as http:
        issue = _ticket(world, "youtrack")
        url = f"https://trackergrant.youtrack.cloud/api/issues/{issue.external_id}?fields=idReadable"
        cache = "https://trackergrant.youtrack.cloud/hub/api/rest/permissions/cache?fields=permission/key,projects/key"

        def reads_launch() -> bool:
            held = [e for e in http.get(cache).json() if e["permission"]["key"] == "jetbrains.youtrack.readIssue"]
            return bool(held) and "LAUNCH" in [p["key"] for p in held[0]["projects"]]

        assert http.get(url).status_code == 200 and reads_launch()
        withheld = world.withhold("youtrack", "sofia", "jetbrains.youtrack.readIssue", project="LAUNCH")
        assert withheld.actor is Actor.SCENARIO
        assert http.get(url).status_code == 200 and not reads_launch()
        world.grant("youtrack", "sofia", "jetbrains.youtrack.readIssue", project="LAUNCH")
        assert reads_launch()


def test_a_youtrack_permission_it_does_not_name_is_refused(served: Served) -> None:
    seed, claims = _youtrack_world("perm:trackerbadgrant")
    with _world(served, seed, claims) as world:
        with pytest.raises(Exception) as refused:
            world.withhold("youtrack", "sofia", "jetbrains.youtrack.flyToTheMoon")
        assert "flyToTheMoon" in str(refused.value)
        with pytest.raises(Unsupported):
            world.withhold("asana", "sofia", "anything")


# ------------------------------------------------------------------ Asana's threshold for a read without `limit`


def test_asanas_threshold_for_a_read_without_limit_lowered_on_an_open_world_refuses_a_large_read(
    served: Served,
) -> None:
    seed, claims = _asana_world("asana-pages")
    with _world(served, seed, claims) as world, _asana(served, "asana-pages") as http:
        everyone = http.get(f"{ASANA}/users")
        assert everyone.status_code == 200 and len(everyone.json()["data"]) == 3
        world.declare_faults("asana", {"limits": {"unpaginated_limit": 2}})
        refused = http.get(f"{ASANA}/users")
        assert refused.status_code == 400 and "too large" in refused.text
        paged = http.get(f"{ASANA}/users", params={"limit": 2}).json()
        assert len(paged["data"]) == 2 and paged["next_page"] is not None
