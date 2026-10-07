"""Google's own client — `googleapiclient` with `google-auth` service-account credentials — against the app on uvicorn.

The app is served over TLS, under a certificate authority made for the test, because
the client keeps `https` for the media-upload URL even when `api_endpoint` names a
plain `http` base: it swaps only the host. The service-account key is generated here
and never written anywhere but the test's own temporary directory.

The server runs on the test's event loop and the blocking client in a worker thread,
so the loop stays free to answer it.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import google_auth_httplib2
import httplib2
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from google.oauth2 import credentials as user_credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build as discover
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload, MediaUpload

from minutehand.adapters.providers.google_workspace import state
from minutehand.domain.world import Actor, DocumentSnapshot, EntityRef, Operation
from tests.providers.google_workspace.drive_world import DOC, LATER, OWNER, Drive

T = TypeVar("T")
SCOPE = "https://www.googleapis.com/auth/drive"


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _certificates(directory: Path) -> tuple[Path, Path, Path]:
    """A CA, and a certificate for 127.0.0.1 it signed: (ca.pem, server.pem, server-key.pem)."""
    now = datetime.now(UTC)
    ca_key, server_key = _key(), _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test run authority")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    paths = directory / "ca.pem", directory / "server.pem", directory / "server-key.pem"
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(server.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(_pem(server_key))
    return paths


@dataclass
class Google:
    base: str
    ca: Path
    opened: list[httplib2.Http] = field(default_factory=list)

    def http(self, credentials: Any) -> google_auth_httplib2.AuthorizedHttp:
        http = httplib2.Http(ca_certs=str(self.ca), proxy_info=None)
        http.redirect_codes = http.redirect_codes - {308}  # as `googleapiclient.http.build_http` sets it
        self.opened.append(http)
        return google_auth_httplib2.AuthorizedHttp(credentials, http=http)

    def close(self) -> None:
        """Close the client's keep-alive connections, so the server's TLS shutdown has nothing to wait for."""
        for http in self.opened:
            http.close()

    def service_account(self) -> service_account.Credentials:
        return service_account.Credentials.from_service_account_info(
            {
                "type": "service_account",
                "project_id": "sim-project",
                "private_key_id": "k1",
                "private_key": _pem(_key()).decode(),
                "client_email": "reader@sim-project.iam.example.com",
                "client_id": "100000000000000000001",
                "token_uri": f"{self.base}/token",
            },
            scopes=[SCOPE],
        )

    def drive(self, credentials: Any) -> Any:
        return discover(
            "drive",
            "v3",
            http=self.http(credentials),
            static_discovery=True,
            client_options={"api_endpoint": f"{self.base}/drive/v3/"},
        )

    def docs(self, credentials: Any) -> Any:
        return discover(
            "docs",
            "v1",
            http=self.http(credentials),
            static_discovery=True,
            client_options={"api_endpoint": f"{self.base}/"},
        )


@pytest.fixture
async def google(drive: Drive, tmp_path: Path) -> AsyncIterator[Google]:
    ca, cert, key = _certificates(tmp_path)
    server = uvicorn.Server(
        uvicorn.Config(
            drive.provider.app(drive.store, drive.clock),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    google = Google(base=f"https://127.0.0.1:{port}", ca=ca)
    yield google
    google.close()
    server.should_exit = True
    await serving


async def off_loop(call: Callable[[], T]) -> T:
    return await asyncio.to_thread(call)


async def refused(call: Callable[[], object]) -> tuple[int, str]:
    """The status and `reason` of the `HttpError` the client raised."""
    with pytest.raises(HttpError) as raised:
        await off_loop(call)
    details = raised.value.error_details
    assert isinstance(details, list) and details
    return raised.value.resp.status, details[0]["reason"]


async def test_a_service_account_signs_in_writes_a_doc_finds_it_exports_it_and_reads_it_through_docs(
    drive: Drive,
    google: Google,
) -> None:
    credentials = google.service_account()
    files = google.drive(credentials).files()
    drive.clock.jump(LATER)

    made = await off_loop(
        lambda: files.create(
            body={"name": "Freight Plan", "mimeType": DOC},
            media_body=MediaInMemoryUpload(b"Rates rise in October.\nBook early.", mimetype="text/plain"),
            fields="id,name,mimeType,createdTime",
        ).execute()
    )
    found = await off_loop(
        lambda: files.list(q="name contains 'Freight' and trashed = false", fields="files(id,name)").execute()
    )
    exported = await off_loop(lambda: files.export(fileId=made["id"], mimeType="text/plain").execute())
    document = await off_loop(lambda: google.docs(credentials).documents().get(documentId=made["id"]).execute())

    assert credentials.token is not None and credentials.token.startswith("ya29.")
    assert made["mimeType"] == DOC and made["createdTime"] == "2026-09-14T11:37:11.250Z"
    assert found["files"] == [{"id": made["id"], "name": "Freight Plan"}]
    assert exported == "﻿Rates rise in October.\r\nBook early.\r\n".encode()
    runs = [e["paragraph"]["elements"][0]["textRun"]["content"] for e in document["body"]["content"][1:]]
    assert document["title"] == "Freight Plan" and runs == ["Rates rise in October.\n", "Book early.\n"]
    write = next(e for e in drive.store.events() if e.entity == state.file_ref(made["id"]))
    assert (write.actor, write.operation) == (Actor.AGENT, Operation.CREATE)
    assert isinstance(write.after, DocumentSnapshot)
    assert (write.after.title, write.after.mime_type) == ("Freight Plan", DOC)
    assert write.after.text == "Rates rise in October.\nBook early." and write.after.last_edited_at is not None


async def test_the_client_follows_page_tokens_to_the_end(google: Google) -> None:
    files = google.drive(google.service_account()).files()
    for n in range(3):
        await off_loop(lambda n=n: files.create(body={"name": f"Batch {n}", "mimeType": DOC}).execute())

    def every_page() -> list[list[str]]:
        pages: list[list[str]] = []
        request = files.list(q="name contains 'Batch'", pageSize=2, fields="nextPageToken,files(name)")
        while request is not None:
            page = request.execute()
            pages.append([f["name"] for f in page["files"]])
            request = files.list_next(request, page)
        return pages

    assert await off_loop(every_page) == [["Batch 0", "Batch 1"], ["Batch 2"]]


async def test_a_binary_upload_downloads_byte_for_byte(google: Google) -> None:
    files = google.drive(google.service_account()).files()
    media = bytes(range(256)) * 4
    made = await off_loop(
        lambda: files.create(
            body={"name": "chart.png"},
            media_body=MediaInMemoryUpload(media, mimetype="image/png"),
            fields="id,size",
        ).execute()
    )
    downloaded = await off_loop(lambda: files.get_media(fileId=made["id"]).execute())
    assert made["size"] == str(len(media)) and downloaded == media


async def test_refresh_token_credentials_sign_in_too(google: Google) -> None:
    credentials = user_credentials.Credentials(
        token=None,
        refresh_token="1//sim-refresh",
        client_id="client.example",
        client_secret="sim-secret",
        token_uri=f"{google.base}/token",
        scopes=[SCOPE],
    )
    about = await off_loop(lambda: google.drive(credentials).about().get(fields="user").execute())
    assert about["user"]["emailAddress"] == OWNER and credentials.token is not None


async def test_each_refusal_is_raised_as_an_http_error_with_drives_reason(google: Google) -> None:
    files = google.drive(google.service_account()).files()
    doc = await off_loop(lambda: files.create(body={"name": "Notes", "mimeType": DOC}).execute())
    binary = await off_loop(
        lambda: files.create(
            body={"name": "scan.pdf"},
            media_body=MediaInMemoryUpload(b"%PDF-1.7", mimetype="application/pdf"),
        ).execute()
    )

    assert await refused(lambda: files.get(fileId="1F00009999doesnotexist0000").execute()) == (404, "notFound")
    assert await refused(lambda: files.list(q="name = 'Q3's Plans'").execute()) == (400, "invalid")
    assert await refused(lambda: files.export(fileId=binary["id"], mimeType="text/plain").execute()) == (
        403,
        "fileNotExportable",
    )
    assert await refused(lambda: files.export(fileId=doc["id"], mimeType="image/png").execute()) == (400, "badRequest")
    assert await refused(lambda: files.get_media(fileId=doc["id"]).execute()) == (403, "fileNotDownloadable")
    assert await refused(
        lambda: files.create(body={"name": "x", "parents": ["1F00009999doesnotexist0000"]}).execute()
    ) == (404, "notFound")
    assert await refused(lambda: files.update(fileId=doc["id"], body={"parents": ["root"]}).execute()) == (
        403,
        "fieldNotWritable",
    )


class _Unmeasured(MediaUpload):
    """A stream whose length nobody knows until it ends: `googleapiclient` begins its resumable upload without
    `X-Upload-Content-Length`, sends each chunk as `bytes a-b/*`, and names the total only on the short read."""

    def __init__(self, payload: bytes, chunk: int) -> None:
        self._payload = payload
        self._chunk = chunk

    def chunksize(self) -> int:
        return self._chunk

    def size(self) -> None:
        return None

    def resumable(self) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride] — the base answers a literal False
        return True

    def getbytes(self, begin: int, end: int) -> bytes:
        """`end` is a length, as the library's own docstring says beneath its parameter's name."""
        return self._payload[begin : begin + end]


CHUNK = 256 * 1024


@pytest.mark.parametrize("size", [CHUNK * 2 + 17, CHUNK * 2], ids=["short-last-chunk", "exact-chunks"])
async def test_a_resumable_upload_of_unknown_length_arrives_whole(drive: Drive, google: Google, size: int) -> None:
    """Google's guide: `X-Upload-Content-Length` is optional
    (https://developers.google.com/workspace/drive/api/guides/manage-uploads#resumable); the total arrives with the
    last chunk."""
    files = google.drive(google.service_account()).files()
    payload = bytes((i * 7) % 251 for i in range(size))

    def upload() -> tuple[list[int | None], dict[str, str]]:
        request = files.create(body={"name": "stream.bin"}, media_body=_Unmeasured(payload, CHUNK), fields="id,size")
        progress: list[int | None] = []
        answer = None
        while answer is None:
            status, answer = request.next_chunk()
            progress.append(None if status is None else status.resumable_progress)
        return progress, answer

    progress, made = await off_loop(upload)
    downloaded = await off_loop(lambda: files.get_media(fileId=made["id"]).execute())
    assert made["size"] == str(size) and downloaded == payload
    assert progress[:2] == [CHUNK, CHUNK * 2] and progress[-1] is None
    begun = [s for s in drive.store.versions(next(iter(_sessions(drive))))]
    assert '"total"' not in begun[0].body, "the session began knowing its length"


async def test_an_interrupted_upload_of_unknown_length_asks_where_it_stands_and_resumes(
    drive: Drive, google: Google
) -> None:
    """After a failed chunk the client asks `Content-Range: bytes */*` and resumes from the `Range` answered
    (https://developers.google.com/workspace/drive/api/guides/manage-uploads#resume-upload)."""
    files = google.drive(google.service_account()).files()
    payload = bytes((i * 13) % 251 for i in range(CHUNK * 2 + 5))

    def upload() -> dict[str, str]:
        request = files.create(body={"name": "resumed.bin"}, media_body=_Unmeasured(payload, CHUNK), fields="id,size")
        request.next_chunk()
        request.resumable_progress = 0
        request._in_error_state = True  # what the client sets when a chunk's answer is lost
        answer = None
        while answer is None:
            _, answer = request.next_chunk()
        return answer

    made = await off_loop(upload)
    downloaded = await off_loop(lambda: files.get_media(fileId=made["id"]).execute())
    assert downloaded == payload


def _sessions(drive: Drive) -> list[EntityRef]:
    """Every resumable upload session the world holds: the records kept under `uploads`."""
    found: list[EntityRef] = []
    for event in drive.store.events():
        stored = drive.store.get(event.entity)
        if stored is not None and stored.parent == state.UPLOADS and event.entity not in found:
            found.append(event.entity)
    return found
