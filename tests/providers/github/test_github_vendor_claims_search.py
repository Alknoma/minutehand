"""Vendor claims about GitHub's code search, each one a fact a retired home-grown emulator was tested for. Every
docstring says whether the claim is documented (with the page) or observed. `CLAIMS.md` is the index."""

from __future__ import annotations

from datetime import timedelta

import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed, SeedCommit, SeedFile, SeedRepository
from tests.providers.github.github_world import Hub, body, github_seed, refusal

LEDGER = "repo:lanternworks/ledger"
TEXT_MATCH = "application/vnd.github.text-match+json"

INDEXED_CEILING = 384 * 1024
LARGE_BUT_INLINE = "# ledger fixtures\nZEPHYRMARK = 1\n" + "# padding line for size\n" * (INDEXED_CEILING // 24 + 8)
"""Past code search's index ceiling and still well under the 1 MiB the contents endpoint carries inline."""


@pytest.fixture
def seeded() -> GitHubSeed:
    bulky = SeedRepository(
        owner="iris-calder",
        name="bulky",
        files=[
            SeedFile(path="fixtures/large.py", text=LARGE_BUT_INLINE),
            SeedFile(path="fixtures/small.py", text="ZEPHYRMARK_SMALL = 2\n"),
        ],
        commits=[SeedCommit(message="Add the fixtures", author="iris-calder", before=timedelta(days=1))],
    )
    base = github_seed()
    return base.model_copy(update={"repositories": [*base.repositories, bulky]})


async def _search(hub: Hub, q: str, **params: object) -> dict[str, object]:
    async with hub.client() as http:
        return body(await http.get("/search/code", params={"q": q, **params}))  # type: ignore[arg-type]


def _paths(found: dict[str, object]) -> list[str]:
    items = found["items"]
    assert isinstance(items, list)
    return [str(i["path"]) for i in items]


async def test_a_substring_of_a_token_does_not_match(hub: Hub) -> None:
    """Observed: the index matches whole tokens; part of a word finds nothing."""
    assert (await _search(hub, f"retry_with_backof {LEDGER}"))["total_count"] == 0
    assert (await _search(hub, f"retry_with_backoff {LEDGER}"))["total_count"] == 2


async def test_every_term_of_the_query_has_to_match(hub: Hub) -> None:
    """Observed: bare terms are joined by AND; a file has to hold all of them."""
    assert _paths(await _search(hub, f"checkout {LEDGER}")) == ["README.md", "services/billing/retry.py"]
    assert _paths(await _search(hub, f"checkout attempts {LEDGER}")) == ["services/billing/retry.py"]
    assert (await _search(hub, f"checkout zeppelin {LEDGER}"))["total_count"] == 0


async def test_a_repo_qualifier_scopes_the_search_and_one_the_token_cannot_see_is_refused_422(hub: Hub) -> None:
    """Documented: `repo:` limits the search to one repository.
    https://docs.github.com/en/search-github/searching-on-github/searching-code
    Observed: a repository that does not exist or is out of reach is a 422 saying it cannot be searched."""
    assert _paths(await _search(hub, "PAYMENT_TIMEOUT repo:lanternworks/ledger")) == ["services/billing/config.py"]
    async with hub.client() as http:
        found = refusal(
            await http.get("/search/code", params={"q": "retry repo:someone/elsewhere"}), 422, "Validation Failed"
        )
    errors = found["errors"]
    assert isinstance(errors, list) and "cannot be searched" in str(errors[0]["message"])


async def test_a_language_qualifier_keeps_only_files_in_that_language(hub: Hub) -> None:
    """Documented: `language:` filters by the file's language.
    https://docs.github.com/en/search-github/searching-on-github/searching-code"""
    assert _paths(await _search(hub, f"checkout {LEDGER} language:python")) == ["services/billing/retry.py"]
    assert (await _search(hub, f"checkout {LEDGER} language:rust"))["total_count"] == 0


async def test_a_query_of_qualifiers_alone_is_refused_422_invalid(hub: Hub) -> None:
    """Documented: a code search must include at least one search term.
    https://docs.github.com/en/rest/search/search#search-code"""
    async with hub.client() as http:
        found = refusal(await http.get("/search/code", params={"q": LEDGER}), 422, "Validation Failed")
    assert found["errors"][0]["code"] == "invalid"  # type: ignore[index]


async def test_an_empty_query_is_refused_422_missing(hub: Hub) -> None:
    """Observed: an empty `q` is refused as missing rather than as invalid."""
    async with hub.client() as http:
        found = refusal(await http.get("/search/code", params={"q": ""}), 422, "Validation Failed")
    assert found["errors"][0]["code"] == "missing"  # type: ignore[index]


async def test_results_are_paged_and_every_page_reports_the_same_total(hub: Hub) -> None:
    """Documented: `per_page` and `page` page the results; `total_count` is the whole result's size.
    https://docs.github.com/en/rest/search/search#search-code"""
    first = await _search(hub, f"billing {LEDGER}", per_page=1, page=1)
    second = await _search(hub, f"billing {LEDGER}", per_page=1, page=2)
    assert first["total_count"] == second["total_count"] == 4
    assert len(_paths(first)) == len(_paths(second)) == 1
    assert _paths(first) != _paths(second)


async def test_text_matches_come_only_when_the_text_match_media_type_is_asked_for(hub: Hub) -> None:
    """Documented: `text_matches` (the fragment and where each term sits in it) is sent only under the
    `application/vnd.github.text-match+json` media type. https://docs.github.com/en/rest/search/search#text-match-metadata"""
    plain = await _search(hub, f"PAYMENT_TIMEOUT {LEDGER}")
    async with hub.client(Accept=TEXT_MATCH) as http:
        asked = body(await http.get("/search/code", params={"q": f"PAYMENT_TIMEOUT {LEDGER}"}))
    assert "text_matches" not in plain["items"][0]  # type: ignore[index]
    [match] = asked["items"][0]["text_matches"]  # type: ignore[index]
    assert match["object_type"] == "FileContent" and match["property"] == "content"
    assert match["fragment"] == "PAYMENT_TIMEOUT = 45"
    assert match["matches"] == [{"text": "PAYMENT_TIMEOUT", "indices": [0, 15]}]


async def test_a_file_past_the_index_ceiling_is_not_searchable_though_it_reads_inline(hub: Hub) -> None:
    """Documented: only files smaller than 384 KB are searchable.
    https://docs.github.com/en/rest/search/search#search-code"""
    assert len(LARGE_BUT_INLINE.encode()) > INDEXED_CEILING
    assert _paths(await _search(hub, "zephyrmark repo:iris-calder/bulky")) == []
    assert _paths(await _search(hub, "zephyrmark_small repo:iris-calder/bulky")) == ["fixtures/small.py"]
    async with hub.client() as http:
        inline = body(await http.get("/repos/iris-calder/bulky/contents/fixtures/large.py"))
    assert inline["encoding"] == "base64"


async def test_a_hit_carries_no_score_made_up_by_the_provider(hub: Hub) -> None:
    """The description requires `score` and documents nothing of how it is computed, and recording it needs a
    credential: it is left out rather than invented (CLAIMS.md, "Pending a recording")."""
    async with hub.client() as http:
        found = body(await http.get("/search/code", params={"q": "retry repo:lanternworks/ledger"}))
    items = found["items"]
    assert isinstance(items, list) and items and all("score" not in item for item in items)
