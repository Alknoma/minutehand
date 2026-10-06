"""A resumable upload begun without `X-Upload-Content-Length`, by hand, as Google's guide lays it out
(https://developers.google.com/workspace/drive/api/guides/manage-uploads#resumable): the header is optional, a
chunk names the total only once it is known (`bytes a-b/*` until then), an empty `PUT` asks how much has arrived
(`Content-Range: */*`, or `*/total`), and an interrupted upload resumes from the `Range` that question answers
(#resume-upload). The session, once complete, answers the file to every further question."""

from __future__ import annotations

import httpx

from tests.providers.google_workspace.drive_world import AUTH, answer, reason_of


async def _begin(api: httpx.AsyncClient, name: str, **headers: str) -> str:
    begun = await api.post(
        "/upload/drive/v3/files",
        params={"uploadType": "resumable", "fields": "id,name,size"},
        json={"name": name},
        headers={**AUTH, **headers},
    )
    assert begun.status_code == 200, begun.text
    return begun.headers["location"]


async def _put(api: httpx.AsyncClient, session: str, body: bytes, spelled: str | None) -> httpx.Response:
    headers = dict(AUTH) | ({"Content-Range": spelled} if spelled is not None else {})
    return await api.put(session, content=body, headers=headers)


async def _download(api: httpx.AsyncClient, file: str) -> bytes:
    got = await api.get(f"/drive/v3/files/{file}", params={"alt": "media"}, headers=AUTH)
    assert got.status_code == 200, got.text
    return got.content


async def test_chunks_of_an_unknown_total_are_answered_308_until_the_last_names_it(api: httpx.AsyncClient) -> None:
    session = await _begin(api, "unmeasured.bin")
    first = await _put(api, session, b"abcd", "bytes 0-3/*")
    second = await _put(api, session, b"efgh", "bytes 4-7/*")
    last = await _put(api, session, b"ij", "bytes 8-9/10")
    assert (first.status_code, first.headers["range"]) == (308, "bytes=0-3")
    assert (second.status_code, second.headers["range"]) == (308, "bytes=0-7")
    made = answer(last)
    assert made["name"] == "unmeasured.bin" and made["size"] == "10"
    assert await _download(api, str(made["id"])) == b"abcdefghij"


async def test_a_status_query_before_any_byte_is_answered_308_with_no_range(api: httpx.AsyncClient) -> None:
    session = await _begin(api, "empty-yet.bin")
    for spelled in ("*/*", "bytes */*"):
        asked = await _put(api, session, b"", spelled)
        assert asked.status_code == 308 and "range" not in asked.headers


async def test_an_interrupted_upload_asks_where_it_stands_and_resumes_from_the_range(api: httpx.AsyncClient) -> None:
    payload = bytes(range(200)) * 3
    session = await _begin(api, "resumed.bin", **{"X-Upload-Content-Type": "application/octet-stream"})
    assert (await _put(api, session, payload[:256], "bytes 0-255/*")).status_code == 308
    # The next chunk is lost on the way: the client never hears, and asks.
    asked = await _put(api, session, b"", "bytes */*")
    assert (asked.status_code, asked.headers["range"]) == (308, "bytes=0-255")
    resumed_at = int(asked.headers["range"].rsplit("-", 1)[1]) + 1
    made = answer(
        await _put(api, session, payload[resumed_at:], f"bytes {resumed_at}-{len(payload) - 1}/{len(payload)}")
    )
    assert await _download(api, str(made["id"])) == payload
    again = await _put(api, session, b"", f"*/{len(payload)}")
    assert answer(again)["id"] == made["id"]


async def test_one_put_with_no_content_range_is_the_whole_upload(api: httpx.AsyncClient) -> None:
    session = await _begin(api, "whole.txt")
    made = answer(await _put(api, session, b"all of it", None))
    assert made["size"] == "9" and await _download(api, str(made["id"])) == b"all of it"


async def test_a_chunk_naming_another_total_than_the_upload_declared_is_refused_400(api: httpx.AsyncClient) -> None:
    session = await _begin(api, "declared.bin", **{"X-Upload-Content-Length": "8"})
    assert reason_of(await _put(api, session, b"abcd", "bytes 0-3/9"), 400) == "badContent"


async def test_a_chunk_that_skips_bytes_is_refused_400(api: httpx.AsyncClient) -> None:
    session = await _begin(api, "skipped.bin")
    assert (await _put(api, session, b"abcd", "bytes 0-3/*")).status_code == 308
    assert reason_of(await _put(api, session, b"ijkl", "bytes 8-11/*"), 400) == "badContent"
