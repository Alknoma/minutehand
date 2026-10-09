"""GitHub's own machine-readable description of its REST API, for the resources this provider has to do with, held
against the provider: every operation in it is either served or refused by name, and a served one answers every
field the description requires.

`openapi/api.github.com.subset.json` is the subset of GitHub's published OpenAPI description
(https://github.com/github/rest-api-description, `descriptions/api.github.com/api.github.com.json`, MIT licence)
whose operations fall in the reference sections named in its `x-minutehand-subset`, with the components they refer
to and without the description's example payloads. Its `info.x-minutehand-source` names the commit and the date it
was taken from."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed, number
from tests.providers.github.github_world import TOMAS, Hub, tracker_seed
from tests.providers.github.schema import missing, resolve

SUBSET = Path(__file__).parent / "openapi" / "api.github.com.subset.json"
DESCRIPTION = json.loads(SUBSET.read_text())


@dataclass(frozen=True)
class Call:
    """A call of a served operation against the seeded world: its address, what it sends and how it must answer."""

    url: str
    json: object = None
    status: int = 200
    before: tuple[Call, ...] = ()
    """Calls made first, to put the world where this one can be answered."""


LEDGER = "/repos/lanternworks/ledger"
COMMENT = number("comment/lanternworks/ledger/1/0")
"""The id the seed derives for the first comment on issue 1."""
REVIEW = number("review/lanternworks/ledger/4/0")
"""The id the seed derives for the first review of pull request 4."""
MERGE = Call(f"{LEDGER}/pulls/4/merge", {}, 200)
SERVED: dict[tuple[str, str], Call] = {
    ("GET", "/user"): Call("/user"),
    ("GET", "/user/repos"): Call("/user/repos"),
    ("GET", "/repos/{owner}/{repo}"): Call(LEDGER),
    ("GET", "/repos/{owner}/{repo}/languages"): Call(f"{LEDGER}/languages"),
    ("GET", "/repos/{owner}/{repo}/branches"): Call(f"{LEDGER}/branches"),
    ("GET", "/repos/{owner}/{repo}/contents/{path}"): Call(f"{LEDGER}/contents/README.md"),
    ("GET", "/repos/{owner}/{repo}/git/blobs/{file_sha}"): Call(f"{LEDGER}/git/blobs/{{readme}}"),
    ("GET", "/repos/{owner}/{repo}/git/trees/{tree_sha}"): Call(f"{LEDGER}/git/trees/main"),
    ("GET", "/repos/{owner}/{repo}/commits"): Call(f"{LEDGER}/commits"),
    ("GET", "/search/code"): Call("/search/code?q=retry"),
    ("GET", "/rate_limit"): Call("/rate_limit"),
    ("POST", "/app/installations/{installation_id}/access_tokens"): Call("/app/installations/7/access_tokens", {}, 201),
    ("GET", "/repos/{owner}/{repo}/issues"): Call(f"{LEDGER}/issues?state=all"),
    ("POST", "/repos/{owner}/{repo}/issues"): Call(f"{LEDGER}/issues", {"title": "A title", "body": "A body"}, 201),
    ("GET", "/repos/{owner}/{repo}/issues/{issue_number}"): Call(f"{LEDGER}/issues/1"),
    ("PATCH", "/repos/{owner}/{repo}/issues/{issue_number}"): Call(f"{LEDGER}/issues/3", {"state": "closed"}),
    ("PUT", "/repos/{owner}/{repo}/issues/{issue_number}/lock"): Call(f"{LEDGER}/issues/1/lock", None, 204),
    ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/lock"): Call(f"{LEDGER}/issues/1/lock", None, 204),
    ("GET", "/repos/{owner}/{repo}/issues/{issue_number}/comments"): Call(f"{LEDGER}/issues/1/comments"),
    ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/comments"): Call(
        f"{LEDGER}/issues/1/comments", {"body": "A comment"}, 201
    ),
    ("GET", "/repos/{owner}/{repo}/issues/comments"): Call(f"{LEDGER}/issues/comments"),
    ("GET", "/repos/{owner}/{repo}/issues/comments/{comment_id}"): Call(f"{LEDGER}/issues/comments/{COMMENT}"),
    ("PATCH", "/repos/{owner}/{repo}/issues/comments/{comment_id}"): Call(
        f"{LEDGER}/issues/comments/{COMMENT}", {"body": "Edited"}
    ),
    ("DELETE", "/repos/{owner}/{repo}/issues/comments/{comment_id}"): Call(
        f"{LEDGER}/issues/comments/{COMMENT}", None, 204
    ),
    ("GET", "/repos/{owner}/{repo}/labels"): Call(f"{LEDGER}/labels"),
    ("POST", "/repos/{owner}/{repo}/labels"): Call(f"{LEDGER}/labels", {"name": "new", "color": "ffffff"}, 201),
    ("GET", "/repos/{owner}/{repo}/labels/{name}"): Call(f"{LEDGER}/labels/bug"),
    ("GET", "/repos/{owner}/{repo}/issues/{issue_number}/labels"): Call(f"{LEDGER}/issues/1/labels"),
    ("POST", "/repos/{owner}/{repo}/issues/{issue_number}/labels"): Call(
        f"{LEDGER}/issues/3/labels", {"labels": ["bug"]}
    ),
    ("PUT", "/repos/{owner}/{repo}/issues/{issue_number}/labels"): Call(
        f"{LEDGER}/issues/3/labels", {"labels": ["bug"]}
    ),
    ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/labels"): Call(f"{LEDGER}/issues/1/labels", None, 204),
    ("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/labels/{name}"): Call(f"{LEDGER}/issues/1/labels/bug"),
    ("GET", "/repos/{owner}/{repo}/pulls"): Call(f"{LEDGER}/pulls?state=all"),
    ("POST", "/repos/{owner}/{repo}/pulls"): Call(
        f"{LEDGER}/pulls", {"title": "Describe the notes", "head": "notes", "base": "main"}, 201
    ),
    ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}"): Call(f"{LEDGER}/pulls/4"),
    ("PATCH", "/repos/{owner}/{repo}/pulls/{pull_number}"): Call(f"{LEDGER}/pulls/4", {"title": "Raise it"}),
    ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/files"): Call(f"{LEDGER}/pulls/4/files"),
    ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/commits"): Call(f"{LEDGER}/pulls/4/commits"),
    ("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/merge"): MERGE,
    ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/merge"): Call(
        f"{LEDGER}/pulls/4/merge", None, 204, before=(MERGE,)
    ),
    ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews"): Call(f"{LEDGER}/pulls/4/reviews"),
    ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews"): Call(
        f"{LEDGER}/pulls/4/reviews", {"event": "COMMENT", "body": "A review"}
    ),
    ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}"): Call(
        f"{LEDGER}/pulls/4/reviews/{REVIEW}"
    ),
    ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/comments"): Call(
        f"{LEDGER}/pulls/4/reviews/{REVIEW}/comments"
    ),
    ("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/comments"): Call(f"{LEDGER}/pulls/4/comments"),
    ("PUT", "/repos/{owner}/{repo}/contents/{path}"): Call(
        f"{LEDGER}/contents/docs/new.md", {"message": "Add it", "content": "eA=="}, 201
    ),
    ("DELETE", "/repos/{owner}/{repo}/contents/{path}"): Call(
        f"{LEDGER}/contents/README.md", {"message": "Drop it", "sha": "{readme}"}
    ),
    ("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/comments"): Call(
        f"{LEDGER}/pulls/4/comments",
        {"body": "Why?", "commit_id": "{head}", "path": "services/billing/config.py", "line": 1, "side": "RIGHT"},
        201,
    ),
}
"""Each operation the provider serves, and a call of it against the seeded world."""

PENDING: dict[str, str] = {
    "$.items[].score": "code search's `score`: required by the description, its computation undocumented, and recording it "
    'needs a credential; left out rather than made up (CLAIMS.md, "Pending a recording")',
}
"""Required fields deliberately not answered, each with its reason, by their place in the answer."""

PLACEHOLDER = re.compile(r"\{([^}]+)\}")
WORLD_VALUES = {"owner": "lanternworks", "repo": "ledger", "username": "iris-calder", "org": "lanternworks"}


def _operations() -> list[tuple[str, str]]:
    found = [
        (method.upper(), path)
        for path, operations in DESCRIPTION["paths"].items()
        for method in operations
        if method in ("get", "post", "put", "patch", "delete")
    ]
    return sorted(found)


OPERATIONS = _operations()
REFUSED = [op for op in OPERATIONS if op not in SERVED]


def _resolve(schema: dict[str, object]) -> dict[str, object]:
    return resolve(DESCRIPTION["components"], schema)


def _missing(schema: dict[str, object], value: object, where: str) -> list[str]:
    """Every field the description requires that `value` lacks, at any depth, by its path."""
    return missing(DESCRIPTION["components"], schema, value, where, PENDING)


def _success_schema(method: str, path: str) -> dict[str, object] | None:
    responses = DESCRIPTION["paths"][path][method.lower()]["responses"]
    status = next(s for s in ("200", "201", "204") if s in responses)
    if "content" not in _resolve(responses[status]):
        return None
    content = _resolve(responses[status])["content"]
    assert isinstance(content, dict)
    return content["application/json"]["schema"] if "application/json" in content else None


async def _call(http: httpx.AsyncClient, method: str, url: str, sent: object = None) -> httpx.Response:
    if sent is None and method in ("POST", "PUT", "PATCH"):
        sent = {}
    return await http.request(method, url, json=sent)


@pytest.fixture
def seeded() -> GitHubSeed:
    return tracker_seed()


def test_the_subset_holds_the_operations_it_is_counted_to_hold() -> None:
    """The counts the provider's README states: 169 operations, 48 served, 121 refused by name."""
    assert (len(OPERATIONS), len([op for op in OPERATIONS if op in SERVED]), len(REFUSED)) == (169, 48, 121)
    assert set(SERVED) <= set(OPERATIONS), set(SERVED) - set(OPERATIONS)


@pytest.mark.parametrize(("method", "path"), sorted(SERVED), ids=[f"{m} {p}" for m, p in sorted(SERVED)])
async def test_a_served_operation_answers_every_field_the_description_requires(
    hub: Hub, method: str, path: str
) -> None:
    call = SERVED[(method, path)]
    async with hub.client(TOMAS) as http:
        readme = (await http.get(f"{LEDGER}/contents/README.md")).json()["sha"]
        head = (await http.get(f"{LEDGER}/pulls/4")).json()["head"]["sha"]
        for earlier in call.before:
            assert (await _call(http, "PUT", earlier.url, earlier.json)).status_code == earlier.status
        answered = await _call(http, method, call.url.replace("{readme}", readme), _filled(call.json, head, readme))
    assert answered.status_code == call.status, answered.text
    schema = _success_schema(method, path)
    if call.status == 204:
        assert answered.content == b""
        return
    assert schema is not None
    gaps = _missing(schema, answered.json(), "$")
    assert gaps == [], " ".join(gaps)


def _filled(sent: object, head: str, readme: str) -> object:
    if isinstance(sent, dict):
        return {k: _filled(v, head, readme) for k, v in sent.items()}
    return {"{head}": head, "{readme}": readme}.get(sent, sent) if isinstance(sent, str) else sent


async def test_every_operation_the_provider_does_not_serve_is_refused_by_name(hub: Hub) -> None:
    unnamed: list[str] = []
    async with hub.client() as http:
        for method, path in REFUSED:
            url = PLACEHOLDER.sub(lambda m: WORLD_VALUES.get(m.group(1), "1"), path)
            answered = await _call(http, method, url)
            message = answered.json()["message"] if answered.status_code == 501 else ""
            if f"minutehand's github fake does not implement {method} {url}" not in message:
                unnamed.append(f"{method} {path}: {answered.status_code} {answered.text[:120]}")
    assert unnamed == []
