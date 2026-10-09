"""Attachments: a file uploaded as `multipart/form-data` to a task, listed, read and deleted, its bytes kept exactly
(https://developers.asana.com/reference/createattachmentforobject)."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx
import pytest

from minutehand.adapters.providers.asana import wire
from tests.providers.asana.asana_workspace import (
    VENUE,
    body,
    client,
    create,
    error,
    items,
    unserved,
    workspace,
)
from tests.providers.asana.rich_workspace import got

__all__ = ["client", "workspace"]

BYTES = bytes(range(256)) + b"\r\n--boundary-looking\r\n\x00\xff" * 3


async def upload(
    client: httpx.AsyncClient, parent: str, name: str = "plan.bin", content: bytes = BYTES
) -> httpx.Response:
    return await client.post(
        "/attachments", data={"parent": parent}, files={"file": (name, content, "application/octet-stream")}
    )


async def test_a_file_uploaded_comes_back_with_the_bytes_it_was_sent(client: httpx.AsyncClient) -> None:
    task = await create(client, name="Pack", projects=[VENUE])
    made = got(await upload(client, str(task["gid"])))
    assert (made["resource_subtype"], made["host"], made["name"], made["size"]) == (
        "asana",
        "asana",
        "plan.bin",
        len(BYTES),
    )
    assert made["parent"]["gid"] == task["gid"]
    fetched = await client.get(
        urlsplit(str(made["download_url"])).path + "?" + urlsplit(str(made["download_url"])).query
    )
    assert fetched.content == BYTES
    assert fetched.headers["content-type"] == "application/octet-stream"


async def test_an_attachment_is_listed_by_its_task_read_and_deleted(client: httpx.AsyncClient) -> None:
    task = await create(client, name="Pack", projects=[VENUE])
    other = await create(client, name="Other", projects=[VENUE])
    first = got(await upload(client, str(task["gid"]), "a.txt", b"one"))
    second = got(await upload(client, str(task["gid"]), "b.txt", b"two"))
    await upload(client, str(other["gid"]), "c.txt", b"three")
    listed = items(await client.get("/attachments", params={"parent": str(task["gid"])}))
    assert [(a["gid"], a["name"]) for a in listed] == [(first["gid"], "a.txt"), (second["gid"], "b.txt")]
    assert got(await client.get(f"/attachments/{first['gid']}"))["name"] == "a.txt"
    assert body(await client.delete(f"/attachments/{first['gid']}")) == {"data": {}}
    assert (await client.get(f"/attachments/{first['gid']}")).status_code == 404
    left = items(await client.get("/attachments", params={"parent": str(task["gid"])}))
    assert [a["gid"] for a in left] == [second["gid"]]


async def test_a_file_name_is_url_decoded_as_the_reference_tells_a_client_to_encode_it(
    client: httpx.AsyncClient,
) -> None:
    task = await create(client, name="Pack", projects=[VENUE])
    made = got(await upload(client, str(task["gid"]), quote("résumé.pdf")))
    assert made["name"] == "résumé.pdf"


async def test_an_upload_is_not_seen_as_a_change_to_a_name_it_does_not_have(client: httpx.AsyncClient) -> None:
    task = await create(client, name="Pack", projects=[VENUE])
    made = got(await upload(client, str(task["gid"])))
    read = got(await client.get(f"/attachments/{made['gid']}", params={"opt_fields": "name,parent.name,size"}))
    assert read == {
        "gid": made["gid"],
        "name": "plan.bin",
        "parent": {"gid": task["gid"], "name": "Pack"},
        "size": len(BYTES),
    }


@pytest.mark.parametrize("field", ["permanent_url", "view_url", "connected_to_app"])
async def test_a_url_the_fake_has_no_page_for_is_refused_by_name(client: httpx.AsyncClient, field: str) -> None:
    task = await create(client, name="Pack", projects=[VENUE])
    made = got(await upload(client, str(task["gid"])))
    answered = await client.get(f"/attachments/{made['gid']}", params={"opt_fields": f"name,{field}"})
    assert unserved(answered) == f"opt_fields={field}"


async def test_an_external_attachment_is_refused_by_name(client: httpx.AsyncClient) -> None:
    task = await create(client, name="Pack", projects=[VENUE])
    answered = await client.post(
        "/attachments",
        files={
            "parent": (None, str(task["gid"])),
            "url": (None, "https://example.com/x"),
            "name": (None, "x"),
            "resource_subtype": (None, "external"),
        },
    )
    assert unserved(answered) == "url"


async def test_an_attachment_on_a_project_is_refused_by_name(client: httpx.AsyncClient) -> None:
    answered = await upload(client, VENUE)
    assert unserved(answered) == "an attachment whose parent is a project"
    assert (
        unserved(await client.get("/attachments", params={"parent": VENUE}))
        == "an attachment whose parent is a project"
    )


async def test_an_upload_without_a_parent_or_a_file_is_refused_missing_input(client: httpx.AsyncClient) -> None:
    task = await create(client, name="Pack", projects=[VENUE])
    no_parent = await client.post("/attachments", files={"file": ("x", b"x", "text/plain")})
    no_file = await client.post(
        "/attachments", data={"parent": str(task["gid"])}, files={"other": ("x", b"x", "text/plain")}
    )
    assert (error(no_parent, 400), error(no_file, 400)) == ("parent: Missing input", "file: Missing input")


async def test_an_upload_to_a_parent_that_is_not_there_is_refused(client: httpx.AsyncClient) -> None:
    answered = await upload(client, "9999")
    assert error(answered, 400) == "parent: Unknown object: 9999"


async def test_a_file_over_the_size_limit_is_refused_by_name(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wire, "MAX_ATTACHMENT_BYTES", 10)
    task = await create(client, name="Pack", projects=[VENUE])
    answered = await upload(client, str(task["gid"]), content=b"x" * 11)
    assert "an attachment over 100MB" in unserved(answered)
    assert got(await upload(client, str(task["gid"]), content=b"x" * 10))["size"] == 10
