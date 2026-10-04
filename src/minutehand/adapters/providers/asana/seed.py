"""The workspace a scenario starts in: one workspace, a user per person and one for the agent,
a project (with To do, Done and Cancelled sections) per project the scenario's Asana tickets name,
and those tickets. All of it is written as actor SCENARIO, stamped at the scenario's start."""

from __future__ import annotations

from minutehand.adapters.providers.asana import state, wire
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.state import AGENT_GID, WORKSPACE_GID, AsanaWorld
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, Operation
from minutehand.ports.store import Store


def seed(scenario: Scenario, world: Store) -> None:
    asana = AsanaWorld(world)
    at = wire.stamp(scenario.starts_at)
    asana.put_record(
        wire.AsanaWorkspace(
            gid=WORKSPACE_GID,
            name=state.WORKSPACE_NAME,
            email_domains=sorted({p.email.split("@")[-1] for p in scenario.people}),
        ),
        parent=state.WORKSPACES,
        actor=Actor.SCENARIO,
    )
    asana.put_record(
        wire.AsanaUser(gid=AGENT_GID, name=state.AGENT_NAME, email=state.AGENT_EMAIL),
        parent=state.USERS,
        actor=Actor.SCENARIO,
    )
    for person in scenario.people:
        asana.put_record(
            wire.AsanaUser(gid=state.user_gid(person.key), name=person.name, email=person.email),
            parent=state.USERS,
            actor=Actor.SCENARIO,
        )

    tickets = [t for t in scenario.tickets if t.provider == MANIFEST.key]
    for name in dict.fromkeys(t.project for t in tickets):
        gid = state.project_gid(name)
        asana.put_record(
            wire.AsanaProject(gid=gid, name=name, workspace=WORKSPACE_GID, created_at=at),
            parent=state.PROJECTS,
            actor=Actor.SCENARIO,
        )
        for role, title in state.SECTIONS:
            asana.put_record(
                wire.AsanaSection(gid=state.section_gid(gid, role), name=title, project=gid, role=role, created_at=at),
                parent=gid,
                actor=Actor.SCENARIO,
            )

    for ticket in tickets:
        project = state.project_gid(ticket.project)
        task = wire.AsanaTask(
            gid=asana.next_gid(),
            name=ticket.title,
            notes=ticket.body,
            assignee=state.user_gid(ticket.assignee) if ticket.assignee is not None else None,
            created_by=state.user_gid(scenario.owner),
            workspace=WORKSPACE_GID,
            memberships=[
                wire.AsanaMembership(project=project, section=state.section_gid(project, wire.SectionRole.TODO))
            ],
            created_at=at,
            modified_at=at,
        )
        asana.put_task(
            asana.moved(task, ticket.state, now=scenario.starts_at), operation=Operation.CREATE, actor=Actor.SCENARIO
        )
