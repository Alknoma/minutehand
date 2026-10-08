"""The fake's surface held to Jira Cloud's own machine-readable reference.

`tests/data/vendor_surface/jira.json` is the subset of Atlassian's OpenAPI descriptions (platform v3 and Agile,
fetched 2026-10-08) for the resources this fake claims. Every operation in it is either served by a handler or
refused by name with a 501; every query parameter and body property the reference documents for a served
operation is either acted on or refused by name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from minutehand.adapters.providers.jira import app, wire
from minutehand.adapters.providers.jira.surface import UNSERVED
from tests.providers.jira.jira_site import API, Site, ok, refused

SUBSET: dict[str, Any] = json.loads(
    (Path(__file__).parents[2] / "data" / "vendor_surface" / "jira.json").read_text(encoding="utf-8")
)
OPERATIONS = {(method, path) for path, methods in SUBSET["paths"].items() for method in methods}
REFUSED = sorted((method, path) for path, methods in UNSERVED.items() for method in methods)
BODIES: dict[tuple[str, str], type[wire.Request]] = {
    ("POST", "/rest/api/3/project"): wire.ProjectIn,
    ("POST", "/rest/api/3/project/{projectIdOrKey}/role/{id}"): wire.RoleActorsIn,
    ("POST", "/rest/api/3/search/jql"): wire.SearchIn,
    ("POST", "/rest/api/3/search/approximate-count"): wire.CountIn,
    ("POST", "/rest/api/3/issue"): wire.IssueIn,
    ("PUT", "/rest/api/3/issue/{issueIdOrKey}"): wire.IssueIn,
    ("PUT", "/rest/api/3/issue/{issueIdOrKey}/assignee"): wire.AssigneeIn,
    ("POST", "/rest/api/3/issue/{issueIdOrKey}/transitions"): wire.TransitionIn,
    ("POST", "/rest/api/3/issue/{issueIdOrKey}/comment"): wire.CommentIn,
    ("POST", "/rest/api/3/issueLink"): wire.LinkIn,
    ("POST", "/rest/agile/1.0/sprint/{sprintId}/issue"): wire.SprintIssuesIn,
}
"""Each served operation that takes a body, and the model it is read into. `POST /search` is answered 410 before
its body is read."""


def _served(site: Site) -> list[app.Served]:
    return app.build(site.store, site.clock).served()


def test_the_subset_names_where_and_when_it_was_cut() -> None:
    assert [s["url"] for s in SUBSET["sources"]] == [
        "https://developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json",
        "https://developer.atlassian.com/cloud/jira/software/swagger.v3.json",
    ]
    assert SUBSET["fetched"] == "2026-10-08"


def test_every_operation_of_the_subset_is_served_or_refused_by_name_and_none_is_both(site: Site) -> None:
    served = {(s.method, s.path) for s in _served(site)}
    refused_here = set(REFUSED)
    assert served & refused_here == set()
    assert served | refused_here == OPERATIONS, {
        "neither": sorted(OPERATIONS - served - refused_here),
        "not in the reference": sorted((served | refused_here) - OPERATIONS),
    }
    assert (len(served), len(refused_here), len(OPERATIONS)) == (45, 167, 212)


def test_every_documented_query_parameter_of_a_served_operation_is_read_or_refused(site: Site) -> None:
    for served in _served(site):
        documented = set(SUBSET["paths"][served.path][served.method].get("query", []))
        assert served.reads & served.refuses == set(), served.path
        assert served.reads | served.refuses == documented, (served.method, served.path)


def test_every_documented_body_property_of_a_served_operation_is_read_or_refused(site: Site) -> None:
    for served in _served(site):
        documented = set(SUBSET["paths"][served.path][served.method].get("body", []))
        model = BODIES.get((served.method, served.path))
        if model is None:
            assert not documented or served.path == "/rest/api/3/search", (served.method, served.path)
            continue
        read = {f.alias or n for n, f in model.model_fields.items()}
        assert read & model.UNSERVED == set(), served.path
        assert read | model.UNSERVED == documented, (served.method, served.path)


def _filled(template: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda m: "LAUNCH-1" if "issue" in m.group(0).lower() else "10000", template)


def _url(template: str) -> str:
    return f"https://lanternworks.atlassian.net{_filled(template)}"


@pytest.mark.parametrize(("method", "template"), REFUSED, ids=[f"{m} {p}" for m, p in REFUSED])
async def test_an_operation_the_fake_does_not_serve_is_refused_501_naming_it(
    site: Site, method: str, template: str
) -> None:
    answer = await site.http.request(method, _url(template), json={} if method in ("POST", "PUT") else None)
    body = refused(answer, 501)
    assert f"{method} {template}" in body["errorMessages"][0]


async def test_a_documented_parameter_the_fake_does_not_act_on_is_refused_501_naming_it(site: Site) -> None:
    body = refused(await site.http.get(f"{API}/issue/LAUNCH-1", params={"properties": "*all"}), 501)
    assert "'properties' parameter of GET /rest/api/3/issue/{issueIdOrKey}" in body["errorMessages"][0]


async def test_a_documented_body_property_the_fake_does_not_keep_is_refused_501_naming_it(site: Site) -> None:
    comment = {
        "body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]},
        "visibility": {"type": "role", "value": "Administrators"},
    }  # fmt: skip
    body = refused(await site.http.post(f"{API}/issue/LAUNCH-1/comment", json=comment), 501)
    assert "'visibility' property" in body["errorMessages"][0]
    assert ok(await site.http.get(f"{API}/issue/LAUNCH-1/comment"))["total"] == 1, "nothing was written"


async def test_a_property_a_closed_body_has_not_got_is_refused_400(site: Site) -> None:
    """As recorded (`data/observed/search_body_unknown_property.http`): Jira's invalid-payload body, no `errors`."""
    answer = await site.http.post(f"{API}/search/jql", json={"jql": "project = LAUNCH", "colour": "red"})
    assert (answer.status_code, answer.json()) == (
        400,
        {"errorMessages": ["Invalid request payload. Refer to the REST API documentation and try again."]},
    )


@pytest.mark.parametrize(
    "path", ["/rest/api/3/dashboard", "/rest/api/2/issue/LAUNCH-1", "/rest/servicedeskapi/request"]
)
async def test_a_path_outside_the_resources_the_fake_claims_is_refused_501_naming_it(site: Site, path: str) -> None:
    body = refused(await site.http.get(f"https://lanternworks.atlassian.net{path}"), 501)
    assert f"GET {path}" in body["errorMessages"][0]


async def test_a_path_in_a_claimed_resource_that_the_reference_does_not_name_is_404(site: Site) -> None:
    """As recorded (`data/observed/unknown_path.http`): Jira's problem body."""
    answer = await site.http.get(f"{API}/issue/LAUNCH-1/no-such-thing")
    assert answer.status_code == 404 and answer.headers["content-type"].startswith("application/problem+json")
    assert answer.json()["detail"] == "No endpoint GET /rest/api/3/issue/LAUNCH-1/no-such-thing."


async def test_a_literal_path_wins_over_a_template_that_would_read_it_as_a_key(site: Site) -> None:
    body = refused(await site.http.get(f"{API}/issue/picker"), 501)
    assert "GET /rest/api/3/issue/picker" in body["errorMessages"][0]
