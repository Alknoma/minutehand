"""The fake against YouTrack's own description of its API: every operation of the claimed resources is served or
refused by name, and nothing is served that YouTrack does not describe.

The subset of YouTrack's OpenAPI description (`/api/openapi.json` on JetBrains' public instance, 2026.3, fetched
2026-10-08) is `tests/data/vendor_surface/youtrack.json`.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.youtrack.app import TABLE
from minutehand.adapters.providers.youtrack.surface import UNSERVED
from tests.providers.youtrack.youtrack_instance import LAUNCH, Instance, client_for, entities, entity, proxied, refusal

SURFACE = Path(__file__).parents[2] / "data" / "vendor_surface" / "youtrack.json"


def _shape(path: str) -> str:
    """A path template with its parameters unnamed: YouTrack's `{id}` and the fake's `{issue}` are one slot."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def _described() -> dict[tuple[str, str], str]:
    surface = json.loads(SURFACE.read_text())
    return {(method, _shape(path)): path for path, ops in surface["paths"].items() for method in ops}


SERVED = {(method, _shape(path)) for path, method, _ in TABLE}
REFUSED = {(method, _shape(path)) for method, path in UNSERVED}


def test_the_surface_file_says_where_it_came_from() -> None:
    surface = json.loads(SURFACE.read_text())

    assert surface["source"] == "https://youtrack.jetbrains.com/api/openapi.json"
    assert surface["fetched"] == "2026-10-08" and surface["version"] == "2026.3"


def test_every_described_operation_is_served_or_refused_by_name_and_none_is_both() -> None:
    described = set(_described())

    assert sorted(described - SERVED - REFUSED) == []
    assert sorted(SERVED & REFUSED) == []


def test_nothing_is_served_or_refused_that_youtrack_does_not_describe() -> None:
    described = set(_described())

    assert sorted(SERVED - described) == []
    assert sorted(REFUSED - described) == []


def test_the_counts_of_served_and_refused_operations() -> None:
    """184 operations under the claimed resources: 55 served, 129 refused by name. A change to either shows here."""
    assert (len(_described()), len(SERVED), len(REFUSED)) == (184, 55, 129)


@pytest.fixture
async def http(instance: Instance, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with proxied(instance, tmp_path / "ca") as client:
        yield client


@pytest.mark.parametrize(("method", "path"), UNSERVED, ids=[f"{m} {p}" for m, p in UNSERVED])
async def test_an_unserved_operation_is_refused_501_naming_it(http: httpx.AsyncClient, method: str, path: str) -> None:
    concrete = re.sub(r"\{[^}]*\}", "2-1", path)

    refused = refusal(await http.request(method, f"/api{concrete}", json={}), 501)

    assert f"{method} {path} is an operation of YouTrack's API" in str(refused["error_description"])


async def test_a_path_outside_the_claimed_resources_is_refused_501_naming_it(http: httpx.AsyncClient) -> None:
    refused = refusal(await http.get("/api/agiles", params={"fields": "id"}), 501)
    hub = refusal(await http.get("/hub/api/rest/roles"), 501)

    assert "GET /api/agiles is outside the resources this fake serves" in str(refused["error_description"])
    assert "GET /hub/api/rest/roles" in str(hub["error_description"])


@pytest.mark.parametrize(
    ("entity_path", "attribute"),
    [("/api/issues/LAUNCH-1", "wikifiedDescription"), ("/api/issues/LAUNCH-1/comments", "textPreview")],
)
async def test_an_attribute_youtrack_renders_from_markup_is_refused_by_name(
    http: httpx.AsyncClient, entity_path: str, attribute: str
) -> None:
    """The Issue and IssueComment pages describe both as the text rendered to HTML; the fake renders nothing, so it
    names the attribute instead of answering the raw text as if rendered."""
    if entity_path.endswith("/comments"):
        await http.post(entity_path, json={"text": "**bold**"})

    refused = refusal(await http.get(entity_path, params={"fields": f"id,{attribute}"}), 501)

    assert attribute in str(refused["error_description"])


async def test_a_command_description_is_refused_by_name(http: httpx.AsyncClient) -> None:
    answered = entity(
        await http.post(
            "/api/commands",
            params={"fields": "commands(error)"},
            json={"query": "State In Progress", "issues": [{"idReadable": "LAUNCH-1"}]},
        )
    )
    refused = refusal(
        await http.post(
            "/api/commands",
            params={"fields": "commands(description)"},
            json={"query": "State In Progress", "issues": [{"idReadable": "LAUNCH-1"}]},
        ),
        501,
    )

    assert answered["commands"] == [{"error": False, "$type": "ParsedCommand"}]
    assert "ParsedCommand.description" in str(refused["error_description"])


async def test_an_issue_and_its_comment_round_trip_as_sent(instance: Instance) -> None:
    """Data stays as sent: summary, description and comment text are stored and answered byte for byte."""
    summary = "  Proof the programme — ünïcode, <b>tags</b> & spaces  "
    description = "Line one\n\n* a list\n`code` and trailing space "
    text = "> quoted\n\nA comment with *markup* left alone\t"
    async with client_for(instance.provider, instance.store, instance.clock, token="any-token-at-all") as http:
        made = entity(
            await http.post(
                "/api/issues",
                params={"fields": "id"},
                json={"project": {"id": LAUNCH}, "summary": summary, "description": description},
            )
        )
        comment = entity(
            await http.post(f"/api/issues/{made['id']}/comments", params={"fields": "id,text"}, json={"text": text})
        )
        read = entity(await http.get(f"/api/issues/{made['id']}", params={"fields": "summary,description"}))
        listed = entities(await http.get(f"/api/issues/{made['id']}/comments", params={"fields": "text"}))

    assert read == {"summary": summary, "description": description, "$type": "Issue"}
    assert comment["text"] == text and listed == [{"text": text, "$type": "IssueComment"}]


async def test_get_issues_shows_only_the_custom_fields_named(instance: Instance) -> None:
    """Documented: `customFields=<name>`, repeated, narrows the custom fields answered
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-issues.html)."""
    async with client_for(instance.provider, instance.store, instance.clock) as http:
        found = entities(
            await http.get(
                "/api/issues",
                params=[
                    ("query", "project: LAUNCH"),
                    ("fields", "customFields(name)"),
                    ("customFields", "State"),
                    ("customFields", "Assignee"),
                    ("$top", "1"),
                ],
            )
        )
        every = entities(await http.get("/api/issues", params={"fields": "customFields(name)", "$top": "1"}))

    assert [f["name"] for f in found[0]["customFields"]] == ["State", "Assignee"]  # type: ignore[index,union-attr]
    assert len(every[0]["customFields"]) > 2  # type: ignore[arg-type]
