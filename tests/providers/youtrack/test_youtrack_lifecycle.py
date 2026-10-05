"""A production client's whole lifecycle of one issue, every id discovered through the API, through the run's proxy."""

from __future__ import annotations

from datetime import timedelta

import httpx

from minutehand.domain.world import Actor, EntityKind, Operation
from tests.providers.youtrack.youtrack_instance import START, Instance, entities, entity, named, refusal


async def test_find_create_move_assign_comment_link_search_read_history_delete(
    yt: httpx.AsyncClient, team: Instance
) -> None:
    projects = entities(await yt.get("/api/admin/projects", params={"fields": "id,shortName,name", "$top": 100}))
    launch = next(p for p in projects if p["shortName"] == "LAUNCH")
    shape = entity(
        await yt.get(
            "/api/admin/projects/LAUNCH",
            params={"fields": "id,shortName,customFields(field(id,name),bundle(values(id,name,ordinal)))"},
        )
    )
    states = [v["name"] for v in named(shape["customFields"], "State")["bundle"]["values"]]  # type: ignore[index]
    people = entities(
        await yt.get(
            f"/api/admin/projects/{launch['id']}/customFields",
            params={"fields": "field(id,name),bundle(id,aggregatedUsers(id,login,name,fullName,email))"},
        )
    )
    noor = next(u for u in named(people, "Assignee")["bundle"]["aggregatedUsers"] if u["login"] == "noor")  # type: ignore[index,union-attr]

    team.clock.jump(START + timedelta(hours=1))
    made = entity(
        await yt.post(
            "/api/issues",
            params={"fields": "id,idReadable,summary"},
            json={"project": {"id": launch["id"]}, "summary": "Order the lanyards", "description": "Two hundred"},
        )
    )
    key = made["idReadable"]
    fields = entity(await yt.get(f"/api/issues/{key}", params={"fields": "customFields(id,name)"}))
    state_id = named(fields["customFields"], "State")["id"]
    assignee_id = named(fields["customFields"], "Assignee")["id"]
    team.clock.jump(START + timedelta(hours=2))
    entity(await yt.post(f"/api/issues/{key}/customFields/{state_id}", json={"value": {"name": "In Progress"}}))
    entity(await yt.post(f"/api/issues/{key}/customFields/{assignee_id}", json={"value": {"id": noor["id"]}}))
    entity(
        await yt.post(f"/api/issues/{key}/comments", params={"fields": "id"}, json={"text": "Noor has the supplier"})
    )
    types = entities(
        await yt.get("/api/issueLinkTypes", params={"fields": "id,sourceToTarget,targetToSource,directed"})
    )
    subtask = next(t for t in types if t["targetToSource"] == "subtask of")
    parent = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "id"}))
    linked = await yt.post(f"/api/issues/{key}/links/{subtask['id']}t/issues", json={"id": parent["id"]})

    searches = {
        query: [
            i["idReadable"]
            for i in entities(await yt.get("/api/issues", params={"query": query, "fields": "idReadable"}))
        ]
        for query in (
            "project: LAUNCH",
            "assignee: noor",
            "State: {In Progress}",
            "#Unresolved",
            "lanyards",
            f"issue id: {key}",
            "created: 2026-08-24",
        )
    }
    history = entities(
        await yt.get(
            f"/api/issues/{key}/activities",
            params={
                "categories": "IssueCreatedCategory,CustomFieldCategory,CommentsCategory,LinksCategory",
                "fields": "timestamp,author(login),category(id),field(name),added(name,login,text,idReadable),removed(name,login)",
            },
        )
    )
    parents_children = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "links(id,issues(idReadable))"}))
    gone = await yt.delete(f"/api/issues/{key}")
    after = await yt.get(f"/api/issues/{key}", params={"fields": "id"})

    assert key == "LAUNCH-4" and "In Progress" in states and linked.status_code == 200
    assert all(key in found for found in searches.values()), searches
    hour, two = 1787568603000 + 3600000, 1787568603000 + 7200000
    assert [(a["$type"], a["timestamp"], a["author"]["login"]) for a in history] == [  # type: ignore[index]
        ("IssueCreatedActivityItem", hour, "agent-bot"),
        ("CustomFieldActivityItem", two, "agent-bot"),
        ("CustomFieldActivityItem", two, "agent-bot"),
        ("CommentActivityItem", two, "agent-bot"),
        ("LinkActivityItem", two, "agent-bot"),
    ]
    assert (history[1]["field"], history[1]["added"], history[1]["removed"]) == (
        {"name": "State", "$type": "CustomFilterField"},
        [{"name": "In Progress", "$type": "StateBundleElement"}],
        [{"name": "Open", "$type": "StateBundleElement"}],
    )
    assert history[2]["added"] == [{"name": "Noor Halvorsen", "login": "noor", "$type": "User"}]
    assert history[2]["removed"] == []
    assert history[3]["added"] == [{"text": "Noor has the supplier", "$type": "IssueComment"}]
    assert history[4]["added"] == [{"idReadable": "LAUNCH-1", "$type": "Issue"}]
    children = next(link for link in parents_children["links"] if link["id"] == "106-0s")  # type: ignore[union-attr]
    assert children["issues"] == [{"idReadable": "LAUNCH-3", "$type": "Issue"}, {"idReadable": key, "$type": "Issue"}]  # type: ignore[index]
    assert gone.status_code == 200
    refusal(after, 404)
    writes = [e for e in team.store.events() if e.operation not in (Operation.READ, Operation.SEARCH)]
    mine = [e for e in writes if e.entity.kind in (EntityKind.TICKET, EntityKind.COMMENT) and e.actor is Actor.AGENT]
    assert [(e.operation, e.entity.kind) for e in mine] == [
        (Operation.CREATE, EntityKind.TICKET),
        (Operation.UPDATE, EntityKind.TICKET),
        (Operation.UPDATE, EntityKind.TICKET),
        (Operation.CREATE, EntityKind.COMMENT),
        (Operation.DELETE, EntityKind.TICKET),
    ]
