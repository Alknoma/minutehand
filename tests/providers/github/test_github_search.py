"""Code search: every shape of `q` the client builds (`<text> repo:<o/n>`, `... language:<l>`), the qualifiers a
model may write into its text, and GitHub's refusals."""

from __future__ import annotations

import pytest

from tests.providers.github.github_world import Hub, body, refusal

LEDGER = "repo:lanternworks/ledger"


async def _paths(hub: Hub, q: str, token: str | None = None) -> list[object]:
    async with hub.client(token) if token is not None else hub.client() as http:
        found = body(await http.get("/search/code", params={"q": q, "per_page": 30}))
    items = found["items"]
    assert isinstance(items, list)
    assert found["total_count"] == len(items) and found["incomplete_results"] is False
    return [i["path"] for i in items]


@pytest.mark.parametrize(
    ("q", "paths"),
    [
        (f"retry_with_backoff {LEDGER}", ["docs/guide.md", "services/billing/retry.py"]),
        (f"retry_with_backoff {LEDGER} language:python", ["services/billing/retry.py"]),
        (f"retry_with_backoff {LEDGER} language:py", ["services/billing/retry.py"]),
        (f"checkoutButton {LEDGER} language:TypeScript", ["web/app.ts"]),
        (f'"doubling the wait" {LEDGER}', ["services/billing/retry.py"]),
        (f"PAYMENT_TIMEOUT {LEDGER}", ["services/billing/config.py"]),
        (f"timeout {LEDGER}", []),  # TIMEOUT_SECONDS is one token: an underscore joins
        (f"retry_limit {LEDGER} path:services/billing", ["services/billing/config.py"]),
        (f"retry {LEDGER} filename:retry.py", ["services/billing/retry.py"]),
        (f"restart {LEDGER} extension:md", ["docs/guide.md"]),
        (f"billing {LEDGER} in:path", ["services/billing/config.py", "services/billing/retry.py"]),
        (f"retry AND attempts {LEDGER}", ["services/billing/retry.py"]),
        ("retry org:lanternworks", ["services/billing/retry.py", "plan.md"]),
        ("billing user:iris-calder", ["README.md"]),
    ],
)
async def test_each_query_shape_finds_what_github_would(hub: Hub, q: str, paths: list[str]) -> None:
    assert await _paths(hub, q) == paths


async def test_a_term_matches_whole_tokens_never_a_substring(hub: Hub) -> None:
    assert await _paths(hub, f"check {LEDGER}") == []
    assert await _paths(hub, f"checkout {LEDGER}") == ["README.md", "services/billing/retry.py"]


async def test_binary_and_oversized_files_are_never_indexed(hub: Hub) -> None:
    assert await _paths(hub, f"generated {LEDGER}") == []  # only the 1.3 MB bundle says it


async def test_without_a_repo_qualifier_only_what_the_token_sees_is_searched(hub: Hub) -> None:
    assert await _paths(hub, "retry", "ghp_outsider000000000000000000000000000000") == []
    assert await _paths(hub, "billing", "ghp_outsider000000000000000000000000000000") == ["README.md"]


async def test_results_are_paged_by_link_header(hub: Hub) -> None:
    async with hub.client() as http:
        first = await http.get("/search/code", params={"q": f"billing {LEDGER}", "per_page": 1})
        second = await http.get("/search/code", params={"q": f"billing {LEDGER}", "per_page": 1, "page": 2})
    assert body(first)["total_count"] == 4 and len(body(first)["items"]) == 1  # type: ignore[arg-type]
    assert 'page=2>; rel="next"' in first.headers["Link"]
    assert body(second)["items"] != body(first)["items"]


@pytest.mark.parametrize(
    "q",
    [
        "repo:lanternworks/ledger",
        "language:python",
        "retry OR timeout repo:lanternworks/ledger",
        "retry NOT timeout repo:lanternworks/ledger",
        "retry -path:docs repo:lanternworks/ledger",
        "retry size:>100 repo:lanternworks/ledger",
        "retry in:comments repo:lanternworks/ledger",
        "retry repo:ledger",
    ],
)
async def test_a_query_it_cannot_read_is_refused_with_422(hub: Hub, q: str) -> None:
    async with hub.client() as http:
        found = refusal(await http.get("/search/code", params={"q": q}), 422, "Validation Failed")
    errors = found["errors"]
    assert isinstance(errors, list) and errors[0]["field"] == "q" and errors[0]["code"] == "invalid"


async def test_a_missing_q_is_refused_with_422_missing(hub: Hub) -> None:
    async with hub.client() as http:
        found = refusal(await http.get("/search/code"), 422, "Validation Failed")
    assert found["errors"] == [{"resource": "Search", "field": "q", "code": "missing"}]


@pytest.mark.parametrize("q", ["retry repo:lanternworks/nothing", "retry repo:lanternworks/plans", "retry user:nobody"])
async def test_a_repository_the_token_cannot_see_is_refused_with_422(hub: Hub, q: str) -> None:
    async with hub.client("ghp_outsider000000000000000000000000000000") as http:
        found = refusal(await http.get("/search/code", params={"q": q}), 422, "Validation Failed")
    errors = found["errors"]
    assert isinstance(errors, list) and "cannot be searched" in str(errors[0]["message"])


async def test_paging_past_the_first_thousand_results_is_refused(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(
            await http.get("/search/code", params={"q": f"billing {LEDGER}", "per_page": 100, "page": 11}),
            422,
            "Validation Failed",
        )
