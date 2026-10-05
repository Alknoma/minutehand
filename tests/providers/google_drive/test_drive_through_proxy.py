"""Every call a production Drive adapter makes, by stock `googleapiclient` over `httplib2` and stock `google-auth`,
in a process configured only by the environment Minutehand hands out, through the proxy.

The `q` strings, `fields` selections and keyword arguments are the adapter's own, quoted from its source."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.google_drive.seed import DriveSeed, FaultSeed
from minutehand.adapters.providers.google_drive.wire import FaultKind
from minutehand.domain.scenario import DocumentHappening, Edited, ProviderSeed, Trashed
from minutehand.domain.world import Actor, DocumentSnapshot, Operation
from tests.providers.google_drive.proxied import REFRESH, ROBOT, SCENARIO, START, SUPPLIERS, Google, google, serving

__all__ = ["google"]

pytestmark = pytest.mark.timeout(120)

FOLDER = "application/vnd.google-apps.folder"


def faults_seed(*faults: FaultSeed) -> ProviderSeed:
    """Drive's own seed, declaring these faults."""
    return ProviderSeed(provider="google_drive", body=DriveSeed(faults=list(faults)).model_dump_json())


async def test_httplib2_reaches_drive_only_through_the_proxy_it_is_handed_and_every_call_is_answered(
    google: Google,
) -> None:
    client = await google.client(
        """
from googleapiclient.http import MediaIoBaseUpload
q_folder = "name='Procurement' and mimeType='application/vnd.google-apps.folder' and trashed=false"
procurement = drive.files().list(q=q_folder, fields="files(id, name)", pageSize=100, supportsAllDrives=True).execute()
pid = procurement["files"][0]["id"]
nested = drive.files().list(
    q=f"name='2026' and mimeType='application/vnd.google-apps.folder' and trashed=false and '{pid}' in parents",
    fields="files(id,name,parents)", pageSize=100, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
fid = nested["files"][0]["id"]
found = drive.files().list(
    q="(name contains 'Supplier' or name contains 'Rates') and trashed=false"
      " and mimeType!='application/vnd.google-apps.folder'",
    fields="files(id, name, mimeType, parents, webViewLink)", pageSize=100, supportsAllDrives=True).execute()
roots = drive.files().list(
    q="'root' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
    spaces="drive", fields="files(id, name, parents)", orderBy="name", pageSize=100).execute()
inside = drive.files().list(
    q=f"'{fid}' in parents and trashed=false", pageSize=1000,
    fields="nextPageToken, files(id, name, mimeType, parents, modifiedTime, size, md5Checksum, owners(emailAddress),"
           " shortcutDetails/targetId, webViewLink)").execute()
doc_id = inside["files"][0]["id"]
got = drive.files().get(fileId=doc_id, fields="id,name,mimeType,webViewLink", supportsAllDrives=True).execute()
parents = drive.files().get(fileId=doc_id, fields="parents", supportsAllDrives=True).execute()
exported = drive.files().export(fileId=doc_id, mimeType="text/plain").execute()
everything = drive.files().list(q="trashed=false", fields="files(id,name,mimeType)", pageSize=100).execute()
by_name = {f["name"]: f for f in everything["files"]}
csv = drive.files().export(fileId=by_name["Rates"]["id"], mimeType="text/csv").execute()
deck = drive.files().export(fileId=by_name["Kickoff Deck"]["id"], mimeType="text/plain").execute()
notes = drive.files().get_media(fileId=by_name["notes.txt"]["id"], supportsAllDrives=True).execute()
made = drive.files().create(body={"name": "Contract Summary", "mimeType": "application/vnd.google-apps.document",
                                  "parents": [fid]}, fields="id,name", supportsAllDrives=True).execute()
folder = drive.files().create(body={"name": "Archive", "mimeType": "application/vnd.google-apps.folder",
                                    "parents": ["root"]}, fields="id,name", supportsAllDrives=True).execute()
png = drive.files().create(body={"name": "chart.png", "mimeType": "image/png"},
                           media_body=MediaIoBaseUpload(io.BytesIO(b"\\x89PNG" + bytes(4000)), mimetype="image/png"),
                           fields="id").execute()
shared = drive.permissions().create(fileId=png["id"], body={"type": "anyone", "role": "reader"},
                                    supportsAllDrives=True).execute()
granted = drive.permissions().create(fileId=made["id"],
    body={"type": "user", "role": "commenter", "emailAddress": "rosa@example.com"},
    sendNotificationEmail=True, supportsAllDrives=True).execute()
renamed = drive.files().update(fileId=made["id"], body={"name": "Contract Summary v2"}, fields="id,name",
                               supportsAllDrives=True).execute()
moved = drive.files().update(fileId=made["id"], addParents=folder["id"], removeParents=",".join([fid]),
                             fields="id, parents", supportsAllDrives=True).execute()
drive.files().delete(fileId=png["id"], supportsAllDrives=True).execute()
gone = refused(lambda: drive.files().get(fileId=png["id"], fields="id", supportsAllDrives=True).execute())
drives = drive.drives().list(pageSize=50, fields="drives(id, name)").execute()
me = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
say(procurement=procurement, found=sorted(f["name"] for f in found["files"]), roots=roots, inside=inside, got=got,
    parents=parents, exported=exported.decode("utf-8"), csv=csv.decode(), deck=deck.decode("utf-8"),
    notes=notes.decode(), made=made, renamed=renamed, moved=moved, folder=folder, shared=shared, granted=granted,
    gone=gone, drives=drives, me=me, fid=fid)
"""
    )
    seen = await client.heard()
    await client.finished()

    procurement = seen["procurement"]
    assert isinstance(procurement, dict) and [f["name"] for f in procurement["files"]] == ["Procurement"]
    assert seen["found"] == ["Rates", "Supplier Shortlist"]
    roots = seen["roots"]
    assert isinstance(roots, dict) and [f["name"] for f in roots["files"]] == ["Procurement"]
    inside = seen["inside"]
    assert isinstance(inside, dict)
    [listed] = inside["files"]
    assert listed["name"] == SUPPLIERS.title and listed["owners"] == [{"emailAddress": "mara@example.com"}]
    assert listed["modifiedTime"] == "2026-09-11T08:30:00.000Z", "three days before the start, as seeded"
    assert "size" not in listed and listed["webViewLink"].startswith("https://docs.google.com/document/d/")
    assert seen["exported"] == "﻿Shortlist\r\nThree suppliers remain.\r\n* Acme\r\n* Globex\r\n"
    assert seen["csv"] == 'supplier,rate\r\nAcme,12\r\n"Globex, Ltd",14\r\n'
    assert seen["deck"] == "﻿Kickoff\r\nWhy now\r\n"
    assert seen["notes"] == "call Acme back\n"
    assert seen["renamed"] == {"id": seen["made"]["id"], "name": "Contract Summary v2"}  # type: ignore[index]
    assert seen["moved"] == {"id": seen["made"]["id"], "parents": [seen["folder"]["id"]]}  # type: ignore[index]
    assert seen["shared"] == {"kind": "drive#permission", "id": "anyoneWithLink", "type": "anyone", "role": "reader"}
    assert seen["gone"] == [404, "notFound"]
    drives = seen["drives"]
    assert isinstance(drives, dict) and [d["name"] for d in drives["drives"]] == ["Partners"]
    me = seen["me"]
    assert isinstance(me, dict) and (me["email"], me["name"]) == ("mara@example.com", "Mara Lindqvist")

    calls = google.exchanges()
    assert calls and all(provider == "google_drive" for provider, _ in calls), [
        (p, e.host, e.path, e.status) for p, e in calls if p != "google_drive"
    ]
    assert {e.host for _, e in calls} == {"oauth2.googleapis.com", "www.googleapis.com"}
    assert all(e.status < 500 for _, e in calls)
    uploads = [e for _, e in calls if e.path.startswith("/upload/drive/v3/files")]
    assert [u.path.split("uploadType=")[1].split("&")[0] for u in uploads] == ["multipart"]


async def test_a_service_account_signs_in_as_itself_and_sees_only_what_is_shared_with_it(google: Google) -> None:
    client = await google.client(
        f"""
from google.oauth2 import service_account
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
robot = service_account.Credentials.from_service_account_info({{
    "type": "service_account", "project_id": "sim-project", "private_key_id": "k1", "private_key": key,
    "client_email": {ROBOT!r}, "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}},
    scopes=["https://www.googleapis.com/auth/drive"])
as_robot = build("drive", "v3", credentials=robot, cache_discovery=False)
before = as_robot.files().list(q="trashed=false", fields="files(name)").execute()
shared = drive.files().list(q="name='notes.txt'", fields="files(id)").execute()["files"][0]["id"]
drive.permissions().create(fileId=shared, body={{"type": "user", "role": "reader", "emailAddress": {ROBOT!r}}}).execute()
after = as_robot.files().list(q="trashed=false", fields="files(name)").execute()
about = as_robot.about().get(fields="user(emailAddress)").execute()
say(before=before, after=after, about=about)
"""
    )
    seen = await client.heard()
    await client.finished()
    assert seen["before"] == {"files": []}
    assert seen["after"] == {"files": [{"name": "notes.txt"}]}
    assert seen["about"] == {"user": {"emailAddress": ROBOT}}
    hosts = {(e.host, e.status) for _, e in google.exchanges()}
    assert ("iamcredentials.googleapis.com", 200) in hosts, "google-auth's boundary lookup is answered, not refused"


async def test_a_shared_drive_is_listed_by_corpora_and_drive_id_and_hidden_from_a_call_that_does_not_support_it(
    google: Google,
) -> None:
    client = await google.client(
        """
drives = drive.drives().list(pageSize=50, fields="drives(id, name)").execute()["drives"]
drive_id = drives[0]["id"]
in_drive = drive.files().list(q="trashed=false", fields="files(id,name,driveId)", pageSize=1000,
    supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="drive", driveId=drive_id).execute()
plan = in_drive["files"][0]["id"]
mine = drive.files().list(q="trashed=false", fields="files(name)", pageSize=1000, supportsAllDrives=True).execute()
everywhere = drive.files().list(q="trashed=false", fields="files(name)", pageSize=1000, supportsAllDrives=True,
                                includeItemsFromAllDrives=True).execute()
without = refused(lambda: drive.files().get(fileId=plan, fields="id").execute())
with_flag = drive.files().get(fileId=plan, fields="id,driveId", supportsAllDrives=True).execute()
no_items = refused(lambda: drive.files().list(q="trashed=false", corpora="drive", driveId=drive_id,
                                              supportsAllDrives=True).execute())
made = drive.files().create(body={"name": "Partner Notes", "mimeType": "application/vnd.google-apps.document",
                                  "parents": [drive_id]}, fields="id,driveId,owners", supportsAllDrives=True).execute()
say(drive_id=drive_id, in_drive=in_drive, mine=sorted(f["name"] for f in mine["files"]),
    everywhere=sorted(f["name"] for f in everywhere["files"]), without=without, with_flag=with_flag,
    no_items=no_items, made=made)
"""
    )
    seen = await client.heard()
    await client.finished()
    drive_id = seen["drive_id"]
    in_drive = seen["in_drive"]
    assert isinstance(in_drive, dict)
    assert [(f["name"], f["driveId"]) for f in in_drive["files"]] == [("Partner Plan", drive_id)]
    mine, everywhere = seen["mine"], seen["everywhere"]
    assert isinstance(mine, list) and isinstance(everywhere, list)
    assert "Partner Plan" not in mine and "Partner Plan" in everywhere and "Supplier Shortlist" in mine
    assert seen["without"] == [404, "notFound"]
    assert seen["with_flag"] == {"id": in_drive["files"][0]["id"], "driveId": drive_id}
    assert seen["no_items"] == [403, "teamDriveIncludeItemsRequired"]
    made = seen["made"]
    assert isinstance(made, dict) and made["driveId"] == drive_id and "owners" not in made


async def test_a_document_written_by_batch_update_through_the_proxy_reads_back_index_by_index(google: Google) -> None:
    client = await google.client(
        """
docs = build("docs", "v1", credentials=creds, cache_discovery=False)
made = drive.files().create(body={"name": "Contract Summary", "mimeType": "application/vnd.google-apps.document"},
                            fields="id,name").execute()
text = "Summary\\nTerms agreed.\\nPrice\\nTerm\\n"
at = 1 + len(text)
cells = [at + 4 + r * (2 * 2 + 1) + c * 2 for r in range(2) for c in range(2)]
requests = [
    {"insertText": {"location": {"index": 1}, "text": text}},
    {"updateParagraphStyle": {"range": {"startIndex": 1, "endIndex": 9},
                              "paragraphStyle": {"namedStyleType": "HEADING_1", "alignment": "CENTER"},
                              "fields": "namedStyleType,alignment"}},
    {"createParagraphBullets": {"range": {"startIndex": 23, "endIndex": 34},
                                "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN"}},
    {"updateTextStyle": {"range": {"startIndex": 9, "endIndex": 14},
                         "textStyle": {"bold": True, "fontSize": {"magnitude": 9, "unit": "PT"},
                                       "weightedFontFamily": {"fontFamily": "Roboto Mono", "weight": 400},
                                       "foregroundColor": {"color": {"rgbColor": {"red": 0.2}}}},
                         "fields": "bold,fontSize,weightedFontFamily,foregroundColor"}},
    {"insertTable": {"rows": 2, "columns": 2, "location": {"index": at}}},
] + [{"insertText": {"location": {"index": i}, "text": t}} for i, t in reversed(list(zip(cells, "wxyz")))]
replied = docs.documents().batchUpdate(documentId=made["id"], body={"requests": requests}).execute()
got = docs.documents().get(documentId=made["id"]).execute()
say(cells=cells, replied=replied, got=got)
"""
    )
    seen = await client.heard()
    await client.finished()
    got = seen["got"]
    assert isinstance(got, dict)
    content = got["body"]["content"]
    shape = [
        (
            e.get("startIndex"),
            e["endIndex"],
            "table" if "table" in e else e.get("paragraph", {}).get("paragraphStyle", {}).get("namedStyleType"),
        )
        for e in content
    ]
    assert seen["cells"] == [38, 40, 43, 45]
    assert shape == [
        (None, 1, None),
        (1, 9, "HEADING_1"),
        (9, 23, "NORMAL_TEXT"),
        (23, 29, "NORMAL_TEXT"),
        (29, 34, "NORMAL_TEXT"),
        (34, 35, "NORMAL_TEXT"),
        (35, 50, "table"),
        (50, 51, "NORMAL_TEXT"),
    ]
    assert content[1]["paragraph"]["paragraphStyle"]["alignment"] == "CENTER"
    terms = content[2]["paragraph"]["elements"][0]
    assert (terms["startIndex"], terms["endIndex"], terms["textRun"]["content"]) == (9, 14, "Terms")
    assert terms["textRun"]["textStyle"]["weightedFontFamily"] == {"fontFamily": "Roboto Mono", "weight": 400}
    assert "bullet" in content[3]["paragraph"] and "bullet" in content[4]["paragraph"]
    cells = [c["content"][0] for row in content[6]["table"]["tableRows"] for c in row["tableCells"]]
    assert [(c["startIndex"], c["paragraph"]["elements"][0]["textRun"]["content"]) for c in cells] == [
        (38, "w\n"),
        (41, "x\n"),
        (45, "y\n"),
        (48, "z\n"),
    ]
    replied = seen["replied"]
    assert isinstance(replied, dict) and replied["writeControl"]["requiredRevisionId"] == got["revisionId"]
    hosts = {e.host for _, e in google.exchanges()}
    assert "docs.googleapis.com" in hosts


async def test_a_resumable_upload_of_a_few_megabytes_arrives_in_chunks_and_downloads_whole(google: Google) -> None:
    size = 3 * 1024 * 1024 + 17
    client = await google.client(
        f"""
from googleapiclient.http import MediaIoBaseUpload
payload = bytes((i * 7) % 251 for i in range({size}))
media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/octet-stream", chunksize=1024 * 1024,
                          resumable=True)
request = drive.files().create(body={{"name": "dump.bin"}}, media_body=media, fields="id,size,md5Checksum")
answer = None
while answer is None:
    status, answer = request.next_chunk()
back = drive.files().get_media(fileId=answer["id"]).execute()
import hashlib
say(answer=answer, same=back == payload, md5=hashlib.md5(payload).hexdigest())
"""
    )
    seen = await client.heard()
    await client.finished()
    answer = seen["answer"]
    assert isinstance(answer, dict)
    assert answer["size"] == str(size) and answer["md5Checksum"] == seen["md5"] and seen["same"] is True
    uploads = [e for _, e in google.exchanges() if e.path.startswith("/upload/")]
    assert [(e.method, e.status) for e in uploads] == [
        ("POST", 200),
        ("PUT", 308),
        ("PUT", 308),
        ("PUT", 308),
        ("PUT", 200),
    ]


async def test_an_expired_token_is_refused_401_and_the_client_signs_in_again_mid_sequence(google: Google) -> None:
    client = await google.client(
        """
first = drive.files().list(q="name='notes.txt'", fields="files(name)").execute()
say(first=first, token=creds.token)
wait()
second = drive.files().list(q="name='notes.txt'", fields="files(name)").execute()
say(second=second, token=creds.token)
"""
    )
    first = await client.heard()
    google.clock.jump(google.clock.now() + timedelta(hours=2))
    await client.go()
    second = await client.heard()
    await client.finished()
    assert first["first"] == second["second"] == {"files": [{"name": "notes.txt"}]}
    assert first["token"] != second["token"]
    sequence = [(e.method, e.host, e.status) for _, e in google.exchanges()]
    assert sequence == [
        ("POST", "oauth2.googleapis.com", 200),
        ("GET", "www.googleapis.com", 200),
        ("GET", "www.googleapis.com", 401),
        ("POST", "oauth2.googleapis.com", 200),
        ("GET", "www.googleapis.com", 200),
    ]


async def test_a_revoked_refresh_token_is_refused_the_way_googles_refresh_path_reads_it(google: Google) -> None:
    client = await google.client(
        f"""
import httpx, google.auth.exceptions
from google.auth.transport.requests import Request
creds.refresh(Request())
before = drive.files().list(q="name='notes.txt'", fields="files(name)").execute()
revoked = httpx.post("https://oauth2.googleapis.com/revoke", params={{"token": {REFRESH!r}}},
                     headers={{"content-type": "application/x-www-form-urlencoded"}})
try:
    drive.files().list(fields="files(name)").execute()
    after = None
except google.auth.exceptions.RefreshError as failed:
    after = str(failed)
again = credentials()
try:
    again.refresh(Request())
    error = None
except google.auth.exceptions.RefreshError as failed:
    error = str(failed)
say(before=before, revoked=revoked.status_code, after=after, error=error)
"""
    )
    seen = await client.heard()
    await client.finished()
    assert seen["before"] == {"files": [{"name": "notes.txt"}]} and seen["revoked"] == 200
    assert "invalid_grant" in str(seen["after"]), "the access token went with the grant: a 401, then a refused refresh"
    statuses = [(e.host, e.status) for _, e in google.exchanges()]
    assert ("www.googleapis.com", 401) in statuses and statuses.count(("oauth2.googleapis.com", 400)) == 2
    assert "invalid_grant" in str(seen["error"]) and "Token has been expired or revoked." in str(seen["error"])


@dataclass
class Webhook:
    url: str
    heard: list[dict[str, str]]


@pytest.fixture
async def webhook() -> AsyncIterator[Webhook]:
    """The agent's own webhook route, which takes Drive's notifications and answers 200."""
    heard: list[dict[str, str]] = []

    async def drive_webhook(request: Request) -> Response:
        heard.append({k.lower(): v for k, v in request.headers.items() if k.lower().startswith("x-goog-")})
        return Response(status_code=200)

    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[Route("/api/v1/webhook/drive", drive_webhook, methods=["POST"])]),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield Webhook(url=f"http://127.0.0.1:{port}/api/v1/webhook/drive", heard=heard)
    server.should_exit = True
    await serving


async def test_the_changes_feed_drains_a_persons_edit_and_the_watch_tells_the_webhook(
    google: Google, webhook: Webhook
) -> None:
    client = await google.client(
        f"""
import time, uuid
start = drive.changes().getStartPageToken(supportsAllDrives=True).execute()["startPageToken"]
channel = drive.changes().watch(pageToken=start, supportsAllDrives=True, body={{
    "id": str(uuid.uuid4()), "type": "web_hook", "address": {webhook.url!r},
    "expiration": str(int(time.time() * 1000) + 24 * 3600 * 1000)}}).execute()
say(start=start, channel=channel)
wait()
fields = ("changes(fileId,file(name,mimeType,parents,trashed,modifiedTime),removed,changeType),"
          "newStartPageToken,nextPageToken")
token, seen = start, []
while True:
    page = drive.changes().list(pageToken=token, fields=fields, includeRemoved=True, spaces="drive",
                                supportsAllDrives=True).execute()
    seen += page["changes"]
    if "newStartPageToken" in page:
        break
    token = page["nextPageToken"]
quiet = drive.changes().list(pageToken=page["newStartPageToken"], fields=fields, includeRemoved=True,
                             spaces="drive", supportsAllDrives=True).execute()
drive.channels().stop(body={{"id": channel["id"], "resourceId": channel["resourceId"]}}).execute()
again = refused(lambda: drive.channels().stop(body={{"id": channel["id"], "resourceId": channel["resourceId"]}}).execute())
say(changes=seen, quiet=quiet, again=again)
"""
    )
    opened = await client.heard()
    channel = opened["channel"]
    assert isinstance(channel, dict)
    google.clock.jump(START + timedelta(hours=5))
    edit = DocumentHappening(
        document=SUPPLIERS.title,
        person="rosa",
        after=timedelta(hours=5),
        action=Edited(append="Initech added."),
    )
    rename = DocumentHappening(document="notes.txt", person="dov", after=timedelta(hours=5), action=Trashed())
    google.provider.change(edit, SCENARIO, google.store, google.clock)
    google.provider.change(rename, SCENARIO, google.store, google.clock)
    assert google.provider.watched(google.store, google.clock)
    await google.provider.notify(google.store, google.clock)
    await client.go()
    drained = await client.heard()
    await client.finished()

    assert [h["x-goog-resource-state"] for h in webhook.heard] == ["sync", "change"]
    assert [h["x-goog-message-number"] for h in webhook.heard] == ["1", "2"]
    assert {h["x-goog-channel-id"] for h in webhook.heard} == {channel["id"]}
    assert {h["x-goog-resource-id"] for h in webhook.heard} == {channel["resourceId"]}
    assert "x-goog-channel-token" not in webhook.heard[0], "the adapter sets no token"
    changes = drained["changes"]
    assert isinstance(changes, list)
    assert [(c["file"]["name"], c["file"]["trashed"], c["removed"]) for c in changes] == [
        (SUPPLIERS.title, False, False),
        ("notes.txt", True, False),
    ]
    assert changes[0]["file"]["modifiedTime"] == "2026-09-14T13:30:00.000Z"
    assert drained["quiet"] == {"changes": [], "newStartPageToken": drained["quiet"]["newStartPageToken"]}  # type: ignore[index]
    assert drained["again"] == [404, "notFound"]
    assert not google.provider.watched(google.store, google.clock)
    edited = [e for e in google.store.events() if e.actor is Actor.PERSON]
    assert [(e.operation, e.after) for e in edited] == [
        (Operation.UPDATE, DocumentSnapshot(title=SUPPLIERS.title, mime_type="application/vnd.google-apps.document")),
        (Operation.UPDATE, DocumentSnapshot(title="notes.txt", mime_type="text/plain")),
    ]


async def test_declared_faults_reach_the_client_in_googles_shape_and_its_retry_logic_reads_them(tmp_path: Path) -> None:
    faulty = SCENARIO.model_copy(
        update={
            "provider_seeds": [
                faults_seed(
                    FaultSeed(operation="files.list", kind=FaultKind.RATE_LIMITED, times=1),
                    FaultSeed(operation="documents.get", kind=FaultKind.RATE_LIMITED, times=1),
                    FaultSeed(operation="files.get", kind=FaultKind.FORBIDDEN, times=1),
                    FaultSeed(operation="changes.list", kind=FaultKind.EXPIRED, times=1),
                )
            ]
        }
    )
    async with serving(tmp_path, faulty) as google:
        client = await google.client(
            """
docs = build("docs", "v1", credentials=creds, cache_discovery=False)
listed = drive.files().list(q="name='notes.txt'", fields="files(id)").execute(num_retries=1)
forbidden = refused(lambda: drive.files().get(fileId=listed["files"][0]["id"]).execute())
doc = drive.files().list(q="name='Supplier Shortlist'", fields="files(id)").execute()["files"][0]["id"]
limited = refused(lambda: docs.documents().get(documentId=doc).execute())
read = docs.documents().get(documentId=doc).execute()["title"]
start = drive.changes().getStartPageToken().execute()["startPageToken"]
gone = refused(lambda: drive.changes().list(pageToken=start).execute())
say(listed=len(listed["files"]), forbidden=forbidden, limited=limited, read=read, gone=gone)
"""
        )
        seen = await client.heard()
        await client.finished()
        statuses = [
            (e.path.split("?")[0], e.status) for _, e in google.exchanges() if e.host != "oauth2.googleapis.com"
        ]
    assert seen == {
        "listed": 1,
        "forbidden": [403, "insufficientFilePermissions"],
        "limited": [429, None],
        "read": SUPPLIERS.title,
        "gone": [410, "expired"],
    }
    assert statuses[:2] == [("/drive/v3/files", 403), ("/drive/v3/files", 200)], "googleapiclient retried the 403"


async def test_a_deck_made_through_drive_is_built_by_slides_batch_update(google: Google) -> None:
    client = await google.client(
        """
slides = build("slides", "v1", credentials=creds, cache_discovery=False)
made = drive.files().create(body={"name": "Board Update", "mimeType": "application/vnd.google-apps.presentation"},
                            fields="id,name").execute()
first = slides.presentations().get(presentationId=made["id"]).execute()
png = drive.files().create(body={"name": "chart.png", "mimeType": "image/png"}, fields="id").execute()
url = f"https://drive.google.com/uc?id={png['id']}"
private = refused(lambda: slides.presentations().batchUpdate(presentationId=made["id"], body={"requests": [
    {"createImage": {"url": url, "elementProperties": {"pageObjectId": first["slides"][0]["objectId"]}}}]}).execute())
drive.permissions().create(fileId=png["id"], body={"type": "anyone", "role": "reader"}, supportsAllDrives=True).execute()
emu = {"magnitude": 3000000, "unit": "EMU"}
box = {"pageObjectId": "slide_one", "size": {"width": emu, "height": emu},
       "transform": {"scaleX": 1, "scaleY": 1, "translateX": 100, "translateY": 200, "unit": "EMU"}}
requests = [
    {"deleteObject": {"objectId": first["slides"][0]["objectId"]}},
    {"createSlide": {"objectId": "slide_one", "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
    {"updatePageProperties": {"objectId": "slide_one", "pageProperties": {"pageBackgroundFill": {
        "solidFill": {"color": {"rgbColor": {"red": 1}}}}}, "fields": "pageBackgroundFill"}},
    {"createShape": {"objectId": "title_box", "shapeType": "TEXT_BOX", "elementProperties": box}},
    {"insertText": {"objectId": "title_box", "text": "Q3 results", "insertionIndex": 0}},
    {"updateTextStyle": {"objectId": "title_box", "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 2},
                         "style": {"bold": True, "link": {"url": "https://example.com"}}, "fields": "bold,link"}},
    {"updateParagraphStyle": {"objectId": "title_box", "textRange": {"type": "ALL"},
                              "style": {"alignment": "CENTER"}, "fields": "alignment"}},
    {"updateShapeProperties": {"objectId": "title_box", "shapeProperties": {"shapeBackgroundFill": {
        "solidFill": {"color": {"rgbColor": {"blue": 1}}}}}, "fields": "shapeBackgroundFill"}},
    {"createTable": {"objectId": "numbers", "elementProperties": box, "rows": 2, "columns": 2}},
    {"insertText": {"objectId": "numbers", "cellLocation": {"rowIndex": 1, "columnIndex": 1}, "text": "42",
                    "insertionIndex": 0}},
    {"updateTableCellProperties": {"objectId": "numbers", "tableRange": {"location": {"rowIndex": 0, "columnIndex": 0},
        "rowSpan": 1, "columnSpan": 1}, "tableCellProperties": {"tableCellBackgroundFill": {
        "solidFill": {"color": {"rgbColor": {"green": 1}}}}}, "fields": "tableCellBackgroundFill"}},
    {"updateTableColumnProperties": {"objectId": "numbers", "columnIndices": [0],
        "tableColumnProperties": {"columnWidth": {"magnitude": 1000000, "unit": "EMU"}}, "fields": "columnWidth"}},
    {"createImage": {"objectId": "chart", "url": url, "elementProperties": box}},
]
replied = slides.presentations().batchUpdate(presentationId=made["id"], body={"requests": requests}).execute()
deck = slides.presentations().get(presentationId=made["id"]).execute()
say(first=first, private=private, replied=replied, deck=deck)
"""
    )
    seen = await client.heard()
    await client.finished()
    first, deck = seen["first"], seen["deck"]
    assert isinstance(first, dict) and isinstance(deck, dict)
    assert len(first["slides"]) == 1 and first["title"] == "Board Update"
    assert [e["shape"]["placeholder"]["type"] for e in first["slides"][0]["pageElements"]] == [
        "CENTERED_TITLE",
        "SUBTITLE",
    ]
    assert seen["private"] == [400, None], "an image Google could not fetch is refused"
    [slide] = deck["slides"]
    assert slide["objectId"] == "slide_one"
    title, table, image = slide["pageElements"]
    runs = [
        (t.get("startIndex", 0), t["endIndex"], t["textRun"]["content"])
        for t in title["shape"]["text"]["textElements"]
        if "textRun" in t
    ]
    assert runs == [(0, 2, "Q3"), (2, 11, " results\n")]
    assert title["shape"]["text"]["textElements"][1]["textRun"]["style"] == {
        "bold": True,
        "link": {"url": "https://example.com"},
    }
    assert title["shape"]["text"]["textElements"][0]["paragraphMarker"]["style"] == {"alignment": "CENTER"}
    assert title["transform"]["translateY"] == 200
    cell = table["table"]["tableRows"][1]["tableCells"][1]
    assert cell["text"]["textElements"][1]["textRun"]["content"] == "42\n"
    assert table["table"]["tableColumns"][0]["columnWidth"]["magnitude"] == 1000000
    assert image["image"]["sourceUrl"].startswith("https://drive.google.com/uc?id=")
    replied = seen["replied"]
    assert isinstance(replied, dict)
    assert [r for r in replied["replies"] if r] == [
        {"createSlide": {"objectId": "slide_one"}},
        {"createShape": {"objectId": "title_box"}},
        {"createTable": {"objectId": "numbers"}},
        {"createImage": {"objectId": "chart"}},
    ]
