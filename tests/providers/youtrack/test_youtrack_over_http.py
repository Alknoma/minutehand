"""A real `httpx` client over a real socket, against the app served by uvicorn.

The server runs on the test's own event loop, because the store's SQLite
connection belongs to the thread that opened it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn

from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, Operation, TicketSnapshot
from tests.providers.youtrack.youtrack_instance import (
    LAUNCH,
    TOKEN,
    Instance,
    assignee_field,
    entities,
    entity,
    refusal,
    state_field,
)


@pytest.fixture
async def served(instance: Instance) -> AsyncIterator[str]:
    server = uvicorn.Server(uvicorn.Config(
        instance.provider.app(instance.store, instance.clock), host="127.0.0.1", port=0, log_level="warning",
        lifespan="off",
    ))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await serving


@pytest.mark.parametrize("prefix", ["/api", "/youtrack/api"])
async def test_an_agent_hands_an_issue_to_a_person_over_a_socket(
    instance: Instance, served: str, prefix: str
) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    async with httpx.AsyncClient(base_url=served + prefix, headers=headers) as http:
        projects = entities(await http.get("/admin/projects", params={"fields": "id,shortName"}))
        team = entities(await http.get(f"/admin/projects/{LAUNCH}/customFields", params={
            "fields": "field(name),bundle(aggregatedUsers(login,email))",
        }))
        made = entity(await http.post("/issues", params={"fields": "id,idReadable"}, json={
            "project": {"id": projects[0]["id"]}, "summary": "Proof the programme",
            "customFields": [assignee_field("noor"), state_field("In Progress")],
        }))
        found = entities(await http.get("/issues", params={"query": "for: noor #Unresolved", "fields": "idReadable"}))
        no_token = await http.get(f"/issues/{made['id']}", headers={"Authorization": ""})

    assert projects[0] == {"id": LAUNCH, "shortName": "LAUNCH", "$type": "Project"}
    assignees = team[1]["bundle"]
    assert isinstance(assignees, dict)
    assert {"login": "noor", "email": "noor@example.com", "$type": "User"} in list(assignees["aggregatedUsers"])
    assert [i["idReadable"] for i in found] == [made["idReadable"]]
    refusal(no_token, 401)
    created = next(e for e in instance.store.events() if e.entity.external_id == made["id"])
    assert (created.actor, created.operation) == (Actor.AGENT, Operation.CREATE)
    assert created.after == TicketSnapshot(title="Proof the programme", project="LAUNCH",
                                           assignee_email="noor@example.com", state=TicketState.OPEN)
