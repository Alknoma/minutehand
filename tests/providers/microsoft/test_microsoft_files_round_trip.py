"""A document's whole life in SharePoint, through the proxy with plain `httpx`: signed in, the site and its library
found, the folder listed, a Word file read with `python-docx` from the bytes behind its 302, a new version uploaded
by session, renamed by a person, the rename reported by `delta` and by a subscription's notification, deleted, and
reported gone by `delta`."""

from __future__ import annotations

import io

import docx

from minutehand.adapters.providers.microsoft import docx as word
from minutehand.domain.world import Actor, DocumentSnapshot, Operation
from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, Webhook, bearer, token


def paragraphs(content: bytes) -> list[str]:
    return [p.text for p in docx.Document(io.BytesIO(content)).paragraphs]


async def test_document_read_versioned_renamed_notified_and_deleted(
    tenant: Tenant, microsoft: Intercepted, webhook: Webhook
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        sites = (await http.get(f"{GRAPH}/sites", params={"search": "*"}, headers=auth)).json()
        site = sites["value"][0]
        drives = (await http.get(f"{GRAPH}/sites/{site['id']}/drives", headers=auth)).json()["value"]
        assert [d["driveType"] for d in drives] == ["documentLibrary"]
        drive = drives[0]["id"]

        subscribed = await http.post(
            f"{GRAPH}/subscriptions",
            json={
                "changeType": "updated",
                "notificationUrl": webhook.url,
                "resource": f"/drives/{drive}/root",
                "expirationDateTime": tenant.clock.now().replace(day=16).isoformat().replace("+00:00", "Z"),
                "clientState": "the-service-secret",
            },
            headers=auth,
        )
        assert subscribed.status_code == 201, subscribed.text
        assert len(webhook.validations) == 1

        first = (await http.get(f"{GRAPH}/drives/{drive}/root/delta", headers=auth)).json()
        assert {i["name"] for i in first["value"]} >= {"root", "Plans", "Vendor Plan.docx", "notes.txt"}
        cursor = first["@odata.deltaLink"]

        listed = await http.get(
            f"{GRAPH}/drives/{drive}/items/root/children",
            params={"$select": "id,name,folder,file,size,webUrl,parentReference"},
            headers=auth,
        )
        assert listed.status_code == 200, listed.text
        plans = next(i for i in listed.json()["value"] if i["name"] == "Plans")
        inside = (await http.get(f"{GRAPH}/drives/{drive}/items/{plans['id']}/children", headers=auth)).json()
        plan = inside["value"][0]
        assert plan["file"]["mimeType"] == word.DOCX

        redirected = await http.get(f"{GRAPH}/drives/{drive}/items/{plan['id']}/content", headers=auth)
        assert redirected.status_code == 302
        assert redirected.headers["location"].startswith(f"https://{tenant.directory.sharepoint_host}/")
        downloaded = await http.get(redirected.headers["location"])
        assert paragraphs(downloaded.content) == ["Three vendors remain.", "Prices due Friday."]

        new_version = word.build("Two vendors remain.\nPrices agreed.")
        session = await http.post(
            f"{GRAPH}/drives/{drive}/items/{plan['id']}/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            headers=auth,
        )
        upload = session.json()["uploadUrl"]
        half = len(new_version) // 2
        partial = await http.put(
            upload,
            content=new_version[:half],
            headers={"Content-Range": f"bytes 0-{half - 1}/{len(new_version)}"},
        )
        assert partial.status_code == 202 and partial.json()["nextExpectedRanges"] == [f"{half}-"]
        done = await http.put(
            upload,
            content=new_version[half:],
            headers={"Content-Range": f"bytes {half}-{len(new_version) - 1}/{len(new_version)}"},
        )
        assert done.status_code == 200, done.text
        again = await http.get(
            f"{GRAPH}/drives/{drive}/items/{plan['id']}/content", headers=auth, follow_redirects=True
        )
        assert paragraphs(again.content) == ["Two vendors remain.", "Prices agreed."]

        before = len(webhook.notifications)
        renamed = await tenant.provider.rename_file(
            "sofia", plan["id"], tenant.store, tenant.clock, name="Vendor Plan (final).docx"
        )
        assert (renamed.actor, renamed.operation) == (Actor.PERSON, Operation.UPDATE)
        assert isinstance(renamed.after, DocumentSnapshot) and renamed.after.title == "Vendor Plan (final).docx"
        [notice] = webhook.notifications[before:]
        assert notice["value"][0]["clientState"] == "the-service-secret"
        assert notice["value"][0]["subscriptionId"] == subscribed.json()["id"]

        changes = (await http.get(cursor, headers=auth)).json()
        assert [(i["id"], i["name"]) for i in changes["value"]] == [(plan["id"], "Vendor Plan (final).docx")]
        cursor = changes["@odata.deltaLink"]

        deleted = await http.delete(f"{GRAPH}/drives/{drive}/items/{plan['id']}", headers=auth)
        assert deleted.status_code == 204
        gone = (await http.get(cursor, headers=auth)).json()["value"]
        assert [(i["id"], "deleted" in i) for i in gone] == [(plan["id"], True)]
        missing = await http.get(f"{GRAPH}/drives/{drive}/items/{plan['id']}", headers=auth)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "itemNotFound"
