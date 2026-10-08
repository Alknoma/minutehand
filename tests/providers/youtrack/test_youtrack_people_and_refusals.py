"""What happens without the agent, and what the scenario makes the instance refuse: people acting on seeded issues at
their moment, seen in the API and the activity feed as themselves; tokens and permissions, which never refuse a call;
required fields and faults, which do; each through the run's proxy."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from minutehand.adapters.providers.youtrack.seed import IssueSeed
from minutehand.domain.scenario import Comments, Deletes, Moves, Reassigns, TicketHappening, TicketState
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


def act(team: Instance, by: str, after: timedelta, action: Moves | Reassigns | Comments | Deletes) -> None:
    team.clock.jump(START + after)
    happening = TicketHappening(person=by, ticket="Write the release notes", after=after, action=action)
    team.provider.act(happening, SCENARIO, team.store, team.clock)


# --------------------------------------------------------------------------- people acting


async def test_a_person_moves_a_seeded_issue_and_the_feed_names_them(yt: httpx.AsyncClient, team: Instance) -> None:
    act(team, "tomas", timedelta(days=2), Moves(to=TicketState.DONE))

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
    act(team, "iris", timedelta(hours=5), Reassigns(to="noor"))
    act(team, "noor", timedelta(hours=6), Comments(text="Picking this up"))

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


async def test_a_person_deletes_a_seeded_issue_and_it_is_gone_404_and_a_later_act_finds_nothing(
    yt: httpx.AsyncClient, team: Instance
) -> None:
    act(team, "iris", timedelta(days=1), Deletes())

    refusal(await yt.get("/api/issues/LAUNCH-1", params={"fields": "id"}), 404)
    children = entity(await yt.get("/api/issues/LAUNCH-3", params={"fields": "links(id,issues(idReadable))"}))
    deleted = [e for e in team.store.events() if e.operation is Operation.DELETE]
    assert [(e.actor, e.sim_time) for e in deleted] == [(Actor.PERSON, START + timedelta(days=1))]
    assert all(link["issues"] == [] for link in children["links"])  # type: ignore[union-attr,index]
    unlinked = [e for e in team.store.events() if e.entity.kind is EntityKind.RECORD and e.actor is Actor.PERSON]
    assert [(e.operation, e.sim_time) for e in unlinked] == [(Operation.UPDATE, START + timedelta(days=1))]
    assert team.youtrack.links() == []
    written = len(team.store.events())
    act(team, "tomas", timedelta(days=2), Moves(to=TicketState.DONE))
    assert len(team.store.events()) == written, "a happening on a deleted issue is left alone"


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


# --------------------------------------------------------------------------- tokens: identity, never a check


async def test_an_unknown_token_acts_as_the_agent_even_once_tokens_are_seeded(team: Instance, tmp_path: Path) -> None:
    """Minutehand deliberately checks no credential: a token the seed does not name, an empty Authorization and a
    Hub call with it all act as the agent's account."""
    async with proxied(team, tmp_path / "ca", token="perm:not.a.seeded.one") as http:
        me = entity(await http.get("/api/users/me", params={"fields": "login"}))
        bare = entity(await http.get("/api/users/me", params={"fields": "login"}, headers={"Authorization": ""}))
        hub = entity(await http.get("/hub/api/rest/users/me", params={"fields": "login"}))
        tomas = entity(await http.get("/api/users/me", params={"fields": "login"}, headers=as_user(TOMAS_TOKEN)))
    assert me["login"] == bare["login"] == hub["login"] == "agent-bot"
    assert tomas["login"] == "tomas"


async def test_an_issued_token_keeps_acting_as_its_user_after_its_hour(yt: httpx.AsyncClient, team: Instance) -> None:
    issued = await yt.post(
        "/hub/api/rest/oauth2/token",
        content=f"grant_type=client_credentials&client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = issued.json()["access_token"]
    team.clock.jump(START + timedelta(hours=2))
    later = entity(await yt.get("/api/users/me", params={"fields": "login"}, headers=as_user(token)))
    unsupported = refusal(
        await yt.post(
            "/hub/api/rest/oauth2/token",
            content=b"grant_type=password",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ),
        400,
    )
    assert issued.json()["expires_in"] == 3600 and later["login"] == "agent-bot"
    assert unsupported["error"] == "unsupported_grant_type"


async def test_a_wrong_client_secret_still_gets_a_token(yt: httpx.AsyncClient) -> None:
    issued = await yt.post(
        "/hub/api/rest/oauth2/token",
        content=f"grant_type=client_credentials&client_id={CLIENT_ID}&client_secret=wrong".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    stranger = await yt.post(
        "/hub/api/rest/oauth2/token",
        content=b"grant_type=client_credentials&client_id=nobody&client_secret=nothing",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert issued.status_code == 200 and stranger.status_code == 200
    me = entity(
        await yt.get("/api/users/me", params={"fields": "login"}, headers=as_user(stranger.json()["access_token"]))
    )
    assert me["login"] == "agent-bot"


# --------------------------------------------------------------------------- permissions: reported, never enforced


def granted(tmp_path: Path, grants: list[dict[str, object]]) -> Instance:
    return seeded(tmp_path, scenario_with(grants=grants))


async def test_a_withheld_update_issue_does_not_refuse_a_field_write(tmp_path: Path) -> None:
    team = granted(
        tmp_path,
        [{"login": "tomas", "permission": "jetbrains.youtrack.updateIssue", "project": "LAUNCH", "held": False}],
    )
    async with proxied(team, tmp_path / "ca", token=TOMAS_TOKEN) as http:
        fields = entity(await http.get("/api/issues/LAUNCH-1", params={"fields": "customFields(id,name)"}))
        state = named(fields["customFields"], "State")["id"]
        written = await http.post(f"/api/issues/LAUNCH-1/customFields/{state}", json={"value": {"name": "Fixed"}})
    assert written.status_code == 200, written.text


async def test_a_withheld_read_issue_leaves_a_project_in_searches_and_readable(tmp_path: Path) -> None:
    team = granted(
        tmp_path,
        [{"login": "agent-bot", "permission": "jetbrains.youtrack.readIssue", "project": "OPS", "held": False}],
    )
    async with proxied(team, tmp_path / "ca") as http:
        found = entities(await http.get("/api/issues", params={"query": "project: OPS", "fields": "idReadable"}))
        read = entity(await http.get("/api/issues/OPS-1", params={"fields": "idReadable"}))
        counted = entity(await http.post("/api/issuesGetter/count", params={"fields": "count"}, json={"query": ""}))
    assert [i["idReadable"] for i in found] == ["OPS-1"] and read["idReadable"] == "OPS-1"
    assert counted["count"] == 4


async def test_a_withheld_create_project_does_not_refuse_a_create_and_the_cache_still_reports_it(
    tmp_path: Path,
) -> None:
    team = granted(tmp_path, [{"login": "agent-bot", "permission": "jetbrains.jetpass.project-create", "held": False}])
    async with proxied(team, tmp_path / "ca") as http:
        made = await http.post("/api/admin/projects", json={"name": "X", "shortName": "X", "leader": {"id": "1-0"}})
        attached = await http.post(
            f"/api/admin/projects/{made.json()['id']}/customFields",
            json={"field": {"id": "58-5"}, "$type": "SimpleProjectCustomField"},
        )
        cache = await http.get("/hub/api/rest/permissions/cache", params={"fields": "permission/key,global"})
    assert made.status_code == 200 and attached.status_code == 200, attached.text
    assert "jetbrains.jetpass.project-create" not in [e["permission"]["key"] for e in cache.json()]


async def test_a_withheld_update_project_does_not_refuse_a_team_change(tmp_path: Path) -> None:
    team = granted(tmp_path, [{"login": "agent-bot", "permission": "jetbrains.jetpass.project-update", "held": False}])
    vendor = team.youtrack.user_by_login("vendor")
    assert vendor is not None
    async with proxied(team, tmp_path / "ca") as http:
        added = await http.post("/api/admin/projects/0-1000/team/ownUsers", json={"id": vendor.id})
        users = entities(await http.get("/api/admin/projects/0-1000/team/users", params={"fields": "login"}))
    assert added.status_code == 200, added.text
    assert "vendor" in [u["login"] for u in users]


# --------------------------------------------------------------------------- what a project requires


async def test_a_required_field_with_no_default_refuses_a_create_without_it_400(tmp_path: Path) -> None:
    """The refusal's body is the one https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-missing-type.html
    shows: `Field required`, naming the field in `error_field`."""
    projects = json.loads(json.dumps(YOUTRACK_SEED["projects"]))
    ops_fields = projects[1]["fields"]
    ops_fields[1] = {"name": "Type", "values": [{"name": "Hardware"}, {"name": "Shipping"}], "can_be_empty": False}
    team = seeded(tmp_path, scenario_with(projects=projects, issues=[]))
    async with proxied(team, tmp_path / "ca") as http:
        refused = refusal(
            await http.post("/api/issues", json={"project": {"id": "0-1001"}, "summary": "Crate the stands"}), 400
        )
        made = entity(
            await http.post(
                "/api/issues",
                params={"fields": "customFields(name,value(name))"},
                json={
                    "project": {"id": "0-1001"},
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
    assert refused == {"error": "Field required", "error_description": "Type is required", "error_field": "Type"}
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
