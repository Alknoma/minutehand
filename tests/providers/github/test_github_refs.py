"""Branches as git references, as an agent's coding helper makes and reads them through the REST API: a branch made at a
commit, read back by its reference and as a branch, then committed to and opened as a pull request. What GitHub refuses
is refused in its words, recorded of the real service (https://github.com/sapientpants/software-factory/pull/31)."""

from __future__ import annotations

import base64
from typing import cast

import pytest

from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.domain.world import Actor, RecordSnapshot
from tests.providers.github.github_world import TOMAS, Hub, Json, body, listing, pulls_seed, refusal

LEDGER = "/repos/lanternworks/ledger"
REFS = f"{LEDGER}/git/refs"
CREATE_DOCS = "https://docs.github.com/rest/git/refs#create-a-reference"


@pytest.fixture
def seeded() -> GitHubSeed:
    return pulls_seed()


def encoded(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def sha_of(found: Json) -> str:
    return str(cast(Json, found["commit"])["sha"])


async def history(hub: Hub, ref: str = "main") -> list[str]:
    """The shas of the commits a ref has, newest first."""
    async with hub.client(TOMAS) as http:
        return [str(c["sha"]) for c in listing(await http.get(f"{LEDGER}/commits", params={"sha": ref}))]


# ------------------------------------------------------------------------------------------------------ making a branch


async def test_a_branch_made_at_the_default_branchs_head_is_committed_to_and_a_pull_request_is_opened_from_it(
    hub: Hub,
) -> None:
    """Documented: a reference is made at a sha and answered 201 with `ref`, `node_id`, `url` and `object`; the branch
    it names is then a branch like any other: listed, committed to with `branch`, a pull request's `head`.
    https://docs.github.com/en/rest/git/refs#create-a-reference"""
    page = {"message": "Describe the retries", "content": encoded(b"# Retries\n"), "branch": "describe-retries"}
    async with hub.client(TOMAS) as http:
        head = sha_of(body(await http.get(f"{LEDGER}/branches/main")))
        by_ref = body(await http.get(f"{LEDGER}/git/ref/heads/main"))
        made = await http.post(REFS, json={"ref": "refs/heads/describe-retries", "sha": head})
        listed = {b["name"]: sha_of(b) for b in listing(await http.get(f"{LEDGER}/branches"))}
        committed = body(await http.put(f"{LEDGER}/contents/docs/retries.md", json=page), 201)
        branch = body(await http.get(f"{LEDGER}/branches/describe-retries"))
        pulled = body(
            await http.post(
                f"{LEDGER}/pulls", json={"title": "Describe the retries", "head": "describe-retries", "base": "main"}
            ),
            201,
        )
        files = listing(await http.get(f"{LEDGER}/pulls/{pulled['number']}/files"))
        on_main = await http.get(f"{LEDGER}/contents/docs/retries.md")
        on_branch = await http.get(f"{LEDGER}/contents/docs/retries.md", params={"ref": "describe-retries"})
        main_after = sha_of(body(await http.get(f"{LEDGER}/branches/main")))
    assert cast(Json, by_ref["object"])["sha"] == head
    created = body(made, 201)
    assert list(created) == ["ref", "node_id", "url", "object"]
    assert (created["ref"], created["url"]) == (
        "refs/heads/describe-retries",
        "https://api.github.com/repos/lanternworks/ledger/git/refs/heads/describe-retries",
    )
    assert created["object"] == {
        "sha": head,
        "type": "commit",
        "url": f"https://api.github.com/repos/lanternworks/ledger/git/commits/{head}",
    }
    assert made.headers["Location"] == created["url"]
    assert listed["describe-retries"] == head, "made at the head, it follows it until it is committed to"
    assert sha_of(branch) == cast(Json, committed["commit"])["sha"] != head
    assert [f["filename"] for f in files] == ["docs/retries.md"]
    assert (pulled["commits"], cast(Json, pulled["head"])["ref"]) == (1, "describe-retries")
    assert on_branch.status_code == 200 and on_main.status_code == 404
    assert main_after == head, "the default branch did not move"


async def test_a_branch_made_at_a_commit_of_a_branch_of_its_own_reads_at_that_commit(hub: Hub) -> None:
    """`cleanup` drops the guide, then says where it went: a branch made at the first of the two has the first and not
    the second, and its commit is found by its short sha though two branches now hold it."""
    said_where, dropped, *_ = await history(hub, "cleanup")
    async with hub.client(TOMAS) as http:
        body(await http.post(REFS, json={"ref": "refs/heads/guide-dropped", "sha": dropped}), 201)
        branch = body(await http.get(f"{LEDGER}/branches/guide-dropped"))
        guide = await http.get(f"{LEDGER}/contents/docs/guide.md", params={"ref": "guide-dropped"})
        moved_here = await http.get(f"{LEDGER}/contents/docs/moved.md", params={"ref": "guide-dropped"})
        moved_there = await http.get(f"{LEDGER}/contents/docs/moved.md", params={"ref": "cleanup"})
        by_short_sha = listing(await http.get(f"{LEDGER}/commits", params={"sha": dropped[:7]}))
    assert sha_of(branch) == dropped != said_where
    assert guide.status_code == 404 and moved_here.status_code == 404 and moved_there.status_code == 200
    assert by_short_sha[0]["sha"] == dropped


async def test_a_branch_made_where_a_branch_of_its_own_was_made_reads_the_tree_of_that_commit(hub: Hub) -> None:
    """A commit to the default branch leaves `release` where it was; a branch made at that commit has its files, not
    the new one's."""
    old_head = (await history(hub))[0]
    async with hub.client(TOMAS) as http:
        body(
            await http.put(
                f"{LEDGER}/contents/docs/later.md", json={"message": "Add a later page", "content": encoded(b"later\n")}
            ),
            201,
        )
        body(await http.post(REFS, json={"ref": "refs/heads/before-the-page", "sha": old_head}), 201)
        branch = body(await http.get(f"{LEDGER}/branches/before-the-page"))
        later = await http.get(f"{LEDGER}/contents/docs/later.md", params={"ref": "before-the-page"})
        readme = await http.get(f"{LEDGER}/contents/README.md", params={"ref": "before-the-page"})
    assert sha_of(branch) == old_head
    assert later.status_code == 404 and readme.status_code == 200


async def test_making_a_branch_is_the_agents_write_of_its_repository(hub: Hub) -> None:
    head = (await history(hub))[0]
    async with hub.client(TOMAS) as http:
        body(await http.post(REFS, json={"ref": "refs/heads/describe-retries", "sha": head}), 201)
    written = [
        e.after
        for e in hub.store.events()
        if e.actor is Actor.AGENT and isinstance(e.after, RecordSnapshot) and e.after.resource == "refs"
    ]
    assert [(w.resource, w.text) for w in written] == [("refs", "refs/heads/describe-retries")]


# ------------------------------------------------------------------------------------------------ what GitHub refuses


async def test_a_reference_that_exists_already_is_refused_422_whatever_the_sha(hub: Hub) -> None:
    """Observed: a second create of a reference that exists is 422 "Reference already exists", at the same sha or
    another commit."""
    head, older, *_ = await history(hub)
    async with hub.client(TOMAS) as http:
        for ref, sha in (("refs/heads/main", head), ("refs/heads/release", older), ("refs/heads/timeout", head)):
            refused = refusal(await http.post(REFS, json={"ref": ref, "sha": sha}), 422, "Reference already exists")
            assert refused["documentation_url"] == CREATE_DOCS


@pytest.mark.parametrize(
    ("sha", "message"),
    [
        ("0123456789abcdef0123456789abcdef01234567", "Object does not exist"),
        (
            "abc123",
            "Invalid request.\n\nAt least 40 characters are required; only 6 were supplied.\n"
            "abc123 does not match /^([0-9a-f]{40}(?:[0-9a-f]{24})?)$/.",
        ),
        (None, 'Invalid request.\n\n"sha" wasn\'t supplied.'),
        ("{blob}", "Reference update failed"),
        ("{tree}", "Reference update failed"),
    ],
    ids=["absent", "short", "none", "a-blob", "a-tree"],
)
async def test_a_sha_that_names_no_commit_is_refused_422_in_githubs_words(
    hub: Hub, sha: str | None, message: str
) -> None:
    """Observed: an absent object, a sha too short to be one, no sha, and a tree or a blob are each 422, told apart
    only by the message."""
    async with hub.client(TOMAS) as http:
        blob = str(body(await http.get(f"{LEDGER}/contents/README.md"))["sha"])
        tree = str(body(await http.get(f"{LEDGER}/git/trees/main"))["sha"])
        sent: Json = {"ref": "refs/heads/describe-retries"}
        if sha is not None:
            sent["sha"] = {"{blob}": blob, "{tree}": tree}[sha] if sha.startswith("{") else sha
        refused = refusal(await http.post(REFS, json=sent), 422, message)
        branches = [b["name"] for b in listing(await http.get(f"{LEDGER}/branches"))]
    assert refused["documentation_url"] == CREATE_DOCS
    assert "describe-retries" not in branches


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("heads/describe-retries", "Reference name must start with 'refs/'."),
        ("describe-retries", "Reference name must start with 'refs/'."),
        ("refs/heads/describe..retries", "refs/heads/describe..retries is not a valid ref name."),
        ("refs/heads/describe retries", "refs/heads/describe retries is not a valid ref name."),
        ("refs/heads/describe.lock", "refs/heads/describe.lock is not a valid ref name."),
        ("refs/heads/release/describe", "Reference update failed"),
    ],
    ids=["no-refs-prefix", "bare-name", "two-dots", "a-space", "a-lock", "under-a-branch"],
)
async def test_a_reference_name_github_rejects_is_refused_422_in_its_words(hub: Hub, ref: str, message: str) -> None:
    """Observed: a name not under `refs/`, one git does not take, and one under an existing branch's name are each 422;
    which names git takes is git's (https://git-scm.com/docs/git-check-ref-format)."""
    head = (await history(hub))[0]
    async with hub.client(TOMAS) as http:
        refused = refusal(await http.post(REFS, json={"ref": ref, "sha": head}), 422, message)
    assert refused["documentation_url"] == CREATE_DOCS


async def test_a_branch_named_as_the_directory_of_another_is_refused_422(hub: Hub) -> None:
    """Observed: a name that is a directory of an existing branch's is 422 "Reference update failed"."""
    head = (await history(hub))[0]
    async with hub.client(TOMAS) as http:
        body(await http.post(REFS, json={"ref": "refs/heads/team/describe", "sha": head}), 201)
        refusal(await http.post(REFS, json={"ref": "refs/heads/team", "sha": head}), 422, "Reference update failed")


async def test_a_reference_the_world_cannot_hold_is_refused_by_name(hub: Hub) -> None:
    """A tag or any reference but a branch, a branch in an empty repository, and a branch at a commit of the default
    branch's history whose files are not kept: each 501, named."""
    head, older, *_ = await history(hub)
    async with hub.client(TOMAS) as http:
        tag = await http.post(REFS, json={"ref": "refs/tags/v1", "sha": head})
        one_slash = await http.post(REFS, json={"ref": "refs/describe", "sha": head})
        empty = await http.post("/repos/iris-calder/empty/git/refs", json={"ref": "refs/heads/main", "sha": "a" * 40})
        at_older = await http.post(REFS, json={"ref": "refs/heads/describe-retries", "sha": older})
    for refused, named in ((tag, "refs/tags/v1"), (one_slash, "refs/describe"), (empty, "empty"), (at_older, older)):
        assert refused.status_code == 501 and named in refused.json()["message"], refused.text


# --------------------------------------------------------------------------------------------------- reading one back


async def test_a_reference_is_read_by_heads_and_its_whole_name_and_nothing_else(hub: Hub) -> None:
    """Documented: the ref "must be formatted as `heads/<branch name>`" and one that "doesn't match an existing ref" is
    404; observed, a prefix of a branch's name matches none. https://docs.github.com/en/rest/git/refs#get-a-reference"""
    async with hub.client(TOMAS) as http:
        tips = {b["name"]: sha_of(b) for b in listing(await http.get(f"{LEDGER}/branches"))}
        release = body(await http.get(f"{LEDGER}/git/ref/heads/release"))
        timeout = body(await http.get(f"{LEDGER}/git/ref/heads/timeout"))
        for missing in ("release", "heads/relea", "tags/v1", "heads/nowhere"):
            refused = refusal(await http.get(f"{LEDGER}/git/ref/{missing}"), 404, "Not Found")
            assert refused["documentation_url"] == "https://docs.github.com/rest/git/refs#get-a-reference"
    assert (release["ref"], cast(Json, release["object"])["sha"]) == ("refs/heads/release", tips["release"])
    assert cast(Json, timeout["object"])["sha"] == tips["timeout"] != tips["main"]


async def test_a_branch_reads_whole_and_one_that_is_not_there_is_404_branch_not_found(hub: Hub) -> None:
    """Documented: a branch is its name, its commit, `_links`, `protected`, `protection` and `protection_url`.
    https://docs.github.com/en/rest/branches/branches#get-a-branch. Observed: one nobody protects has protection
    `enabled: false` and no required status checks (https://github.com/nazarli-shabnam/clevis/issues/788); one that is
    not there is 404 "Branch not found" (https://github.com/PyGithub/PyGithub/issues/3011)."""
    async with hub.client(TOMAS) as http:
        head = listing(await http.get(f"{LEDGER}/commits", params={"per_page": 1}))[0]
        release = body(await http.get(f"{LEDGER}/branches/release"))
        refused = refusal(await http.get(f"{LEDGER}/branches/nowhere"), 404, "Branch not found")
        under = await http.get(f"{LEDGER}/branches/release/protection")
    assert list(release) == ["name", "commit", "_links", "protected", "protection", "protection_url"]
    assert release["commit"] == head
    assert release["_links"] == {
        "self": "https://api.github.com/repos/lanternworks/ledger/branches/release",
        "html": "https://github.com/lanternworks/ledger/tree/release",
    }
    assert (release["protected"], release["protection"], release["protection_url"]) == (
        False,
        {"enabled": False, "required_status_checks": {"enforcement_level": "off", "contexts": [], "checks": []}},
        "https://api.github.com/repos/lanternworks/ledger/branches/release/protection",
    )
    assert refused["documentation_url"] == "https://docs.github.com/rest/branches/branches#get-a-branch"
    assert under.status_code == 501 and "protection of the branch release" in under.json()["message"]


async def test_an_empty_repository_has_no_reference_and_no_branch(hub: Hub) -> None:
    """Documented: reading a reference of an empty repository is 409 (the words are those the commit list answers
    there); it has no branch to read."""
    async with hub.client(TOMAS) as http:
        refusal(await http.get("/repos/iris-calder/empty/git/ref/heads/main"), 409, "Git Repository is empty.")
        refusal(await http.get("/repos/iris-calder/empty/branches/main"), 404, "Branch not found")
