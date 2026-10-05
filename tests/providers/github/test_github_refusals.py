"""What GitHub refuses, refused the same way: credentials, visibility, refs, an empty repository, versions."""

from __future__ import annotations

import pytest

from tests.providers.github.github_world import NO_SCOPES, OUTSIDER, TOMAS, Hub, body, refusal


async def test_an_unknown_token_is_refused_as_bad_credentials(hub: Hub) -> None:
    async with hub.client("ghp_nobodyissuedthis000000000000000000000") as http:
        refusal(await http.get("/user"), 401, "Bad credentials")
        refusal(await http.get("/repos/iris-calder/notes"), 401, "Bad credentials")


async def test_an_authorization_scheme_github_does_not_take_is_refused(hub: Hub) -> None:
    async with hub.client(None, Authorization="Basic aXJpczpwYXNz") as http:
        refusal(await http.get("/user"), 401, "Bad credentials")


async def test_the_token_scheme_is_accepted_beside_bearer(hub: Hub) -> None:
    async with hub.client(None, Authorization="token ghp_iris0000000000000000000000000000000000") as http:
        assert body(await http.get("/user"))["login"] == "iris-calder"


async def test_without_a_token_a_public_repository_reads_and_the_user_is_refused(hub: Hub) -> None:
    async with hub.client(None) as http:
        assert body(await http.get("/repos/iris-calder/notes"))["permissions"] is None
        refusal(await http.get("/repos/lanternworks/ledger"), 404, "Not Found")
        refusal(await http.get("/user"), 401, "Requires authentication")


@pytest.mark.parametrize(
    "path",
    [
        "/repos/lanternworks/ledger",
        "/repos/lanternworks/ledger/contents/README.md",
        "/repos/lanternworks/ledger/commits",
        "/repos/lanternworks/ledger/git/trees/main",
        "/repos/lanternworks/nothing",
    ],
)
async def test_a_private_repository_without_access_is_not_found(hub: Hub, path: str) -> None:
    async with hub.client(OUTSIDER) as http:
        refusal(await http.get(path), 404, "Not Found")


async def test_a_classic_token_without_the_repo_scope_reaches_no_private_repository(hub: Hub) -> None:
    async with hub.client(NO_SCOPES) as http:
        refusal(await http.get("/repos/lanternworks/ledger"), 404, "Not Found")
        assert body(await http.get("/repos/iris-calder/notes"))["full_name"] == "iris-calder/notes"


async def test_a_fine_grained_token_reaches_only_what_it_selected(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        assert body(await http.get("/repos/lanternworks/ledger"))["full_name"] == "lanternworks/ledger"
        refusal(await http.get("/repos/lanternworks/plans"), 404, "Not Found")
        listed = await http.get("/user/repos")
    assert [r["full_name"] for r in listed.json()] == ["lanternworks/ledger"]


async def test_a_ref_that_names_no_commit_is_refused(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(
            await http.get("/repos/lanternworks/ledger/contents/README.md", params={"ref": "nope"}),
            404,
            "No commit found for the ref nope",
        )
        refusal(
            await http.get("/repos/lanternworks/ledger/commits", params={"sha": "nope"}),
            404,
            "No commit found for SHA: nope",
        )
        refusal(await http.get("/repos/lanternworks/ledger/git/trees/nope"), 404, "Not Found")


async def test_an_empty_repository_has_no_contents_and_no_history(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(await http.get("/repos/iris-calder/empty/contents"), 404, "This repository is empty.")
        refusal(await http.get("/repos/iris-calder/empty/commits"), 409, "Git Repository is empty.")


async def test_a_path_that_is_not_there_is_not_found(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(await http.get("/repos/lanternworks/ledger/contents/services/nothing.py"), 404, "Not Found")


async def test_a_blob_sha_that_is_not_a_sha_is_refused_and_an_unknown_one_not_found(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(await http.get("/repos/lanternworks/ledger/git/blobs/xyz"), 422, "Validation Failed")
        refusal(await http.get(f"/repos/lanternworks/ledger/git/blobs/{'0' * 40}"), 404, "Not Found")


async def test_an_api_version_github_does_not_serve_is_refused(hub: Hub) -> None:
    async with hub.client(**{"X-GitHub-Api-Version": "2019-01-01"}) as http:
        refusal(await http.get("/user"), 400, "API version 2019-01-01 is not supported.")
