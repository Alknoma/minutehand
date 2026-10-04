"""What real Drive refuses, refused the same way: status, Google's envelope, and the `reason` a client branches on."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.providers.google_drive.drive_world import (
    AUTH,
    DOC,
    FOLDER,
    Drive,
    answer,
    create_doc,
    listed,
    multipart,
    names_of,
    reason_of,
)

MISSING = "1F00009999doesnotexist0000"


async def test_an_unknown_file_is_refused_404_not_found_by_every_method(api: httpx.AsyncClient) -> None:
    for response in [
        await api.get(f"/drive/v3/files/{MISSING}", headers=AUTH),
        await api.get(f"/drive/v3/files/{MISSING}/export", params={"mimeType": "text/plain"}, headers=AUTH),
        await api.patch(f"/drive/v3/files/{MISSING}", json={"name": "x"}, headers=AUTH),
        await api.delete(f"/drive/v3/files/{MISSING}", headers=AUTH),
        await api.post(f"/drive/v3/files/{MISSING}/copy", json={}, headers=AUTH),
        await api.get(f"/drive/v3/files/{MISSING}/permissions", headers=AUTH),
    ]:
        assert reason_of(response, 404) == "notFound"
    body = answer(await api.get(f"/drive/v3/files/{MISSING}", headers=AUTH), 404)
    assert body["error"] == {
        "code": 404,
        "message": f"File not found: {MISSING}.",
        "errors": [
            {
                "domain": "global",
                "reason": "notFound",
                "message": f"File not found: {MISSING}.",
                "location": "fileId",
                "locationType": "parameter",
            }
        ],
    }


async def test_an_unknown_document_is_refused_by_docs_in_its_own_shape(docs: httpx.AsyncClient) -> None:
    body = answer(await docs.get(f"/v1/documents/{MISSING}", headers=AUTH), 404)
    assert body == {"error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}}


@pytest.mark.parametrize(
    "q",
    [
        "name = 'Q3's Plans'",
        "name = 'unterminated",
        "colour = 'blue'",
        "name contains",
        "name > 'a'",
        "trashed = 'yes'",
        "name = 'a' and",
        "(name = 'a'",
        "modifiedTime > 'yesterday'",
    ],
)
async def test_a_malformed_q_is_refused_400_invalid(api: httpx.AsyncClient, q: str) -> None:
    response = await api.get("/drive/v3/files", params={"q": q}, headers=AUTH)
    assert reason_of(response, 400) == "invalid"


async def test_a_real_drive_term_this_fake_cannot_evaluate_is_refused_501_not_zero_results(
    api: httpx.AsyncClient,
) -> None:
    response = await api.get("/drive/v3/files", params={"q": "'mara@example.com' in owners"}, headers=AUTH)
    assert reason_of(response, 501) == "notImplemented"


async def test_exporting_a_file_that_is_not_a_docs_file_is_refused_403_file_not_exportable(
    api: httpx.AsyncClient,
) -> None:
    upload = answer(
        await api.post(
            "/upload/drive/v3/files",
            params={"uploadType": "media"},
            content=b"%PDF-1.7",
            headers={**AUTH, "Content-Type": "application/pdf"},
        )
    )
    folder = answer(await api.post("/drive/v3/files", json={"name": "F", "mimeType": FOLDER}, headers=AUTH))
    for file_id in (upload["id"], folder["id"]):
        response = await api.get(f"/drive/v3/files/{file_id}/export", params={"mimeType": "text/plain"}, headers=AUTH)
        assert reason_of(response, 403) == "fileNotExportable"


async def test_exporting_a_doc_to_an_unsupported_type_is_refused(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    image = await api.get(f"/drive/v3/files/{made['id']}/export", params={"mimeType": "image/png"}, headers=AUTH)
    pdf = await api.get(f"/drive/v3/files/{made['id']}/export", params={"mimeType": "application/pdf"}, headers=AUTH)
    none = await api.get(f"/drive/v3/files/{made['id']}/export", headers=AUTH)

    assert reason_of(image, 400) == "badRequest"
    assert reason_of(pdf, 501) == "notImplemented", "real Drive renders a PDF; this fake says it does not"
    assert reason_of(none, 400) == "required"


async def test_downloading_a_google_doc_with_alt_media_is_refused_403_file_not_downloadable(
    api: httpx.AsyncClient,
) -> None:
    made = await create_doc(api, "Minutes", "text")
    response = await api.get(f"/drive/v3/files/{made['id']}", params={"alt": "media"}, headers=AUTH)
    assert reason_of(response, 403) == "fileNotDownloadable"


async def test_creating_under_an_unknown_parent_is_refused_404(api: httpx.AsyncClient) -> None:
    response = await api.post("/drive/v3/files", json={"name": "x", "parents": [MISSING]}, headers=AUTH)
    assert reason_of(response, 404) == "notFound"
    assert MISSING in answer(response, 404)["error"]["message"]  # type: ignore[index]


async def test_creating_under_a_parent_that_is_not_a_folder_is_refused_400(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    response = await api.post("/drive/v3/files", json={"name": "x", "parents": [made["id"]]}, headers=AUTH)
    assert reason_of(response, 400) == "invalid"


async def test_moving_into_an_unknown_parent_is_refused_404(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    response = await api.patch(
        f"/drive/v3/files/{made['id']}", json={}, params={"addParents": MISSING, "removeParents": "root"}, headers=AUTH
    )
    assert reason_of(response, 404) == "notFound"


async def test_a_second_parent_is_refused_403_cannot_add_parent(api: httpx.AsyncClient) -> None:
    folder = answer(await api.post("/drive/v3/files", json={"name": "F", "mimeType": FOLDER}, headers=AUTH))
    made = await create_doc(api, "Minutes", "text")
    response = await api.patch(
        f"/drive/v3/files/{made['id']}", json={}, params={"addParents": str(folder["id"])}, headers=AUTH
    )
    assert reason_of(response, 403) == "cannotAddParent"


async def test_parents_in_an_update_body_is_refused_403_field_not_writable(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    response = await api.patch(f"/drive/v3/files/{made['id']}", json={"parents": ["root"]}, headers=AUTH)
    assert reason_of(response, 403) == "fieldNotWritable"


async def test_a_call_without_a_token_is_refused_401(api: httpx.AsyncClient, docs: httpx.AsyncClient) -> None:
    drive_answer = await api.get("/drive/v3/files")
    docs_answer = await docs.get(f"/v1/documents/{MISSING}")

    assert reason_of(drive_answer, 401) == "required"
    assert drive_answer.headers["www-authenticate"].startswith("Bearer")
    assert answer(docs_answer, 401)["error"]["status"] == "UNAUTHENTICATED"  # type: ignore[index]


async def test_comments_and_about_without_fields_are_refused_400_required(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    for response in [
        await api.get(f"/drive/v3/files/{made['id']}/comments", headers=AUTH),
        await api.post(f"/drive/v3/files/{made['id']}/comments", json={"content": "hi"}, headers=AUTH),
        await api.get("/drive/v3/about", headers=AUTH),
    ]:
        assert reason_of(response, 400) == "required"


@pytest.mark.parametrize("fields", ["id,colour", "files(id", "owners(nickname)", "name(first)"])
async def test_a_field_selection_naming_nothing_is_refused_400_invalid_parameter(
    api: httpx.AsyncClient,
    fields: str,
) -> None:
    made = await create_doc(api, "Minutes", "text")
    response = await api.get(f"/drive/v3/files/{made['id']}", params={"fields": fields}, headers=AUTH)
    assert reason_of(response, 400) == "invalidParameter"


@pytest.mark.parametrize(
    ("grant", "reason"),
    [
        ({"type": "user", "role": "editor", "emailAddress": "dov@example.com"}, "invalid"),
        ({"type": "person", "role": "reader", "emailAddress": "dov@example.com"}, "invalid"),
        ({"type": "user", "role": "reader"}, "invalidSharingRequest"),
        ({"type": "anyone", "role": "reader", "emailAddress": "dov@example.com"}, "invalidSharingRequest"),
    ],
)
async def test_a_permission_drive_would_not_grant_is_refused_400(
    api: httpx.AsyncClient,
    grant: dict[str, str],
    reason: str,
) -> None:
    made = await create_doc(api, "Minutes", "text")
    response = await api.post(f"/drive/v3/files/{made['id']}/permissions", json=grant, headers=AUTH)
    assert reason_of(response, 400) == reason


async def test_an_ownership_grant_without_transfer_ownership_is_refused_403(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    response = await api.post(
        f"/drive/v3/files/{made['id']}/permissions",
        json={"type": "user", "role": "owner", "emailAddress": "dov@example.com"},
        headers=AUTH,
    )
    assert reason_of(response, 403) == "forbidden"


@pytest.mark.parametrize("size", ["0", "1001", "ten"])
async def test_a_page_size_out_of_range_is_refused_400_invalid(api: httpx.AsyncClient, size: str) -> None:
    assert reason_of(await api.get("/drive/v3/files", params={"pageSize": size}, headers=AUTH), 400) == "invalid"


async def test_a_page_token_drive_did_not_hand_out_is_refused_400_invalid(api: httpx.AsyncClient) -> None:
    assert reason_of(await api.get("/drive/v3/files", params={"pageToken": "!!"}, headers=AUTH), 400) == "invalid"


async def test_an_upload_over_the_size_limit_is_refused_413(api: httpx.AsyncClient) -> None:
    too_big = b"x" * (5 * 1024 * 1024 + 1)
    response = await api.post(
        "/upload/drive/v3/files",
        params={"uploadType": "media"},
        content=too_big,
        headers={**AUTH, "Content-Type": "application/octet-stream"},
    )
    assert reason_of(response, 413) == "uploadTooLarge"


async def test_a_resumable_upload_is_refused_501(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/upload/drive/v3/files", params={"uploadType": "resumable"}, json={"name": "x"}, headers=AUTH
    )
    assert reason_of(response, 501) == "notImplemented"


async def test_converting_html_into_a_doc_is_refused_501(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/upload/drive/v3/files",
        params={"uploadType": "multipart"},
        content=multipart(json.dumps({"name": "x", "mimeType": DOC}), b"<p>hi</p>", "text/html"),
        headers={**AUTH, "Content-Type": 'multipart/related; boundary="===b0undary=="'},
    )
    assert reason_of(response, 501) == "notImplemented"


async def test_a_refusal_writes_nothing(drive: Drive, api: httpx.AsyncClient) -> None:
    head = drive.store.head()
    await api.post("/drive/v3/files", json={"name": "x", "parents": [MISSING]}, headers=AUTH)
    await api.get("/drive/v3/files", params={"q": "name = 'oops"}, headers=AUTH)
    assert drive.store.head() == head
    assert names_of(await listed(api, "name = 'x'")) == []
