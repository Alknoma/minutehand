"""An OAuth 2.0 (3LO) app's whole conversation with one issue, every id read off an earlier answer: refresh the
token, find the site, find the project, its issue types, statuses and transitions, create, comment, walk the
workflow to Done, find it by each JQL shape, read its changelog, delete it and get 404."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, EntityKind, Operation, TicketSnapshot
from tests.providers.jira.jira_site import (
    AGENT,
    CLIENT_ID,
    CLIENT_SECRET,
    CLOUD_ID,
    IRIS,
    OAUTH_REFRESH,
    START,
    Site,
    ok,
)


def _doc(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


async def test_an_oauth_app_runs_an_issue_from_creation_to_deletion(site: Site) -> None:
    site.clock.jump(START + timedelta(hours=2))
    async with site.client(None) as anonymous:
        token = ok(
            await anonymous.post(
                "https://auth.atlassian.com/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "refresh_token": OAUTH_REFRESH,
                },
            )
        )
        assert (token["token_type"], token["expires_in"]) == ("Bearer", 3600)
        assert token["refresh_token"] != OAUTH_REFRESH
        reused = await anonymous.post(
            "https://auth.atlassian.com/oauth/token",
            json={"grant_type": "refresh_token", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                  "refresh_token": OAUTH_REFRESH},
        )  # fmt: skip
        again = ok(reused)
        assert again["access_token"] != token["access_token"], "refresh tokens rotate"
        async with site.client(f"Bearer {again['access_token']}") as spent:
            ex = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"
            assert ok(await spent.get(f"{ex}/myself"))["accountId"] == AGENT, "a spent grant is no one's: the agent's"

    async with site.client(f"Bearer {token['access_token']}") as app:
        [resource] = ok(await app.get("https://api.atlassian.com/oauth/token/accessible-resources"))
        assert (resource["url"], resource["name"]) == ("https://lanternworks.atlassian.net", "lanternworks")
        api = f"https://api.atlassian.com/ex/jira/{resource['id']}/rest/api/3"

        me = ok(await app.get(f"{api}/myself"))
        assert me["accountId"] == IRIS
        projects = ok(await app.get(f"{api}/project/search"))["values"]
        launch = next(p for p in projects if p["name"] == "Launch")
        assert launch["self"].startswith(api)
        types = ok(await app.get(f"{api}/issue/createmeta/{launch['key']}/issuetypes"))["issueTypes"]
        task = next(t for t in types if t["name"] == "Task")
        statuses = ok(await app.get(f"{api}/project/{launch['key']}/statuses"))
        done = next(s for t in statuses if t["id"] == task["id"] for s in t["statuses"] if s["name"] == "Done")
        assert done["statusCategory"]["key"] == "done"

        made = ok(
            await app.post(
                f"{api}/issue",
                json={
                    "fields": {
                        "project": {"id": launch["id"]},
                        "issuetype": {"id": task["id"]},
                        "summary": "Hire the photographer",
                        "description": _doc("Two hours on launch day."),
                        "assignee": {"accountId": me["accountId"]},
                    }
                },
            ),
            201,
        )
        key = made["key"]
        comment = ok(await app.post(f"{api}/issue/{key}/comment", json={"body": _doc("Three quotes in.")}), 201)
        assert comment["author"]["accountId"] == IRIS

        site.clock.jump(START + timedelta(hours=2, minutes=30))
        path: list[str] = []
        while True:
            issue = ok(await app.get(f"{api}/issue/{key}", params={"fields": "status"}))
            if issue["fields"]["status"]["id"] == done["id"]:
                break
            offered = ok(await app.get(f"{api}/issue/{key}/transitions", params={"expand": "transitions.fields"}))
            step = next(
                (t for t in offered["transitions"] if t["to"]["id"] == done["id"]),
                next(t for t in offered["transitions"] if t["to"]["statusCategory"]["key"] != "done"),
            )
            body: dict[str, Any] = {"transition": {"id": step["id"]}}
            if "resolution" in step.get("fields", {}):
                body["fields"] = {"resolution": {"name": "Done"}}
            assert (await app.post(f"{api}/issue/{key}/transitions", json=body)).status_code == 204
            path.append(step["name"])
        assert path == ["Start work", "Submit for review", "Approve"]

        read = ok(await app.get(f"{api}/issue/{key}", params={"fields": "resolution,resolutiondate,status"}))
        assert read["fields"]["resolution"]["name"] == "Done"
        assert read["fields"]["resolutiondate"] == "2026-08-24T13:20:03.000+0000"

        for jql in (
            f"project = {launch['key']}",
            f'project = "{launch["key"]}"',
            'status = "Done"',
            'status in (Done, "Won\'t Do")',
            "statusCategory = Done",
            "resolution = Done",
            "assignee = currentUser()",
            f'assignee = "{me["accountId"]}"',
            'text ~ "photographer"',
            'text ~ "quotes"',
            f'issuetype = "{task["name"]}"',
            'priority = "Medium"',
            "updated >= -1h",
            "created >= startOfDay()",
            f"project = {launch['key']} AND NOT statusCategory != Done ORDER BY updated DESC",
            "duedate is EMPTY AND labels is EMPTY",
        ):
            found = ok(await app.post(f"{api}/search/jql", json={"jql": jql, "fields": ["summary"]}))
            assert key in [i["key"] for i in found["issues"]], jql
        assert key not in [
            i["key"] for i in ok(await app.post(f"{api}/search/jql", json={"jql": "resolution = Unresolved"}))["issues"]
        ]

        log = ok(await app.get(f"{api}/issue/{key}/changelog"))
        moves = [
            (item["fromString"], item["toString"])
            for entry in log["values"]
            for item in entry["items"]
            if item["field"] == "status"
        ]
        assert moves == [("To Do", "In Progress"), ("In Progress", "In Review"), ("In Review", "Done")]
        assert {entry["author"]["accountId"] for entry in log["values"]} == {IRIS}
        resolution = [i for e in log["values"] for i in e["items"] if i["field"] == "resolution"]
        assert [(i["fromString"], i["toString"]) for i in resolution] == [(None, "Done")]

        assert (await app.delete(f"{api}/issue/{key}")).status_code == 204
        gone = await app.get(f"{api}/issue/{key}")
        assert gone.status_code == 404 and gone.json()["errorMessages"]

    tickets = [
        e for e in site.store.events() if e.entity.kind is EntityKind.TICKET and e.entity.external_id == made["id"]
    ]
    assert tickets[0].operation is Operation.CREATE and tickets[-1].operation is Operation.DELETE
    assert {e.actor for e in tickets} == {Actor.AGENT}
    finished = [e.after for e in tickets if isinstance(e.after, TicketSnapshot)][-1]
    assert finished == TicketSnapshot(
        title="Hire the photographer",
        body="Two hours on launch day.",
        project="LAUNCH",
        assignee_email="iris@example.com",
        state=TicketState.DONE,
    )
