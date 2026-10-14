"""What a client reads before it can read an issue's size: the project's time-tracking settings, which name the period
fields that hold an estimate and the time spent, and the agile boards, which name the field a board estimates by and
the projects it holds. Both are the seeded world's, and a ticket's size then reads back through the `youtrack-sdk`
client as its vendor's users read it.

The answers' shapes are held to what JetBrains' public instance answered on 2026-10-13
(`data/observed/time_tracking_settings.http`, `unknown_project_time_tracking.http`, `agiles.http`)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from youtrack_sdk.client import Client
from youtrack_sdk.entities import PeriodIssueCustomField, SimpleIssueCustomField

from minutehand.adapters.answering import Guarded
from minutehand.adapters.providers.youtrack.provider import build
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import BUILTIN_PACKAGE, Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import ProviderSeed, Scenario
from tests.providers.youtrack.youtrack_instance import HOST, SCENARIO, START, TOKEN, entities, entity, refusal

OBSERVED = Path(__file__).parent / "data" / "observed"
SIZED = {"ticket": "sized", "fields": [
    {"field": "Estimation", "value": 300}, {"field": "Spent time", "value": 90}, {"field": "Story Points", "value": 2},
]}  # fmt: skip
SETTINGS_FIELDS = "enabled,estimate(field(name)),timeSpent(field(name))"
AGILE_FIELDS = "estimationField(name),projects(id)"


def _scenario(seed: dict[str, object]) -> Scenario:
    tickets = [
        t.model_copy(update={"key": "sized"}) if t.title == "Write the release notes" else t for t in SCENARIO.tickets
    ]
    return SCENARIO.model_copy(
        update={"tickets": tickets, "provider_seeds": [ProviderSeed(provider="youtrack", body=json.dumps(seed))]}
    )


class World:
    def __init__(self, tmp_path: Path, seed: dict[str, object]) -> None:
        self.clock = RunClock(START)
        self.store = SqliteStore(tmp_path / "world.db", "root", self.clock)
        self.provider = build()
        self.provider.seed(_scenario(seed), self.store)

    def client(self) -> httpx.AsyncClient:
        app = Guarded(self.provider.app(self.store, self.clock), self.provider, provider="youtrack", clock=self.clock)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=HOST, headers={"Authorization": f"Bearer {TOKEN}"}
        )


async def _http(tmp_path: Path, seed: dict[str, object]) -> AsyncIterator[httpx.AsyncClient]:
    async with World(tmp_path, seed).client() as http:
        yield http


@pytest.fixture
async def yt(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async for http in _http(tmp_path, {"issues": [SIZED]}):
        yield http


def _recorded(name: str) -> tuple[int, Any]:
    head, _, body = (OBSERVED / f"{name}.http").read_bytes().decode("utf-8").partition("\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


def _shape(value: Any) -> Any:
    """An answer with its values dropped: the keys of every object and the `$type` each names."""
    if isinstance(value, list):
        return [_shape(v) for v in value[:1]]
    if isinstance(value, dict):
        return {k: (v if k == "$type" else _shape(v)) for k, v in value.items()}
    return None


async def _project_ids(yt: httpx.AsyncClient) -> dict[str, str]:
    return {
        str(p["shortName"]): str(p["id"])
        for p in entities(await yt.get("/api/admin/projects", params={"fields": "id,shortName"}))
    }


# --------------------------------------------------------------------------- time-tracking settings


@pytest.mark.parametrize("project", ["0-0", "LAUNCH"])
async def test_time_tracking_names_the_projects_period_fields_by_id_or_short_name(
    yt: httpx.AsyncClient, project: str
) -> None:
    answer = entity(
        await yt.get(f"/api/admin/projects/{project}/timeTrackingSettings", params={"fields": SETTINGS_FIELDS})
    )

    assert answer == {
        "enabled": True,
        "estimate": {"field": {"name": "Estimation", "$type": "CustomField"}, "$type": "PeriodProjectCustomField"},
        "timeSpent": {"field": {"name": "Spent time", "$type": "CustomField"}, "$type": "PeriodProjectCustomField"},
        "$type": "ProjectTimeTrackingSettings",
    }


async def test_time_tracking_answers_in_the_shape_the_public_instance_does(yt: httpx.AsyncClient) -> None:
    _, recorded = _recorded("time_tracking_settings")
    asked = "id,enabled,project(id,shortName),estimate(id,field(name)),timeSpent(id,field(name))"
    answer = entity(await yt.get("/api/admin/projects/LAUNCH/timeTrackingSettings", params={"fields": asked}))
    project_fields = entities(
        await yt.get("/api/admin/projects/LAUNCH/customFields", params={"fields": "id,field(name)"})
    )
    field_ids = {f["field"]["name"]: f["id"] for f in project_fields}  # type: ignore[index]

    assert _shape(answer) == _shape(recorded)
    assert answer["estimate"]["id"] == field_ids["Estimation"]  # type: ignore[index]
    assert answer["timeSpent"]["id"] == field_ids["Spent time"]  # type: ignore[index]
    assert answer["project"] == {"shortName": "LAUNCH", "id": "0-0", "$type": "Project"}


async def test_time_tracking_of_an_unknown_project_answers_as_the_public_instance_does(yt: httpx.AsyncClient) -> None:
    status, recorded = _recorded("unknown_project_time_tracking")
    answer = await yt.get("/api/admin/projects/0-99999/timeTrackingSettings", params={"fields": "id"})

    assert (answer.status_code, answer.json()) == (status, recorded)


async def test_time_tracking_is_as_the_seed_states_it(tmp_path: Path) -> None:
    seed: dict[str, object] = {
        "projects": [{"name": "Launch", "short_name": "LAUNCH", "time_tracking": {"enabled": False, "estimate": None, "spent_time": None}}],
    }  # fmt: skip
    async for yt in _http(tmp_path, seed):
        answer = entity(
            await yt.get("/api/admin/projects/LAUNCH/timeTrackingSettings", params={"fields": SETTINGS_FIELDS})
        )

        assert answer == {"enabled": False, "estimate": None, "timeSpent": None, "$type": "ProjectTimeTrackingSettings"}


def test_a_seed_whose_time_tracking_names_a_field_that_is_no_period_is_refused(tmp_path: Path) -> None:
    seed: dict[str, object] = {"projects": [{"name": "Launch", "time_tracking": {"estimate": "Story Points"}}]}

    with pytest.raises(ValueError, match="names 'Story Points', no period field it carries"):
        World(tmp_path, seed)


async def test_a_project_without_the_period_fields_tracks_time_with_none_named(tmp_path: Path) -> None:
    seed: dict[str, object] = {"projects": [{"name": "Launch", "short_name": "LAUNCH", "fields": [{"name": "State"}, {"name": "Assignee"}]}]}  # fmt: skip
    async for yt in _http(tmp_path, seed):
        answer = entity(
            await yt.get("/api/admin/projects/LAUNCH/timeTrackingSettings", params={"fields": SETTINGS_FIELDS})
        )

        assert answer == {"enabled": True, "estimate": None, "timeSpent": None, "$type": "ProjectTimeTrackingSettings"}


async def test_time_tracking_of_a_project_made_through_the_api_is_refused_501_naming_it(yt: httpx.AsyncClient) -> None:
    made = {"name": "Fresh", "shortName": "FRESH", "leader": {"id": "1-0"}}
    entity(await yt.post("/api/admin/projects", params={"fields": "id"}, json=made))

    refused = refusal(await yt.get("/api/admin/projects/FRESH/timeTrackingSettings", params={"fields": "enabled"}), 501)

    assert "the time-tracking settings of FRESH, made through POST /admin/projects" in str(refused["error_description"])


async def test_a_time_tracking_attribute_the_fake_keeps_none_of_is_refused_501_naming_it(yt: httpx.AsyncClient) -> None:
    refused = refusal(
        await yt.get("/api/admin/projects/LAUNCH/timeTrackingSettings", params={"fields": "enabled,workItemTypes(id)"}),
        501,
    )

    assert "ProjectTimeTrackingSettings.workItemTypes" in str(refused["error_description"])


# --------------------------------------------------------------------------- agile boards


async def test_agiles_list_a_board_per_project_estimated_by_story_points(yt: httpx.AsyncClient) -> None:
    boards = entities(await yt.get("/api/agiles", params={"fields": AGILE_FIELDS, "$top": "500"}))
    projects = await _project_ids(yt)

    assert boards == [
        {
            "estimationField": {"name": "Story Points", "$type": "CustomField"},
            "projects": [{"id": projects[key], "$type": "Project"}],
            "$type": "Agile",
        }
        for key in ("LAUNCH", "FIELDOPS")
    ]


async def test_agiles_answer_in_the_shape_the_public_instance_does(yt: httpx.AsyncClient) -> None:
    _, recorded = _recorded("agiles")
    asked = "id,name,estimationField(name),projects(id,shortName)"
    answer = entities(await yt.get("/api/agiles", params={"fields": asked, "$top": "2"}))

    assert {k for k in answer[0] if k != "estimationField"} == {k for k in recorded[0] if k != "estimationField"}
    assert _shape(answer[0]["projects"]) == _shape(recorded[0]["projects"])
    assert [b["name"] for b in answer] == ["Launch Board", "Field Ops Board"]


async def test_agiles_are_as_the_seed_states_them(tmp_path: Path) -> None:
    seed: dict[str, object] = {"agiles": [
        {"name": "Delivery", "projects": ["Launch", "FIELDOPS"]},
        {"name": "Ops wall", "projects": ["Field Ops"], "estimation_field": None},
    ]}  # fmt: skip
    async for yt in _http(tmp_path, seed):
        boards = entities(await yt.get("/api/agiles", params={"fields": "name," + AGILE_FIELDS}))
        projects = await _project_ids(yt)

        assert sorted(boards, key=lambda b: str(b["name"])) == [
            {
                "name": "Delivery",
                "estimationField": {"name": "Story Points", "$type": "CustomField"},
                "projects": [{"id": projects["LAUNCH"], "$type": "Project"}, {"id": projects["FIELDOPS"], "$type": "Project"}],
                "$type": "Agile",
            },
            {"name": "Ops wall", "estimationField": None, "projects": [{"id": projects["FIELDOPS"], "$type": "Project"}], "$type": "Agile"},
        ]  # fmt: skip


def test_a_seed_whose_board_holds_a_project_the_instance_has_not_got_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="holds project 'Nowhere'"):
        World(tmp_path, {"agiles": [{"name": "Delivery", "projects": ["Nowhere"]}]})


@pytest.mark.parametrize(
    ("template", "made"),
    [
        (None, []),
        ("scrum", [("Fresh Project Management", "Story Points"), ("Fresh Development", "Ideal days")]),
        ("kanban", [("Fresh Kanban Board", None)]),
    ],
)
async def test_a_project_made_from_a_template_gets_the_boards_its_page_lists(
    yt: httpx.AsyncClient, template: str | None, made: list[tuple[str, str | None]]
) -> None:
    params = {"fields": "id"} | ({} if template is None else {"template": template})
    project = entity(
        await yt.post(
            "/api/admin/projects", params=params, json={"name": "Fresh", "shortName": "FRESH", "leader": {"id": "1-0"}}
        )
    )

    boards = entities(await yt.get("/api/agiles", params={"fields": "name,estimationField(name),projects(id)"}))
    fresh = [b for b in boards if b["projects"] == [{"id": project["id"], "$type": "Project"}]]

    assert [(b["name"], None if b["estimationField"] is None else b["estimationField"]["name"]) for b in fresh] == made  # type: ignore[index]


async def test_an_agile_attribute_the_fake_keeps_none_of_is_refused_501_naming_it(yt: httpx.AsyncClient) -> None:
    refused = refusal(await yt.get("/api/agiles", params={"fields": "id,sprints(id)"}), 501)

    assert "Agile.sprints" in str(refused["error_description"])


async def test_agiles_page_by_skip_and_top(yt: httpx.AsyncClient) -> None:
    first = entities(await yt.get("/api/agiles", params={"fields": "name", "$top": "1"}))
    second = entities(await yt.get("/api/agiles", params={"fields": "name", "$skip": "1", "$top": "1"}))

    assert [b["name"] for b in first + second] == ["Launch Board", "Field Ops Board"]


# --------------------------------------------------------------------------- an issue's size, through a real client


async def test_an_issues_size_reads_back_through_the_youtrack_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path, {"issues": [SIZED]})
    registry = Registry()
    registry.discover(BUILTIN_PACKAGE)
    async with Proxy(Routing(registry), world.store, world.clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(world.store, world.clock, {"youtrack": world.provider.app(world.store, world.clock)})
        monkeypatch.setenv("HTTPS_PROXY", proxy.url)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(proxy.ca_cert))
        sdk = Client(base_url=HOST, token=TOKEN)

        custom_fields = await asyncio.to_thread(lambda: sdk.get_issue_custom_fields(issue_id="LAUNCH-1"))

    by_name = {f.name: f for f in custom_fields}
    points = by_name["Story Points"]
    assert _hours(by_name["Estimation"]) == (5.0, "5h")
    assert _hours(by_name["Spent time"]) == (1.5, "1h 30m")
    assert isinstance(points, SimpleIssueCustomField) and points.value == 2


def _hours(field: object) -> tuple[float, str | None]:
    """A period field as the SDK reads it, in hours and as YouTrack presents it."""
    assert isinstance(field, PeriodIssueCustomField) and field.value is not None and field.value.minutes is not None
    return field.value.minutes / 60, field.value.presentation


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("issue_custom_fields_top_negative", "/api/issues/LAUNCH-1/customFields"),
        ("agiles_top_negative", "/api/agiles"),
    ],
)
async def test_a_negative_top_answers_every_entity_as_the_public_instance_does(
    yt: httpx.AsyncClient, name: str, path: str
) -> None:
    status, recorded = _recorded(name)
    every = entities(await yt.get(path, params={"fields": "id"}))

    answer = await yt.get(path, params={"fields": "id", "$top": "-1"})

    assert answer.status_code == status and isinstance(recorded, list) and len(recorded) > 0
    assert answer.json() == every
