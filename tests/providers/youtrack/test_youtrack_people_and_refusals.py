"""What happens without the agent, and what the scenario makes the instance refuse: people acting on seeded issues at
their moment, seen in the API and the activity feed as themselves; tokens, permissions, required fields and faults,
each through the run's proxy."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from minutehand.adapters.providers.youtrack.seed import IssueSeed
from minutehand.domain.scenario import (
    Commented,
    Reassigned,
    Removed,
    StateChanged,
    TicketHappening,
    TicketState,
)
from minutehand.domain.world import Actor, EntityKind, Operation, TicketSnapshot
from tests.providers.youtrack.caller_world import (
    CLIENT_ID,
    CLIENT_SECRET,
    SCENARIO,
    TOMAS_TOKEN,
    YOUTRACK_SEED,
    as_user,
    scenario_with,
    seeded,
)
from tests.providers.youtrack.youtrack_instance import START, Instance, entities, entity, named, proxied, refusal

DAY = 86_400_000
START_MS = 1787568603000
FEED = "IssueCreatedCategory,CustomFieldCategory,CommentsCategory"
FEED_FIELDS = "$type,timestamp,author(login),field(name),added(name,login,text),removed(name,login)"


def person(key: str):
    return next(p for p in SCENARIO.people if p.key == key)


def act(team: Instance, by: str, after: timedelta, change: StateChanged | Reassigned | Commented | Removed) -> None:
    team.clock.jump(START + after)
    team.provider.happen(
        TicketHappening(ticket="notes", by=by, after=after, change=change), person(by), team.store, team.clock
    )


# --------------------------------------------------------------------------- people acting


async def test_a_person_moves_a_seeded_issue_and_the_feed_names_them(yt: httpx.AsyncClient, team: Instance) -> None:
    act(team, "tomas", timedelta(days=2), StateChanged(to=TicketState.DONE))

    read = entity(
        await yt.get(
            "/api/issues/LAUNCH-1", params={"fields": "resolved,updater(login),customFields(name,value(name))"}
        )
    )
    feed = entities(await yt.get("/api/issues/LAUNCH-1/activities", params={"categories": FEED, "fields": FEED_FIELDS}))
    found = entities(await yt.get("/api/issues", params={"query": "#Resolved project: LAUNCH", "fields": "idReadable"}))

    assert named(read["customFields"], "State")["value"] == {"name": "Fixed", "$type": "StateBundleElement"}
    assert read["resolved"] == START_MS + 2 * DAY and read["updater"] == {"login": "tomas", "$type": "User"}
    assert feed[-1] == {
        "$type": "CustomFieldActivityItem",
        "timestamp": START_MS + 2 * DAY,
        "author": {"login": "tomas", "$type": "User"},
        "field": {"name": "State", "$type": "CustomFilterField"},
        "added": [{"name": "Fixed", "$type": "StateBundleElement"}],
        "removed": [{"name": "Open", "$type": "StateBundleElement"}],
    }
    assert [i["idReadable"] for i in found] == ["LAUNCH-1", "LAUNCH-2"]
    last = [e for e in team.store.events() if e.entity.kind is EntityKind.TICKET and e.actor is Actor.PERSON]
    assert len(last) == 1 and last[0].operation is Operation.UPDATE
    assert last[0].sim_time == START + timedelta(days=2)
    assert isinstance(last[0].after, TicketSnapshot) and last[0].after.state is TicketState.DONE


async def test_a_person_reassigns_and_comments_as_themselves(yt: httpx.AsyncClient, team: Instance) -> None:
    act(team, "iris", timedelta(hours=5), Reassigned(to="noor"))
    act(team, "noor", timedelta(hours=6), Commented(text="Picking this up"))

    comments = entities(await yt.get("/api/issues/LAUNCH-1/comments", params={"fields": "text,created,author(login)"}))
    feed = entities(await yt.get("/api/issues/LAUNCH-1/activities", params={"categories": FEED, "fields": FEED_FIELDS}))
    mine = entities(await yt.get("/api/issues", params={"query": "for: noor", "fields": "idReadable"}))

    assert comments[-1] == {
        "text": "Picking this up",
        "created": START_MS + 6 * 3_600_000,
        "author": {"login": "noor", "$type": "User"},
        "$type": "IssueComment",
    }
    assert [(a["$type"], a["author"]["login"]) for a in feed[-2:]] == [  # type: ignore[index]
        ("CustomFieldActivityItem", "iris"),
        ("CommentActivityItem", "noor"),
    ]
    assert feed[-2]["added"] == [{"name": "Noor Halvorsen", "login": "noor", "$type": "User"}]
    assert feed[-2]["removed"] == [{"name": "Tomas Brandt", "login": "tomas", "$type": "User"}]
    assert [i["idReadable"] for i in mine] == ["LAUNCH-1", "LAUNCH-2"]
    people = [e for e in team.store.events() if e.actor is Actor.PERSON]
    assert [(e.operation, e.entity.kind) for e in people] == [
        (Operation.UPDATE, EntityKind.TICKET),
        (Operation.CREATE, EntityKind.COMMENT),
    ]


async def test_a_person_deletes_a_seeded_issue_and_it_is_gone_404(yt: httpx.AsyncClient, team: Instance) -> None:
    act(team, "iris", timedelta(days=1), Removed())

    refusal(await yt.get("/api/issues/LAUNCH-1", params={"fields": "id"}), 404)
    children = entity(await yt.get("/api/issues/LAUNCH-3", params={"fields": "links(id,issues(idReadable))"}))
    deleted = [e for e in team.store.events() if e.operation is Operation.DELETE]
    assert [(e.actor, e.sim_time) for e in deleted] == [(Actor.PERSON, START + timedelta(days=1))]
    assert all(link["issues"] == [] for link in children["links"])  # type: ignore[union-attr,index]
    unlinked = [e for e in team.store.events() if e.entity.kind is EntityKind.RECORD and e.actor is Actor.PERSON]
    assert [(e.operation, e.sim_time) for e in unlinked] == [(Operation.UPDATE, START + timedelta(days=1))]
    assert team.youtrack.links() == []
    with pytest.raises(LookupError, match="notes"):
        act(team, "tomas", timedelta(days=2), StateChanged(to=TicketState.DONE))


async def test_history_seeded_before_the_run_reads_as_its_people(yt: httpx.AsyncClient) -> None:
    feed = entities(await yt.get("/api/issues/LAUNCH-3/activities", params={"categories": FEED, "fields": FEED_FIELDS}))
    newest_first = entities(
        await yt.get(
            "/api/issues/LAUNCH-3/activities",
            params={"categories": "CustomFieldCategory", "fields": "author(login)", "reverse": "true", "$top": 1},
        )
    )

    assert [(a["$type"], a["timestamp"], a["author"]["login"]) for a in feed] == [  # type: ignore[index]
        ("IssueCreatedActivityItem", START_MS - 3 * DAY, "iris"),
        ("CustomFieldActivityItem", START_MS - 2 * DAY, "tomas"),
        ("CustomFieldActivityItem", START_MS - DAY, "iris"),
    ]
    assert feed[2]["added"] == [{"name": "Show-stopper", "$type": "EnumBundleElement"}]
    assert newest_first == [{"author": {"login": "iris", "$type": "User"}, "$type": "CustomFieldActivityItem"}]
    refusal(await yt.get("/api/issues/LAUNCH-3/activities", params={"fields": "id"}), 400)


# --------------------------------------------------------------------------- tokens


async def test_an_unknown_token_is_refused_401_once_tokens_are_seeded(team: Instance, tmp_path: Path) -> None:
    async with proxied(team, tmp_path / "ca", token="perm:not.a.seeded.one") as http:
        refused = refusal(await http.get("/api/users/me", params={"fields": "id"}), 401)
        bare = refusal(await http.get("/api/users/me", headers={"Authorization": ""}), 401)
        hub = refusal(await http.get("/hub/api/rest/permissions/cache"), 401)
    assert (
        refused == bare == hub == {"error": "Unauthorized", "error_description": "Not authorized, try to login first"}
    )


async def test_an_issued_token_expires_after_an_hour_of_the_runs_time(yt: httpx.AsyncClient, team: Instance) -> None:
    issued = await yt.post(
        "/hub/api/rest/oauth2/token",
        content=f"grant_type=client_credentials&client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = issued.json()["access_token"]
    entity(await yt.get("/api/users/me", params={"fields": "id"}, headers=as_user(token)))
    team.clock.jump(START + timedelta(hours=1))
    refusal(await yt.get("/api/users/me", params={"fields": "id"}, headers=as_user(token)), 401)
    unsupported = refusal(
        await yt.post(
            "/hub/api/rest/oauth2/token",
            content=b"grant_type=password",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ),
        400,
    )
    assert unsupported["error"] == "unsupported_grant_type"


# --------------------------------------------------------------------------- permissions


def granted(tmp_path: Path, grants: list[dict[str, object]]) -> Instance:
    return seeded(tmp_path, scenario_with(grants=grants))


async def test_without_update_issue_a_field_write_is_refused_403(tmp_path: Path) -> None:
    team = granted(
        tmp_path,
        [{"login": "tomas", "permission": "jetbrains.youtrack.updateIssue", "project": "LAUNCH", "held": False}],
    )
    async with proxied(team, tmp_path / "ca", token=TOMAS_TOKEN) as http:
        fields = entity(await http.get("/api/issues/LAUNCH-1", params={"fields": "customFields(id,name)"}))
        state = named(fields["customFields"], "State")["id"]
        refused = refusal(
            await http.post(f"/api/issues/LAUNCH-1/customFields/{state}", json={"value": {"name": "Fixed"}}), 403
        )
        elsewhere = await http.post("/api/issues/OPS-1/comments", json={"text": "still mine to write"})
    assert refused == {"error": "Forbidden", "error_description": "Insufficient permissions: Update Issue is required"}
    assert elsewhere.status_code == 200


async def test_without_read_issue_a_project_is_out_of_every_search_and_its_issues_403(tmp_path: Path) -> None:
    team = granted(
        tmp_path,
        [{"login": "agent-bot", "permission": "jetbrains.youtrack.readIssue", "project": "OPS", "held": False}],
    )
    async with proxied(team, tmp_path / "ca") as http:
        found = entities(await http.get("/api/issues", params={"query": "", "fields": "idReadable"}))
        refusal(await http.get("/api/issues/OPS-1", params={"fields": "id"}), 403)
        counted = entity(await http.post("/api/issuesGetter/count", params={"fields": "count"}, json={"query": ""}))
    assert [i["idReadable"] for i in found] == ["LAUNCH-3", "LAUNCH-1", "LAUNCH-2"] and counted["count"] == 3


async def test_without_create_project_a_project_is_refused_403_and_a_grant_lets_one_be_changed(tmp_path: Path) -> None:
    team = granted(
        tmp_path,
        [
            {"login": "agent-bot", "permission": "jetbrains.jetpass.project-create", "held": False},
            {"login": "tomas", "permission": "jetbrains.jetpass.project-update", "held": True},
        ],
    )
    async with proxied(team, tmp_path / "ca") as http:
        refused = refusal(
            await http.post("/api/admin/projects", json={"name": "X", "shortName": "X", "leader": {"id": "1-0"}}), 403
        )
        made = await http.post(
            "/api/admin/projects",
            json={"name": "Summit", "shortName": "SUMMIT", "leader": {"id": "1-2"}},
            headers=as_user(TOMAS_TOKEN),
        )
        attached = await http.post(
            f"/api/admin/projects/{made.json()['id']}/customFields",
            json={"field": {"id": "58-5"}, "$type": "SimpleProjectCustomField"},
            headers=as_user(TOMAS_TOKEN),
        )
        cache = await http.get("/hub/api/rest/permissions/cache", params={"fields": "permission/key,global"})
    assert refused["error_description"] == "Insufficient permissions: Create Project is required"
    assert made.status_code == 200 and attached.status_code == 200
    assert "jetbrains.jetpass.project-create" not in [e["permission"]["key"] for e in cache.json()]


async def test_hub_refuses_a_team_change_without_update_project_403(tmp_path: Path) -> None:
    team = granted(tmp_path, [{"login": "agent-bot", "permission": "jetbrains.jetpass.project-update", "held": False}])
    launch = team.youtrack.project("0-0")
    vendor = team.youtrack.user_by_login("vendor")
    assert launch is not None and vendor is not None
    async with proxied(team, tmp_path / "ca") as http:
        refused = refusal(
            await http.post(f"/hub/api/rest/usergroups/{launch.teamRingId}/users", json={"id": vendor.ringId}), 403
        )
        groups = entity(await http.get("/hub/api/rest/usergroups", params={"fields": "name"}))
    assert refused["error_description"] == "Insufficient permissions: Update Project is required"
    assert [g["name"] for g in groups["usergroups"]] == ["All Users", "Registered Users"]  # type: ignore[union-attr,index]


# --------------------------------------------------------------------------- what a project requires


async def test_a_required_field_with_no_default_refuses_a_create_without_it_400(tmp_path: Path) -> None:
    projects = json.loads(json.dumps(YOUTRACK_SEED["projects"]))
    ops_fields = projects[1]["fields"]
    ops_fields[1] = {"name": "Type", "values": [{"name": "Hardware"}, {"name": "Shipping"}], "can_be_empty": False}
    team = seeded(tmp_path, scenario_with(projects=projects, issues=[]))
    async with proxied(team, tmp_path / "ca") as http:
        refused = refusal(
            await http.post("/api/issues", json={"project": {"id": "0-1"}, "summary": "Crate the stands"}), 400
        )
        made = entity(
            await http.post(
                "/api/issues",
                params={"fields": "customFields(name,value(name))"},
                json={
                    "project": {"id": "0-1"},
                    "summary": "Crate the stands",
                    "customFields": [
                        {"name": "Type", "$type": "SingleEnumIssueCustomField", "value": {"name": "Shipping"}}
                    ],
                },
            )
        )
        cleared = refusal(
            await http.post("/api/issues/OPS-2", json={"customFields": [{"name": "Type", "value": None}]}), 400
        )
    assert refused == {"error": "bad_request", "error_description": "Type is required"}
    assert named(made["customFields"], "Priority")["value"] == {"name": "P2 - Normal", "$type": "EnumBundleElement"}
    assert named(made["customFields"], "Type")["value"] == {"name": "Shipping", "$type": "EnumBundleElement"}
    assert cleared["error_description"] == "Value is not allowed"


# --------------------------------------------------------------------------- faults and an unknown count


async def test_a_fault_refuses_one_route_for_its_while_429_with_retry_after(tmp_path: Path) -> None:
    faults = [{"method": "POST", "path": "/issues/*/customFields/*", "status": 429, "after": "PT1H", "lasts": "PT30M"}]
    team = seeded(tmp_path, scenario_with(faults=faults))
    async with proxied(team, tmp_path / "ca") as http:
        fields = entity(await http.get("/api/issues/LAUNCH-1", params={"fields": "customFields(id,name)"}))
        state = named(fields["customFields"], "State")["id"]
        before = await http.post(f"/api/issues/LAUNCH-1/customFields/{state}", json={"value": {"name": "In Progress"}})
        team.clock.jump(START + timedelta(hours=1, minutes=10))
        during = await http.post(f"/api/issues/LAUNCH-1/customFields/{state}", json={"value": {"name": "Fixed"}})
        reads = await http.get("/api/issues/LAUNCH-1", params={"fields": "id"})
        team.clock.jump(START + timedelta(hours=1, minutes=30))
        after = await http.post(f"/api/issues/LAUNCH-1/customFields/{state}", json={"value": {"name": "Fixed"}})
    assert (before.status_code, during.status_code, reads.status_code, after.status_code) == (200, 429, 200, 200)
    assert during.headers["Retry-After"] == "1200"
    assert during.json() == {"error": "Too Many Requests", "error_description": "Too many requests. Try again later."}


async def test_a_count_still_running_answers_minus_one(tmp_path: Path) -> None:
    team = seeded(tmp_path, scenario_with(count_unknown=True))
    async with proxied(team, tmp_path / "ca") as http:
        counted = entity(await http.post("/api/issuesGetter/count", params={"fields": "count"}, json={"query": ""}))
    assert counted == {"count": -1, "$type": "IssueCountResponse"}


# --------------------------------------------------------------------------- what the seed refuses


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tokens": [{"token": "perm:x", "login": "nobody"}]}, "names 'nobody', who is no user"),
        (
            {"issues": [{"ticket": "notes", "fields": [{"field": "Priority", "value": "Urgent"}]}]},
            "has no value 'Urgent'",
        ),
        ({"issues": [{"ticket": "ghost"}]}, "describes tickets no seeded YouTrack ticket is: ghost"),
        (
            {
                "grants": [
                    {"login": "iris", "permission": "jetbrains.youtrack.readIssue", "project": "NOPE", "held": False}
                ]
            },
            "project 'NOPE'",
        ),
        ({"issues": [{"ticket": "notes", "links": [{"phrase": "blocks", "ticket": "venue"}]}]}, "reads 'blocks'"),
        (
            {"issues": [{"ticket": "notes", "fields": [{"field": "Assignee", "value": "vendor"}]}]},
            "no user on the team",
        ),
    ],
)
def test_a_seed_that_names_what_the_instance_has_not_got_is_refused(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        seeded(tmp_path, scenario_with(**change))


def test_history_before_the_issue_was_made_is_refused() -> None:
    with pytest.raises(ValidationError, match="before it was created"):
        IssueSeed.model_validate({"ticket": "notes", "history": [{"ago": "P2D", "by": "iris", "fields": []}]})
