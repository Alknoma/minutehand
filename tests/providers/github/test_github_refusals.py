"""What GitHub refuses, refused the same way: visibility, refs, an empty repository, versions. Credentials are
never refused: Minutehand does not authenticate (docs/design.md, "Authentication is out of scope")."""

from __future__ import annotations

import base64

import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed
from tests.providers.github.github_world import NO_SCOPES, OUTSIDER, TOMAS, Hub, body, github_seed, refusal

UNSEEDED = "ghp_nobodyissuedthis000000000000000000000"
APP_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxMjM0NSJ9.c2lnbmF0dXJl"


@pytest.mark.parametrize(
    "authorization",
    [f"Bearer {UNSEEDED}", f"token {UNSEEDED}", f"Bearer {APP_JWT}", "Bearer", "Bearer ghs_installation0000", ""],
    ids=["unseeded-bearer", "unseeded-token", "app-jwt", "bearer-alone", "installation-token", "empty"],
)
async def test_any_credential_the_world_does_not_hold_acts_as_its_first_user(hub: Hub, authorization: str) -> None:
    async with hub.client(None, Authorization=authorization) as http:
        me = await http.get("/user")
        ledger = await http.get("/repos/lanternworks/ledger")
    assert body(me)["login"] == "iris-calder"
    assert body(ledger)["full_name"] == "lanternworks/ledger"
    assert "X-OAuth-Scopes" not in me.headers


async def test_a_call_with_no_authorization_acts_as_the_first_user_on_rest_graphql_and_search(hub: Hub) -> None:
    async with hub.client(None) as http:
        me = await http.get("/user")
        ledger = await http.get("/repos/lanternworks/ledger")
        viewer = await http.post("/graphql", json={"query": "query { viewer { login } }"})
        found = await http.get("/search/code", params={"q": "retry repo:lanternworks/ledger"})
    assert body(me)["login"] == "iris-calder" and me.headers["X-RateLimit-Limit"] == "5000"
    assert body(ledger)["full_name"] == "lanternworks/ledger"
    assert viewer.json() == {"data": {"viewer": {"login": "iris-calder"}}}
    assert body(found)["total_count"] > 0  # type: ignore[operator]


@pytest.mark.parametrize("seeded", [github_seed(unknown_credentials_act_as="outsider")])
async def test_the_seed_names_who_an_unknown_credential_acts_as(hub: Hub, seeded: GitHubSeed) -> None:
    async with hub.client(UNSEEDED) as http:
        assert body(await http.get("/user"))["login"] == "outsider"
        refusal(await http.get("/repos/lanternworks/ledger"), 404, "Not Found")


@pytest.mark.parametrize(
    ("credentials", "login"),
    [(b"tomas-b:hunter22", "iris-calder"), (b"anyone:" + TOMAS.encode(), "tomas-b")],
    ids=["password", "token-as-password"],
)
async def test_basic_authentication_is_accepted(hub: Hub, credentials: bytes, login: str) -> None:
    secret = base64.b64encode(credentials).decode()
    async with hub.client(None, Authorization=f"Basic {secret}") as http:
        assert body(await http.get("/user"))["login"] == login


async def test_the_token_scheme_is_accepted_beside_bearer(hub: Hub) -> None:
    async with hub.client(None, Authorization="token ghp_iris0000000000000000000000000000000000") as http:
        assert body(await http.get("/user"))["login"] == "iris-calder"


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


async def test_a_classic_token_without_the_repo_scope_reads_every_private_repository_its_user_may(hub: Hub) -> None:
    """Scopes are not enforced: the token's user reaches the private ledger through the organization, so the token
    does too, and its scopes are only echoed."""
    async with hub.client(NO_SCOPES) as http:
        found = await http.get("/repos/lanternworks/ledger")
    assert body(found)["full_name"] == "lanternworks/ledger"
    assert found.headers["X-OAuth-Scopes"] == ""


async def test_a_fine_grained_token_reads_everything_its_user_may_not_only_what_it_selected(hub: Hub) -> None:
    """A fine-grained token's selection of repositories is not enforced: its user collaborates on both."""
    async with hub.client(TOMAS) as http:
        assert body(await http.get("/repos/lanternworks/plans"))["full_name"] == "lanternworks/plans"
        listed = await http.get("/user/repos")
    assert [r["full_name"] for r in listed.json()] == ["lanternworks/ledger", "lanternworks/plans"]


async def test_a_ref_that_names_no_commit_is_refused(hub: Hub) -> None:
    async with hub.client() as http:
        refusal(
            await http.get("/repos/lanternworks/ledger/contents/README.md", params={"ref": "nope"}),
            404,
            "No commit found for the ref nope",
        )
        refusal(await http.get("/repos/lanternworks/ledger/commits", params={"sha": "nope"}), 404, "Not Found")
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
        refused = refusal(
            await http.get("/repos/lanternworks/ledger/git/blobs/xyz"),
            422,
            "The sha parameter must be exactly 40 characters and contain only [0-9a-f].",
        )
        assert "errors" not in refused
        refusal(await http.get(f"/repos/lanternworks/ledger/git/blobs/{'0' * 40}"), 404, "Not Found")


async def test_an_api_version_github_does_not_serve_is_refused(hub: Hub) -> None:
    """Observed 2026-10-08 (`observed/api.github.com.2026-10-08.json`): 400 "Bad Request", the reason in `errors`."""
    async with hub.client(**{"X-GitHub-Api-Version": "2019-01-01"}) as http:
        refused = refusal(await http.get("/user"), 400, "Bad Request")
    assert refused["errors"] == (
        'The version you specified in the "X-GitHub-API-Version" request header, "2019-01-01", is not a supported '
        'version. The following versions are currently supported: "2026-03-10" (most recent) and "2022-11-28".'
    )
    assert refused["documentation_url"] == "https://docs.github.com/rest"


async def test_an_api_version_github_serves_and_this_provider_does_not_is_refused_by_name(hub: Hub) -> None:
    async with hub.client(**{"X-GitHub-Api-Version": "2026-03-10"}) as http:
        refusal(
            await http.get("/user"),
            501,
            "minutehand's github fake does not implement GET /user: API version 2026-03-10: only 2022-11-28 is served",
        )
