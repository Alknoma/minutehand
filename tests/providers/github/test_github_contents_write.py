"""Committing a file through the REST API: `PUT` and `DELETE /repos/{owner}/{repo}/contents/{path}`. Each is one commit on
a branch, made by the user who asks (or by whom the request names), at the run's moment; what the agent writes comes
back as written."""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import cast

import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.seed import GitHubSeed
from minutehand.domain.world import Actor, Operation
from tests.providers.github.github_world import (
    IRIS,
    OUTSIDER,
    START,
    TOMAS,
    Hub,
    Json,
    body,
    listing,
    pulls_seed,
    refusal,
)

LEDGER = "/repos/lanternworks/ledger"
NEW = f"{LEDGER}/contents/docs/new.md"
TEXT = "# New — “quoted”\n\n\ttabbed \U0001f600  \r\nno final newline"


def encoded(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


async def put(hub: Hub, path: str, raw: bytes, token: str = TOMAS, **more: object) -> Json:
    async with hub.client(token) as http:
        sent = {"message": "Add " + path, "content": encoded(raw), **more}
        return body(await http.put(f"{LEDGER}/contents/{path}", json=sent), 201)


# ------------------------------------------------------------------------------------------------------ creating


async def test_a_file_is_committed_and_read_back_as_written(hub: Hub) -> None:
    """Documented: create-or-update-file-contents answers 201 for a new file with its `content` and its `commit`.
    Data stays as sent: the bytes and the message come back exactly. https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents"""
    raw = TEXT.encode()
    message = "Add the page — “new”\n\nWith a body.\n"
    async with hub.client(TOMAS) as http:
        before = (await http.get(f"{LEDGER}/commits")).json()[0]["sha"]
        made = body(await http.put(NEW, json={"message": message, "content": encoded(raw)}), 201)
        read = body(await http.get(NEW))
        head = listing(await http.get(f"{LEDGER}/commits"))[0]
        blob = body(await http.get(f"{LEDGER}/git/blobs/{wire.blob_sha(raw)}"))
    content, commit = cast(Json, made["content"]), cast(Json, made["commit"])
    assert (content["name"], content["path"], content["sha"], content["size"], content["type"]) == (
        "new.md",
        "docs/new.md",
        wire.blob_sha(raw),
        len(raw),
        "file",
    )
    assert content["url"] == "https://api.github.com/repos/lanternworks/ledger/contents/docs/new.md?ref=main"
    assert content["html_url"] == "https://github.com/lanternworks/ledger/blob/main/docs/new.md"
    assert content["download_url"] == "https://raw.githubusercontent.com/lanternworks/ledger/main/docs/new.md"
    assert commit["message"] == message and commit["sha"] == head["sha"]
    assert [p["sha"] for p in cast(list[Json], commit["parents"])] == [before]
    assert base64.b64decode(str(read["content"])) == raw and read["sha"] == content["sha"]
    assert base64.b64decode(str(blob["content"])) == raw
    assert cast(Json, head["commit"])["message"] == message


async def test_every_byte_comes_back(hub: Hub) -> None:
    raw = bytes(range(256)) * 3
    made = await put(hub, "assets/all-bytes.bin", raw)
    async with hub.client() as http:
        read = body(await http.get(f"{LEDGER}/contents/assets/all-bytes.bin"))
    assert base64.b64decode(str(read["content"])) == raw and cast(Json, made["content"])["size"] == len(raw)


async def test_a_commit_is_by_the_user_who_asks_at_the_runs_moment(hub: Hub) -> None:
    """Documented: "`committer`: ... Default: the authenticated user."; "`author`: ... Default: The `committer` or the
    authenticated user if you omit `committer`." https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents"""
    hub.clock.jump(START + timedelta(hours=1))
    made = await put(hub, "docs/new.md", b"x")
    commit = cast(Json, made["commit"])
    for who in ("author", "committer"):
        assert cast(Json, commit[who]) == {
            "name": "Tomas Brandt",
            "email": "tomas@example.com",
            "date": "2026-08-24T11:50:03Z",
        }
    async with hub.client(TOMAS) as http:
        head = listing(await http.get(f"{LEDGER}/commits"))[0]
    assert cast(Json, head["author"])["login"] == "tomas-b" and cast(Json, head["committer"])["login"] == "tomas-b"


async def test_the_author_and_the_committer_the_request_names_are_the_commits(hub: Hub) -> None:
    """Documented: `author` and `committer` take a `name`, an `email` and a `date`; "You'll receive a `422` status code if
    `email` is omitted"."""
    author = {"name": "Iris Calder", "email": "iris@example.com", "date": "2026-08-20T08:30:00Z"}
    committer = {"name": "Release Robot", "email": "robot@example.com"}
    both = await put(hub, "docs/a.md", b"a", author=author, committer=committer)
    only_committer = await put(hub, "docs/b.md", b"b", committer={**committer, "date": "2026-08-21T09:00:00+02:00"})
    only_author = await put(hub, "docs/c.md", b"c", author=author)
    assert cast(Json, cast(Json, both["commit"])["author"]) == author
    assert cast(Json, cast(Json, both["commit"])["committer"]) == {**committer, "date": "2026-08-24T10:50:03Z"}
    assert cast(Json, cast(Json, only_committer["commit"])["author"]) == {**committer, "date": "2026-08-21T07:00:00Z"}
    assert cast(Json, cast(Json, only_author["commit"])["committer"])["name"] == "Tomas Brandt"
    async with hub.client() as http:
        listed = listing(await http.get(f"{LEDGER}/commits", params={"per_page": 3}))
    by_author = {c["commit"]["message"]: cast(Json, c["author"]) for c in cast(list[dict[str, Json]], listed)}
    assert by_author["Add docs/c.md"]["login"] == "iris-calder", "the author's email is a user's"
    assert by_author["Add docs/a.md"]["login"] == "iris-calder"
    assert listed[1]["author"] is None, "the robot is no user of this GitHub"


@pytest.mark.parametrize(
    "named",
    [
        {"author": {"name": "Only A Name"}},
        {"committer": {"email": "only@example.com"}},
        {"author": "Iris"},
        {"author": {"name": "N", "email": "e@x", "date": "yesterday"}},
    ],
    ids=["no-email", "no-name", "not-an-object", "bad-date"],
)
async def test_an_author_or_committer_missing_a_name_or_an_email_is_422(hub: Hub, named: Json) -> None:
    async with hub.client(TOMAS) as http:
        sent = {"message": "m", "content": encoded(b"x"), **named}
        refusal(await http.put(NEW, json=sent), 422, "Invalid request")


# ------------------------------------------------------------------------------------------------------ updating


async def test_a_file_is_replaced_with_the_sha_it_replaces(hub: Hub) -> None:
    """Documented: `sha` is "**Required if you are updating a file**" and the answer is 200; a `sha` that is not the
    file's is 409 (the reference's "Conflict")."""
    async with hub.client(TOMAS) as http:
        old = body(await http.get(f"{LEDGER}/contents/services/billing/config.py"))
        before = (await http.get(f"{LEDGER}/commits")).json()[0]["sha"]
        refusal(
            await http.put(
                f"{LEDGER}/contents/services/billing/config.py",
                json={"message": "m", "content": encoded(b"x = 1\n")},
            ),
            422,
            "Invalid request",
        )
        refusal(
            await http.put(
                f"{LEDGER}/contents/services/billing/config.py",
                json={"message": "m", "content": encoded(b"x = 1\n"), "sha": "0" * 40},
            ),
            409,
            "Conflict",
        )
        made = body(
            await http.put(
                f"{LEDGER}/contents/services/billing/config.py",
                json={"message": "Raise it", "content": encoded(b"PAYMENT_TIMEOUT = 60\n"), "sha": old["sha"]},
            )
        )
        read = body(await http.get(f"{LEDGER}/contents/services/billing/config.py"))
        scoped = listing(await http.get(f"{LEDGER}/commits", params={"path": "services/billing/config.py"}))
        again = await http.put(
            f"{LEDGER}/contents/services/billing/config.py",
            json={"message": "m", "content": encoded(b"PAYMENT_TIMEOUT = 60\n"), "sha": read["sha"]},
        )
    assert cast(Json, made["content"])["sha"] == read["sha"] != old["sha"]
    assert [p["sha"] for p in cast(list[Json], cast(Json, made["commit"])["parents"])] == [before]
    assert scoped[0]["sha"] == cast(Json, made["commit"])["sha"] and len(scoped) == 2
    assert again.status_code == 501 and "bytes it already has" in again.json()["message"]


async def test_a_new_file_cannot_come_with_a_sha(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.put(NEW, json={"message": "m", "content": encoded(b"x"), "sha": "0" * 40})
    assert refused.status_code == 501 and "holds no file to replace" in refused.json()["message"]


# ------------------------------------------------------------------------------------------------------ deleting


async def test_a_file_is_deleted_with_its_sha(hub: Hub) -> None:
    """Documented: delete-a-file takes `message` and `sha` (both required) and answers 200 with the commit and no
    `content`; a `sha` that is not the file's is 409. https://docs.github.com/en/rest/repos/contents#delete-a-file"""
    async with hub.client(TOMAS) as http:
        guide = body(await http.get(f"{LEDGER}/contents/docs/guide.md"))
        before = (await http.get(f"{LEDGER}/commits")).json()[0]["sha"]
        refusal(
            await http.request("DELETE", f"{LEDGER}/contents/docs/guide.md", json={"message": "m"}),
            422,
            "Invalid request",
        )
        refusal(
            await http.request("DELETE", f"{LEDGER}/contents/docs/guide.md", json={"sha": guide["sha"]}),
            422,
            "Invalid request",
        )
        refusal(
            await http.request("DELETE", f"{LEDGER}/contents/docs/guide.md", json={"message": "m", "sha": "0" * 40}),
            409,
            "Conflict",
        )
        refusal(
            await http.request("DELETE", f"{LEDGER}/contents/docs/none.md", json={"message": "m", "sha": guide["sha"]}),
            404,
            "Not Found",
        )
        gone = body(
            await http.request(
                "DELETE", f"{LEDGER}/contents/docs/guide.md", json={"message": "Drop it", "sha": guide["sha"]}
            )
        )
        after = await http.get(f"{LEDGER}/contents/docs/guide.md")
        docs = await http.get(f"{LEDGER}/contents/docs")
        head = listing(await http.get(f"{LEDGER}/commits"))[0]
        search = body(await http.get("/search/code", params={"q": "ledger repo:lanternworks/ledger"}))
    assert gone["content"] is None and cast(Json, gone["commit"])["message"] == "Drop it"
    assert [p["sha"] for p in cast(list[Json], cast(Json, gone["commit"])["parents"])] == [before]
    assert after.status_code == 404 and docs.status_code == 404, "a directory of nothing is no directory"
    assert head["sha"] == cast(Json, gone["commit"])["sha"]
    assert "docs/guide.md" not in [i["path"] for i in cast(list[Json], search["items"])]


# ------------------------------------------------------------------------------------------------------ where it goes


async def test_a_commit_to_a_branch_that_followed_the_head_leaves_it_behind(hub: Hub) -> None:
    """Documented: `branch` is "The branch name. Default: the repository's default branch." `release` was at the head; a
    commit to it moves it alone, and a commit to the default branch leaves it where it was."""
    async with hub.client(TOMAS) as http:
        first = body(
            await http.put(NEW, json={"message": "On release", "content": encoded(b"r"), "branch": "release"}), 201
        )
        branches = {b["name"]: cast(Json, b["commit"])["sha"] for b in listing(await http.get(f"{LEDGER}/branches"))}
        on_release = await http.get(NEW, params={"ref": "release"})
        on_main = await http.get(NEW)
        second = body(
            await http.put(f"{LEDGER}/contents/docs/other.md", json={"message": "On main", "content": encoded(b"m")}),
            201,
        )
        after = {b["name"]: cast(Json, b["commit"])["sha"] for b in listing(await http.get(f"{LEDGER}/branches"))}
        history = listing(await http.get(f"{LEDGER}/commits", params={"sha": "release"}))
    assert branches["release"] == cast(Json, first["commit"])["sha"] != branches["main"]
    assert on_release.status_code == 200 and on_main.status_code == 404
    assert after["release"] == branches["release"] and after["main"] == cast(Json, second["commit"])["sha"]
    assert history[0]["sha"] == branches["release"]
    assert str(cast(Json, first["content"])["url"]).endswith("?ref=release")


@pytest.mark.parametrize("seeded", [pulls_seed()])
async def test_a_commit_to_a_branch_of_its_own_adds_to_the_pull_request_from_it(hub: Hub, seeded: GitHubSeed) -> None:
    async with hub.client(TOMAS) as http:
        before = body(await http.get(f"{LEDGER}/pulls/4"))
        await http.put(
            f"{LEDGER}/contents/docs/extra.md",
            json={"message": "Also this", "content": encoded(b"extra\n"), "branch": "timeout"},
        )
        after = body(await http.get(f"{LEDGER}/pulls/4"))
        commits = listing(await http.get(f"{LEDGER}/pulls/4/commits"))
        files = listing(await http.get(f"{LEDGER}/pulls/4/files"))
        main = await http.get(f"{LEDGER}/contents/docs/extra.md")
    assert (before["commits"], after["commits"]) == (1, 2) and (before["changed_files"], after["changed_files"]) == (
        2,
        3,
    )
    assert cast(Json, after["head"])["sha"] == commits[-1]["sha"] != cast(Json, before["head"])["sha"]
    assert [f["filename"] for f in files] == ["docs/extra.md", "docs/timeouts.md", "services/billing/config.py"]
    assert main.status_code == 404


async def test_a_branch_that_does_not_exist_is_404(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        refusal(
            await http.put(NEW, json={"message": "m", "content": encoded(b"x"), "branch": "nowhere"}), 404, "Not Found"
        )
        refusal(
            await http.request(
                "DELETE", f"{LEDGER}/contents/README.md", json={"message": "m", "sha": "0" * 40, "branch": "nowhere"}
            ),
            404,
            "Not Found",
        )


async def test_a_commit_to_an_empty_repository_is_refused_by_name(hub: Hub) -> None:
    async with hub.client(IRIS) as http:
        refused = await http.put(
            "/repos/iris-calder/empty/contents/README.md", json={"message": "First", "content": encoded(b"hi\n")}
        )
    assert refused.status_code == 501 and "empty repository" in refused.json()["message"]


# ------------------------------------------------------------------------------------------------------ what it will not do


async def test_a_reader_cannot_commit_and_a_stranger_cannot_see_the_repository(hub: Hub) -> None:
    async with hub.client(IRIS) as http:  # an organization member reads the ledger and does not push
        refused = await http.put(NEW, json={"message": "m", "content": encoded(b"x")})
    assert refused.status_code == 501 and "without push access" in refused.json()["message"]
    async with hub.client(OUTSIDER) as http:
        refusal(await http.put(NEW, json={"message": "m", "content": encoded(b"x")}), 404, "Not Found")
        refusal(await http.request("DELETE", NEW, json={"message": "m", "sha": "0" * 40}), 404, "Not Found")


@pytest.mark.parametrize(
    ("sent", "named"),
    [
        ({"message": "m", "content": "!!! not base64"}, "not base64"),
        ({"message": "m", "content": "e!A=="}, "not base64"),
    ],
)
async def test_content_that_is_not_base64_is_refused_by_name(hub: Hub, sent: Json, named: str) -> None:
    async with hub.client(TOMAS) as http:
        refused = await http.put(NEW, json=sent)
    assert refused.status_code == 501 and named in refused.json()["message"]


@pytest.mark.parametrize(
    "sent",
    [{}, {"message": "m"}, {"content": "eA=="}, {"message": 5, "content": "eA=="}, {"message": "m", "content": 5}],
)
async def test_a_commit_without_a_message_or_content_is_422_invalid_request(hub: Hub, sent: Json) -> None:
    async with hub.client(TOMAS) as http:
        refusal(await http.put(NEW, json=sent), 422, "Invalid request")


async def test_a_path_is_a_file_or_a_directory_never_both(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        under_a_file = await http.put(
            f"{LEDGER}/contents/README.md/inner.md", json={"message": "m", "content": encoded(b"x")}
        )
        over_a_directory = await http.put(f"{LEDGER}/contents/docs", json={"message": "m", "content": encoded(b"x")})
    for refused in (under_a_file, over_a_directory):
        assert refused.status_code == 501


async def test_a_body_that_is_not_json_is_400(hub: Hub) -> None:
    async with hub.client(TOMAS) as http:
        refusal(await http.put(NEW, content=b"{broken"), 400, "Problems parsing JSON")


# ------------------------------------------------------------------------------------------------------ in the world


async def test_what_is_committed_is_everywhere_the_repository_is_read(hub: Hub) -> None:
    raw = b"def newly_committed_function():\n    return 'distinctive-marker-text'\n"
    await put(hub, "services/billing/fresh.py", raw)
    async with hub.client() as http:
        found = body(await http.get("/search/code", params={"q": "distinctive repo:lanternworks/ledger"}))
        languages = body(await http.get(f"{LEDGER}/languages"))
        listed = listing(await http.get(f"{LEDGER}/contents/services/billing"))
        tree = body(await http.get(f"{LEDGER}/git/trees/main", params={"recursive": "1"}))
        graph = (
            await http.post(
                "/graphql",
                json={
                    "query": '{ repository(owner: "lanternworks", name: "ledger") { object(expression: "HEAD:services/billing/fresh.py") { ... on Blob { text } } } }'
                },
            )
        ).json()
    assert [i["path"] for i in cast(list[Json], found["items"])] == ["services/billing/fresh.py"]
    assert cast(int, languages["Python"]) >= len(raw)
    assert "fresh.py" in [e["name"] for e in listed]
    assert "services/billing/fresh.py" in [t["path"] for t in cast(list[Json], tree["tree"])]
    assert graph["data"]["repository"]["object"]["text"] == raw.decode()


async def test_a_commit_is_the_agents_in_the_log_with_its_blob_and_its_file(hub: Hub) -> None:
    await put(hub, "docs/new.md", b"x")
    events = [e for e in hub.store.events() if e.actor is Actor.AGENT and e.operation is not Operation.READ]
    kinds = sorted(e.entity.external_id.split("/")[0] for e in events)
    assert kinds == ["blob", "file", "repo"]
