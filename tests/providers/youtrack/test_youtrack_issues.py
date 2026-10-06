"""Issues as an agent's YouTrack client reads and writes them: ids, queries, `fields=`, time, snapshots, commands."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from minutehand.adapters.providers.youtrack import state
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, EntityKind, Operation, TicketSnapshot
from tests.providers.youtrack.youtrack_instance import (
    AGENT_ID,
    FIELD_OPS,
    LAUNCH,
    START,
    TOMAS,
    Instance,
    assignee_field,
    create,
    entities,
    entity,
    millis_now,
    named,
    readable_ids,
    state_field,
)


async def test_a_created_issue_reads_back_by_its_database_id_and_its_readable_id(client: httpx.AsyncClient) -> None:
    made = await create(client, "Draft the press kit", description="Two pages")
    assert made["idReadable"] == "LAUNCH-3"

    fields = {"fields": "id,idReadable,summary,description,project(shortName)"}
    by_id = entity(await client.get(f"/api/issues/{made['id']}", params=fields))
    by_readable = entity(await client.get("/api/issues/LAUNCH-3", params=fields))

    assert by_id == by_readable
    assert by_id["summary"] == "Draft the press kit" and by_id["description"] == "Two pages"
    assert by_id["project"] == {"shortName": "LAUNCH", "$type": "Project"}


async def test_numbers_count_per_project_and_database_ids_follow_the_log(client: httpx.AsyncClient) -> None:
    first = await create(client, "One", project=FIELD_OPS)
    second = await create(client, "Two", project=FIELD_OPS)

    assert (first["idReadable"], second["idReadable"]) == ("FIELDOPS-2", "FIELDOPS-3")
    assert first["id"] != second["id"]
    assert all(str(i["id"]).startswith("2-") for i in (first, second))


async def test_a_created_issue_lists_under_its_project(client: httpx.AsyncClient) -> None:
    await create(client, "Print the badges")

    listed = entities(await client.get("/api/issues", params={"query": "project: LAUNCH", "fields": "idReadable"}))
    elsewhere = entities(
        await client.get("/api/issues", params={"query": "project: {Field Ops}", "fields": "idReadable"})
    )

    assert readable_ids(listed) == ["LAUNCH-1", "LAUNCH-2", "LAUNCH-3"]
    assert readable_ids(elsewhere) == ["FIELDOPS-1"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("for: tomas", ["LAUNCH-1", "LAUNCH-3"]),
        ("Assignee: tomas", ["LAUNCH-1", "LAUNCH-3"]),
        ("for: me", ["LAUNCH-4"]),
        ("Assignee: Unassigned", ["FIELDOPS-1"]),
        ("#Resolved", ["LAUNCH-2"]),
        ("#Unresolved project: LAUNCH", ["LAUNCH-1", "LAUNCH-3", "LAUNCH-4"]),
        ("State: {In Progress}", ["LAUNCH-3"]),
        ("State: Fixed, {In Progress}", ["LAUNCH-2", "LAUNCH-3"]),
        ("badges", ["LAUNCH-3"]),
        ("venue forty", ["LAUNCH-2"]),
        ("issue id: LAUNCH-4", ["LAUNCH-4"]),
        ("project: LAUNCH for: tomas #Unresolved badges", ["LAUNCH-3"]),
        ("", ["LAUNCH-1", "LAUNCH-2", "FIELDOPS-1", "LAUNCH-3", "LAUNCH-4"]),
    ],
)
async def test_each_supported_query_finds_what_it_names(
    client: httpx.AsyncClient, query: str, expected: list[str]
) -> None:
    await create(client, "Print the badges", customFields=[state_field("In Progress"), assignee_field("tomas")])
    await create(client, "Chase the caterer", customFields=[assignee_field("agent-bot")])

    found = entities(await client.get("/api/issues", params={"query": query, "fields": "idReadable"}))

    assert readable_ids(found) == expected


async def test_an_issue_without_fields_is_its_id_and_type_alone(client: httpx.AsyncClient) -> None:
    answer = entity(await client.get("/api/issues/LAUNCH-1"))

    assert answer == {"id": answer["id"], "$type": "Issue"}


async def test_fields_select_nested_fields_and_nothing_else(client: httpx.AsyncClient) -> None:
    answer = entity(
        await client.get(
            "/api/issues/LAUNCH-1",
            params={
                "fields": "idReadable,reporter,customFields(name,value(name,login)),project(id,team(users(login)))",
            },
        )
    )

    assert set(answer) == {"idReadable", "reporter", "customFields", "project", "$type"}
    assert answer["reporter"] == {"id": "1-1", "$type": "User"}
    assert answer["customFields"] == [
        {
            "name": "Priority",
            "value": {"name": "Normal", "$type": "EnumBundleElement"},
            "$type": "SingleEnumIssueCustomField",
        },
        {"name": "Type", "value": {"name": "Bug", "$type": "EnumBundleElement"}, "$type": "SingleEnumIssueCustomField"},
        {"name": "State", "value": {"name": "Open", "$type": "StateBundleElement"}, "$type": "StateIssueCustomField"},
        {
            "name": "Assignee",
            "value": {"name": "Tomas Brandt", "login": "tomas", "$type": "User"},
            "$type": "SingleUserIssueCustomField",
        },
        {"name": "Due Date", "value": None, "$type": "DateIssueCustomField"},
        {"name": "Estimation", "value": None, "$type": "PeriodIssueCustomField"},
        {"name": "Spent time", "value": None, "$type": "PeriodIssueCustomField"},
        {"name": "Story Points", "value": None, "$type": "SimpleIssueCustomField"},
    ]
    project = answer["project"]
    assert isinstance(project, dict)
    assert project["id"] == LAUNCH and set(project) == {"id", "team", "$type"}
    assert project["team"] == {
        "users": [{"login": login, "$type": "User"} for login in ("agent-bot", "iris", "tomas", "noor")],
        "$type": "ProjectTeam",
    }


async def test_a_list_applies_fields_to_every_entity(client: httpx.AsyncClient) -> None:
    listed = entities(await client.get("/api/admin/projects", params={"fields": "shortName,leader(login)"}))

    assert listed == [
        {"shortName": "LAUNCH", "leader": {"login": "agent-bot", "$type": "User"}, "$type": "Project"},
        {"shortName": "FIELDOPS", "leader": {"login": "agent-bot", "$type": "User"}, "$type": "Project"},
    ]


async def test_timestamps_are_the_clock_in_milliseconds(instance: Instance, client: httpx.AsyncClient) -> None:
    instance.clock.jump(instance.clock.now() + timedelta(days=3, hours=2))
    made = await create(client, "Confirm the caterer", fields="id,created,updated,resolved")
    created = millis_now(instance.clock)

    instance.clock.jump(instance.clock.now() + timedelta(hours=5))
    updated = entity(
        await client.post(
            f"/api/issues/{made['id']}",
            params={"fields": "created,updated,resolved"},
            json={"customFields": [state_field("Fixed")]},
        )
    )

    assert (made["created"], made["updated"], made["resolved"]) == (created, created, None)
    assert updated["created"] == created
    assert updated["updated"] == updated["resolved"] == millis_now(instance.clock) == created + 5 * 3_600_000


async def test_seeded_issues_carry_the_scenario_start_as_their_time(client: httpx.AsyncClient) -> None:
    seeded = entity(await client.get("/api/issues/LAUNCH-2", params={"fields": "created,resolved"}))

    assert seeded["created"] == seeded["resolved"] == state.millis(START)


@pytest.mark.parametrize(
    ("value", "outcome"),
    [
        ("Open", TicketState.OPEN),
        ("In Progress", TicketState.OPEN),
        ("Fixed", TicketState.DONE),
        ("Won't fix", TicketState.CANCELLED),
    ],
)
async def test_the_snapshot_carries_the_assignee_email_and_the_state_the_value_means(
    instance: Instance, client: httpx.AsyncClient, value: str, outcome: TicketState
) -> None:
    made = await create(client, "Hand over the keys", description="Front desk")
    entity(
        await client.post(
            f"/api/issues/{made['id']}", json={"customFields": [state_field(value), assignee_field("noor")]}
        )
    )

    events = [e for e in instance.store.events() if e.entity.external_id == made["id"]]
    assert [(e.operation, e.actor, e.entity.kind) for e in events] == [
        (Operation.CREATE, Actor.AGENT, EntityKind.TICKET),
        (Operation.UPDATE, Actor.AGENT, EntityKind.TICKET),
    ]
    assert events[0].after == TicketSnapshot(title="Hand over the keys", body="Front desk", project="LAUNCH")
    assert events[1].after == TicketSnapshot(
        title="Hand over the keys",
        body="Front desk",
        project="LAUNCH",
        assignee_email="noor@example.com",
        state=outcome,
    )


async def test_reads_and_searches_are_recorded_as_the_agent(instance: Instance, client: httpx.AsyncClient) -> None:
    before = instance.store.head()
    entity(await client.get("/api/issues/LAUNCH-1"))
    entities(await client.get("/api/issues", params={"query": "project: LAUNCH"}))
    entities(await client.get("/api/issues", params={"query": "badges"}))
    entity(await client.get("/api/users/me"))

    seen = [(e.operation, e.actor, e.entity.external_id) for e in instance.store.events(since=before)]
    first = instance.youtrack.find_issue("LAUNCH-1")
    assert first is not None
    assert seen == [
        (Operation.READ, Actor.AGENT, first.id),
        (Operation.SEARCH, Actor.AGENT, LAUNCH),
        (Operation.SEARCH, Actor.AGENT, state.INSTANCE),
        (Operation.READ, Actor.AGENT, AGENT_ID),
    ]


async def test_top_and_skip_page_without_repeating(client: httpx.AsyncClient) -> None:
    for n in range(4):
        await create(client, f"Task {n}")

    pages = [
        readable_ids(
            entities(
                await client.get(
                    "/api/issues",
                    params={
                        "query": "project: LAUNCH",
                        "fields": "idReadable",
                        "$skip": str(skip),
                        "$top": "2",
                    },
                )
            )
        )
        for skip in (0, 2, 4)
    ]
    everything = readable_ids(
        entities(
            await client.get(
                "/api/issues",
                params={
                    "query": "project: LAUNCH",
                    "fields": "idReadable",
                    "$top": "-1",
                },
            )
        )
    )

    assert pages == [["LAUNCH-1", "LAUNCH-2"], ["LAUNCH-3", "LAUNCH-4"], ["LAUNCH-5", "LAUNCH-6"]]
    assert everything == [i for page in pages for i in page]


async def test_a_collection_with_no_top_stops_at_forty_two(client: httpx.AsyncClient) -> None:
    for n in range(42):
        await create(client, f"Task {n}", project=FIELD_OPS)

    assert len(entities(await client.get("/api/issues", params={"query": "project: FIELDOPS", "$top": "-1"}))) == 43
    assert len(entities(await client.get("/api/issues", params={"query": "project: FIELDOPS"}))) == 42


async def test_a_command_moves_state_and_assignee_and_comments(instance: Instance, client: httpx.AsyncClient) -> None:
    made = await create(client, "Order the lanyards")

    answer = entity(
        await client.post(
            "/api/commands",
            params={"fields": "issues(idReadable)"},
            json={
                "query": "State In Progress for tomas",
                "issues": [{"idReadable": made["idReadable"]}],
                "comment": "Taking this on",
            },
        )
    )
    after = entity(
        await client.get(
            f"/api/issues/{made['id']}",
            params={
                "fields": "customFields(name,value(name,login)),comments(text,author(login))",
            },
        )
    )

    assert answer == {"issues": [{"idReadable": "LAUNCH-3", "$type": "Issue"}], "$type": "CommandList"}
    assert named(after["customFields"], "State") == {
        "name": "State",
        "value": {"name": "In Progress", "$type": "StateBundleElement"},
        "$type": "StateIssueCustomField",
    }
    assert named(after["customFields"], "Assignee") == {
        "name": "Assignee",
        "value": {"name": "Tomas Brandt", "login": "tomas", "$type": "User"},
        "$type": "SingleUserIssueCustomField",
    }
    assert after["comments"] == [
        {"text": "Taking this on", "author": {"login": "agent-bot", "$type": "User"}, "$type": "IssueComment"},
    ]
    writes = [e for e in instance.store.events() if e.operation not in (Operation.READ, Operation.SEARCH)]
    kinds = [(e.operation, e.entity.kind) for e in writes][-2:]
    assert kinds == [(Operation.UPDATE, EntityKind.TICKET), (Operation.CREATE, EntityKind.COMMENT)]


@pytest.mark.parametrize(
    ("query", "login", "outcome"),
    [
        ("State Fixed", "tomas", TicketState.DONE),
        ("state {Won't fix}", "tomas", TicketState.CANCELLED),
        ("Assignee iris", "iris", TicketState.OPEN),
        ("for me", "agent-bot", TicketState.OPEN),
    ],
)
async def test_each_command_lands_on_the_issue_by_database_id(
    instance: Instance, client: httpx.AsyncClient, query: str, login: str, outcome: TicketState
) -> None:
    first = instance.youtrack.find_issue("LAUNCH-1")
    assert first is not None
    entity(await client.post("/api/commands", json={"query": query, "issues": [{"id": first.id}]}))

    moved = instance.youtrack.issue(first.id)
    home = instance.youtrack.project(first.project)
    assert moved is not None and home is not None
    assert instance.youtrack.assignee_of(home, moved) == instance.youtrack.user_by_login(login)
    last = instance.store.events()[-1]
    assert isinstance(last.after, TicketSnapshot) and last.after.state is outcome


async def test_a_comment_posts_and_lists(instance: Instance, client: httpx.AsyncClient) -> None:
    posted = entity(
        await client.post(
            "/api/issues/LAUNCH-1/comments", params={"fields": "id,text,created"}, json={"text": "Is the draft ready?"}
        )
    )
    listed = entities(await client.get("/api/issues/LAUNCH-1/comments", params={"fields": "text,author(fullName)"}))

    assert posted["text"] == "Is the draft ready?" and posted["created"] == millis_now(instance.clock)
    assert str(posted["id"]).startswith("4-")
    assert listed == [
        {"text": "Is the draft ready?", "author": {"fullName": "Agent", "$type": "User"}, "$type": "IssueComment"}
    ]
    written = instance.store.events()[-2]
    assert (written.operation, written.actor, written.entity.kind) == (
        Operation.CREATE,
        Actor.AGENT,
        EntityKind.COMMENT,
    )


async def test_the_project_and_issue_custom_fields_describe_state_and_assignee(client: httpx.AsyncClient) -> None:
    project_fields = entities(
        await client.get(
            f"/api/admin/projects/{LAUNCH}/customFields",
            params={
                "fields": "field(name),bundle(values(name,isResolved),aggregatedUsers(login))",
            },
        )
    )
    issue_fields = entities(
        await client.get(
            "/api/issues/LAUNCH-2/customFields",
            params={
                "fields": "name,value(name,isResolved,login)",
            },
        )
    )

    assert [named(project_fields, n)["$type"] for n in ("Priority", "State", "Assignee", "Due Date", "Estimation")] == [
        "EnumProjectCustomField",
        "StateProjectCustomField",
        "UserProjectCustomField",
        "SimpleProjectCustomField",
        "PeriodProjectCustomField",
    ]
    assert named(project_fields, "Due Date")["bundle"] is None
    assert named(project_fields, "State")["bundle"] == {
        "values": [
            {"name": "Open", "isResolved": False, "$type": "StateBundleElement"},
            {"name": "In Progress", "isResolved": False, "$type": "StateBundleElement"},
            {"name": "Fixed", "isResolved": True, "$type": "StateBundleElement"},
            {"name": "Won't fix", "isResolved": True, "$type": "StateBundleElement"},
        ],
        "$type": "StateBundle",
    }
    assert named(project_fields, "Assignee")["bundle"] == {
        "aggregatedUsers": [{"login": login, "$type": "User"} for login in ("agent-bot", "iris", "tomas", "noor")],
        "$type": "UserBundle",
    }
    assert named(issue_fields, "State") == {
        "name": "State",
        "value": {"name": "Fixed", "isResolved": True, "$type": "StateBundleElement"},
        "$type": "StateIssueCustomField",
    }
    assert named(issue_fields, "Assignee") == {
        "name": "Assignee",
        "value": {"name": "Noor Halvorsen", "login": "noor", "$type": "User"},
        "$type": "SingleUserIssueCustomField",
    }


async def test_users_me_and_the_user_directory(client: httpx.AsyncClient) -> None:
    me = entity(await client.get("/api/users/me", params={"fields": "login,email"}))
    found = entities(await client.get("/api/users", params={"query": "brandt", "fields": "id,login,email"}))

    assert me == {"login": "agent-bot", "email": "agent-bot@youtrack.invalid", "$type": "Me"}
    assert found == [{"id": TOMAS, "login": "tomas", "email": "tomas@example.com", "$type": "User"}]


async def test_a_project_reads_by_id_and_by_short_name(client: httpx.AsyncClient) -> None:
    by_id = entity(await client.get(f"/api/admin/projects/{FIELD_OPS}", params={"fields": "id,name,shortName"}))
    by_key = entity(await client.get("/api/admin/projects/FIELDOPS", params={"fields": "id,name,shortName"}))

    assert by_id == by_key == {"id": FIELD_OPS, "name": "Field Ops", "shortName": "FIELDOPS", "$type": "Project"}


async def test_a_deleted_issue_is_gone_and_its_number_is_not_reused(
    instance: Instance, client: httpx.AsyncClient
) -> None:
    made = await create(client, "Throwaway")
    deleted = await client.delete(f"/api/issues/{made['idReadable']}")

    assert deleted.status_code == 200
    assert (await client.get(f"/api/issues/{made['id']}")).status_code == 404
    assert (await create(client, "Next"))["idReadable"] == "LAUNCH-4"
    assert instance.store.events()[-3].operation is Operation.DELETE
