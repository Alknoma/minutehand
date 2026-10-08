"""Facts about how Asana answers, each pinned through the real proxy over TLS at app.asana.com/api/1.0.

Every case here is a claim the emulator this provider replaced was tested for and that no other test in this
directory already pinned (`CLAIMS.md` beside the provider maps each claim to the test that holds it). A
docstring says whether the claim is DOCUMENTED, with the page that says so, or OBSERVED: asserted by the old
emulator on someone's reading of a real response and not stated in Asana's reference.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.provider import build
from minutehand.adapters.providers.asana.seed import AsanaSeed
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, SeededTicket, TicketState
from tests.providers.asana.asana_workspace import START

API = "https://app.asana.com/api/1.0"
TOKEN = "pat-surveyor"
STAGES = ["Queued", "Sounding", "Charted"]
UNKNOWN = "1987654321012345"

SEED = AsanaSeed.model_validate(
    {
        "workspace": {"name": "Estuary Works", "organization": True, "premium": True},
        "teams": [{"name": "Survey"}],
        "custom_fields": [
            {"name": "Stage", "kind": "enum", "options": [{"name": n} for n in STAGES]},
            {"name": "Depth", "kind": "number", "precision": 1},
        ],
        "tags": ["silt"],
        "projects": [
            {
                "name": "Harbour Survey",
                "team": "Survey",
                "sections": [{"name": n} for n in STAGES],
                "custom_fields": ["Stage"],
            },
            {"name": "Tide Tables", "team": "Survey"},
            {"name": "Old Dredging", "team": "Survey", "archived": True},
        ],
        "tasks": [
            {
                "ticket": "Sound the north channel",
                "section": "Sounding",
                "values": [{"field": "Stage", "option": "Sounding"}],
            },
            {"ticket": "Calibrate the echo sounder", "parent": "Sound the north channel"},
        ],
        "status": {
            "kind": "custom_field",
            "field": "Stage",
            "means": {"Queued": "open", "Sounding": "open", "Charted": "done"},
        },
        "tokens": [{"token": TOKEN}],
    }
)

SCENARIO = Scenario(
    name="estuary_survey",
    goal="Every channel in the harbour is sounded and charted.",
    owner="wren",
    starts_at=START,
    people=[
        Person(key="wren", name="Wren Okafor", email="wren@estuary.example"),
        Person(key="otto", name="Otto Lindqvist", email="otto@estuary.example"),
    ],
    tickets=[
        SeededTicket(provider="asana", project="Harbour Survey", title="Sound the north channel", assignee="otto"),
        SeededTicket(provider="asana", project="Harbour Survey", title="Calibrate the echo sounder"),
        SeededTicket(provider="asana", project="Tide Tables", title="Print the spring tables"),
        SeededTicket(provider="asana", project="Old Dredging", title="Close the dredging log", state=TicketState.DONE),
    ],
    provider_seeds=[ProviderSeed(provider="asana", body=SEED.model_dump_json())],
)

WS = state.WORKSPACE_GID
HARBOUR = state.project_gid("Harbour Survey")
TIDES = state.project_gid("Tide Tables")
DREDGING = state.project_gid("Old Dredging")
SURVEY = state.team_gid("Survey")
STAGE = state.field_gid("Stage")
DEPTH = state.field_gid("Depth")
SILT = state.tag_gid("silt")
CHANNEL = state.task_gid(0)
SOUNDER = state.task_gid(1)
SPRING_TABLES = state.task_gid(2)


def stage_section(name: str) -> str:
    return state.section_gid(HARBOUR, STAGES.index(name))


@dataclass
class Proxied:
    http: httpx.AsyncClient
    store: SqliteStore


@asynccontextmanager
async def proxied(scenario: Scenario, tmp_path: Path) -> AsyncIterator[Proxied]:
    """The scenario's Asana behind the real proxy, and a client that trusts its CA and sends the token."""
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    registry = Registry()
    registry.register(MANIFEST, lambda: provider)
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        trust = ssl.create_default_context(cafile=str(proxy.ca_cert))
        async with httpx.AsyncClient(
            base_url=API,
            proxy=proxy.url,
            verify=trust,
            trust_env=False,
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
        ) as http:
            yield Proxied(http=http, store=store)


@pytest.fixture
async def asana(tmp_path: Path) -> AsyncIterator[Proxied]:
    async with proxied(SCENARIO, tmp_path) as found:
        yield found


def got(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()["data"]


def refused(response: httpx.Response, status: int) -> str:
    """The one message of an Asana refusal, after checking the status and the envelope."""
    assert response.status_code == status, response.text
    errors = response.json()["errors"]
    assert isinstance(errors, list) and len(errors) == 1, errors
    message = errors[0]["message"]
    assert isinstance(message, str)
    return message


# ---------------------------------------------------------------------------------------------- authentication


async def test_a_write_with_no_token_is_answered_as_the_agent(asana: Proxied) -> None:
    """Minutehand does not enforce credentials (CLAIMS.md): Asana answers a write with no token 401 "Not Authorized"
    (`tests/data/asana_rest_1_0/real-service-without-a-token-2026-10-08.txt`), and this fake writes it, as the
    agent."""
    bare = {"Authorization": ""}
    project = await asana.http.post(
        "/projects", headers=bare, json={"data": {"workspace": WS, "team": SURVEY, "name": "Buoy Moorings"}}
    )
    assert project.status_code == 201, project.text
    assert [m["gid"] for m in project.json()["data"]["members"]] == [state.AGENT_GID]


# ---------------------------------------------------------------------------------------------- the data envelope


@pytest.mark.parametrize(
    "path, unwrapped",
    [
        ("/projects", {"workspace": WS, "team": SURVEY, "name": "Buoy Moorings"}),
        (f"/tasks/{CHANNEL}/addTag", {"tag": SILT}),
        (f"/tasks/{SPRING_TABLES}/setParent", {"parent": CHANNEL}),
        (f"/tasks/{CHANNEL}/stories", {"text": "Soundings logged."}),
        (f"/sections/{stage_section('Charted')}/addTask", {"task": CHANNEL}),
        (f"/projects/{TIDES}/addCustomFieldSetting", {"custom_field": DEPTH}),
    ],
)
async def test_a_write_not_wrapped_in_data_is_refused_naming_the_stray_field(
    asana: Proxied, path: str, unwrapped: dict[str, object]
) -> None:
    """OBSERVED: a body with a field outside `data` is refused naming it, in the words reported from the real service
    (https://forum.asana.com/t/238695), without a write."""
    before = asana.store.head()

    first = next(iter(unwrapped))
    assert refused(await asana.http.post(path, json=unwrapped), 400) == (
        f"Unrecognized request field {first} . The only allowed keys at the top level are: data, options. "
        "Is it possible you did not wrap object properties in a data object?"
    )
    assert asana.store.head() == before


# ---------------------------------------------------------------------------------------------- tasks


async def test_completing_a_task_leaves_its_section_and_its_status_field_where_they_were(asana: Proxied) -> None:
    """OBSERVED: ticking `completed` moves the task to no other section and rewrites no custom field, so a task
    can read completed while its board column and its Stage field still say Sounding. Asana's task reference
    (https://developers.asana.com/reference/updatetask) changes only the fields sent; it says nothing about
    completion moving a task."""
    done = await asana.http.put(
        f"/tasks/{CHANNEL}",
        params={"opt_fields": "completed,memberships.section.name,custom_fields.enum_value.name"},
        json={"data": {"completed": True}},
    )
    task = got(done)

    assert task["completed"] is True
    assert [m["section"]["name"] for m in task["memberships"]] == ["Sounding"]
    assert [f["enum_value"]["name"] for f in task["custom_fields"] if f["enum_value"]] == ["Sounding"]


@pytest.mark.parametrize("sent", ["true", 1, None])
async def test_an_update_whose_completed_is_not_a_json_boolean_is_refused(asana: Proxied, sent: object) -> None:
    """DOCUMENTED: `completed` is a boolean (https://developers.asana.com/reference/updatetask); the string
    "true" is not one, and nothing is written."""
    before = asana.store.head()

    message = refused(await asana.http.put(f"/tasks/{CHANNEL}", json={"data": {"completed": sent}}), 400)

    assert message == "completed: Not a boolean"
    assert asana.store.head() == before


async def test_set_parent_to_a_task_that_does_not_exist_is_refused_400(asana: Proxied) -> None:
    """OBSERVED: a parent gid that names no task is a 400 on `setParent` rather than a 404, since the task in
    the path exists. The reference (https://developers.asana.com/reference/setparentfortask) requires
    `parent` but does not say how an unknown one is answered."""
    before = asana.store.head()

    message = refused(
        await asana.http.post(f"/tasks/{SPRING_TABLES}/setParent", json={"data": {"parent": UNKNOWN}}), 400
    )

    assert message == f"parent: Unknown object: {UNKNOWN}"
    assert asana.store.head() == before


async def test_a_task_created_in_two_projects_is_a_member_of_both(asana: Proxied) -> None:
    """DOCUMENTED: `memberships` holds one project-and-section pair per project, and a create may name many
    projects (https://developers.asana.com/reference/gettask)."""
    made = got(
        await asana.http.post("/tasks", json={"data": {"name": "Publish the chart", "projects": [HARBOUR, TIDES]}}),
        201,
    )
    read = got(await asana.http.get(f"/tasks/{made['gid']}", params={"opt_fields": "memberships.project.name"}))

    assert sorted(m["project"]["name"] for m in read["memberships"]) == ["Harbour Survey", "Tide Tables"]


async def test_a_subtask_never_added_to_a_project_has_no_membership(asana: Proxied) -> None:
    """OBSERVED: a subtask lives under its parent and belongs to no project until one is added, so its
    `memberships` is empty, not its parent's project."""
    read = got(await asana.http.get(f"/tasks/{SOUNDER}", params={"opt_fields": "memberships.project.name,parent.gid"}))

    assert read["parent"] == {"gid": CHANNEL}
    assert read["memberships"] == []


# ---------------------------------------------------------------------------------------------- listings


async def test_a_project_listing_without_archived_includes_archived_projects_and_the_filter_narrows_it(
    asana: Proxied,
) -> None:
    """DOCUMENTED: `archived` only returns projects whose `archived` field equals it and has no default
    (https://developers.asana.com/reference/getprojectsforworkspace), so leaving it out filters nothing."""
    listing = f"/workspaces/{WS}/projects"

    every = got(await asana.http.get(listing, params={"limit": "100"}))
    live = got(await asana.http.get(listing, params={"limit": "100", "archived": "false"}))
    archived = got(await asana.http.get(listing, params={"limit": "100", "archived": "true"}))

    assert sorted(p["gid"] for p in every) == sorted([HARBOUR, TIDES, DREDGING])
    assert sorted(p["gid"] for p in live) == sorted([HARBOUR, TIDES])
    assert [p["gid"] for p in archived] == [DREDGING]


async def test_custom_field_settings_are_compact_until_the_field_is_asked_for(asana: Proxied) -> None:
    """DOCUMENTED: a project's custom field settings are answered compact, gid and resource_type, and the field
    itself only through `opt_fields` (https://developers.asana.com/reference/getcustomfieldsettingsforproject)."""
    settings = f"/projects/{HARBOUR}/custom_field_settings"

    compact = got(await asana.http.get(settings))
    asked = got(
        await asana.http.get(settings, params={"opt_fields": "custom_field.name,custom_field.enum_options.name"})
    )

    assert compact == [{"gid": state.setting_gid(HARBOUR, STAGE), "resource_type": "custom_field_setting"}]
    assert asked[0]["custom_field"]["name"] == "Stage"
    assert [o["name"] for o in asked[0]["custom_field"]["enum_options"]] == STAGES


# ---------------------------------------------------------------------------------------------- projects


async def test_a_project_create_naming_no_workspace_is_refused_missing_input(asana: Proxied) -> None:
    """DOCUMENTED: every project is created in a workspace (https://developers.asana.com/reference/createproject);
    a create that names neither a workspace nor a team to find one through is refused."""
    before = asana.store.head()

    message = refused(await asana.http.post("/projects", json={"data": {"name": "Buoy Moorings"}}), 400)

    assert message == "workspace: Missing input"
    assert asana.store.head() == before


async def test_a_blank_project_name_is_refused_missing_input(asana: Proxied) -> None:
    """OBSERVED: a name of only spaces is no name; Asana answers it as a missing one."""
    blank = await asana.http.post("/projects", json={"data": {"workspace": WS, "team": SURVEY, "name": "  \t "}})

    assert refused(blank, 400) == "name: Missing input"


@pytest.mark.parametrize("field", ["workspace", "team"])
async def test_a_project_in_a_workspace_or_team_that_is_not_there_is_refused_unknown_object(
    asana: Proxied, field: str
) -> None:
    """OBSERVED: a well-formed gid that names no workspace or team is a 400 naming the field and the gid."""
    before = asana.store.head()
    sent = {"workspace": WS, "team": SURVEY, "name": "Buoy Moorings", field: UNKNOWN}

    assert (
        refused(await asana.http.post("/projects", json={"data": sent}), 400) == f"{field}: Unknown object: {UNKNOWN}"
    )
    assert asana.store.head() == before


async def test_a_custom_field_setting_naming_a_field_by_its_name_is_refused_not_a_recognized_id(asana: Proxied) -> None:
    """DOCUMENTED: `custom_field` is the gid of a field the workspace defines
    (https://developers.asana.com/reference/addcustomfieldsettingforproject); its name is not an id."""
    sent = {"data": {"custom_field": "Depth"}}

    assert refused(await asana.http.post(f"/projects/{TIDES}/addCustomFieldSetting", json=sent), 400) == (
        "custom_field: Not a Recognized ID"
    )


async def test_a_custom_field_setting_on_a_project_that_is_not_there_is_refused_404(asana: Proxied) -> None:
    """DOCUMENTED: an object that does not exist is a 404 (https://developers.asana.com/docs/errors)."""
    sent = {"data": {"custom_field": DEPTH}}

    assert refused(await asana.http.post(f"/projects/{UNKNOWN}/addCustomFieldSetting", json=sent), 404) == (
        f"project: Unknown object: {UNKNOWN}"
    )


async def test_a_created_project_reads_back_with_its_team_and_a_full_read_carries_it_unasked(asana: Proxied) -> None:
    """DOCUMENTED: a single project is answered as the full record, team included
    (https://developers.asana.com/reference/getproject). The old emulator answered a bare read compact,
    without the team; that contradicted the reference and is not carried over."""
    made = got(
        await asana.http.post(
            "/projects",
            params={"opt_fields": "name,team.name"},
            json={"data": {"workspace": WS, "team": SURVEY, "name": "Buoy Moorings"}},
        ),
        201,
    )
    asked = got(await asana.http.get(f"/projects/{made['gid']}", params={"opt_fields": "team.gid,team.name"}))
    bare = got(await asana.http.get(f"/projects/{made['gid']}"))

    assert made["team"] == {"gid": SURVEY, "name": "Survey"}
    assert asked["team"] == {"gid": SURVEY, "name": "Survey"}
    assert bare["team"] == {"gid": SURVEY, "resource_type": "team", "name": "Survey"}


async def test_a_full_workspace_read_says_whether_it_is_an_organization_unasked(asana: Proxied) -> None:
    """DOCUMENTED: a single workspace is answered as the full record, whose schema carries `is_organization`
    (https://developers.asana.com/reference/getworkspace); a listing answers it compact, without. The old
    emulator left it out of the bare single read; that contradicted the reference and is not carried over."""
    bare = got(await asana.http.get(f"/workspaces/{WS}"))
    listed = got(await asana.http.get("/workspaces"))

    assert bare["is_organization"] is True
    assert listed == [{"gid": WS, "resource_type": "workspace", "name": "Estuary Works"}]


# ---------------------------------------------------------------------------------------------- a plain workspace

PLAIN = SCENARIO.model_copy(
    update={
        "provider_seeds": [
            ProviderSeed(
                provider="asana",
                body=AsanaSeed.model_validate(
                    {"workspace": {"name": "Estuary Works", "organization": False}}
                ).model_dump_json(),
            )
        ]
    }
)


@pytest.fixture
async def plain(tmp_path: Path) -> AsyncIterator[Proxied]:
    async with proxied(PLAIN, tmp_path) as found:
        yield found


async def test_my_teams_in_a_plain_workspace_are_refused_by_name(plain: Proxied) -> None:
    """That `organization` is required is documented (https://developers.asana.com/reference/getteamsforuser);
    what Asana answers when it names a workspace that is not an organization is not, so it is refused by name."""
    teams = await plain.http.get("/users/me/teams", params={"organization": WS})

    assert teams.status_code == 501
    assert "a workspace that is not an organization" in refused(teams, 501)


async def test_a_project_in_a_plain_workspace_needs_no_team_and_has_none(plain: Proxied) -> None:
    """DOCUMENTED: only a workspace that is an organization needs a team on a new project
    (https://developers.asana.com/reference/createproject)."""
    made = await plain.http.post(
        "/projects", params={"opt_fields": "name,team"}, json={"data": {"workspace": WS, "name": "Buoy Moorings"}}
    )

    assert got(made, 201) == {"gid": got(made, 201)["gid"], "name": "Buoy Moorings", "team": None}


# ---------------------------------------------------------------------------------------------- too large

CROWDED = SCENARIO.model_copy(
    update={
        "tickets": [
            SeededTicket(provider="asana", project="Harbour Survey", title=f"Sound buoy {n}") for n in range(1001)
        ],
        "provider_seeds": [],
    }
)


@pytest.fixture
async def crowded(tmp_path: Path) -> AsyncIterator[Proxied]:
    async with proxied(CROWDED, tmp_path) as found:
        yield found


async def test_an_unpaginated_listing_too_large_to_answer_is_refused_and_a_paged_one_is_not(crowded: Proxied) -> None:
    """OBSERVED: past a size Asana does not publish, a listing sent without `limit` is refused 400 rather than
    cut short, and paging the same listing works. The errors page names a 400 for results too large to return
    (https://developers.asana.com/docs/errors) and the pagination guide warns unpaginated reads fail at size
    (https://developers.asana.com/docs/pagination); neither gives the threshold or the words."""
    harbour = state.project_gid("Harbour Survey")
    listing = f"/projects/{harbour}/tasks"

    message = refused(await crowded.http.get(listing), 400)
    paged = (await crowded.http.get(listing, params={"limit": "100"})).json()

    assert message.startswith("The result is too large. You should use pagination")
    assert len(paged["data"]) == 100 and paged["next_page"]["offset"]
