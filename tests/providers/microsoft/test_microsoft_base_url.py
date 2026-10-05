"""Graph and SharePoint by base URL: every URL Graph answers with that is meant to be called — a page's
`@odata.nextLink`, `delta`'s `@odata.deltaLink`, a content read's 302, an item's pre-authenticated
`@microsoft.graph.downloadUrl`, an upload session's `uploadUrl` — leads back through the proxy, and works."""

from __future__ import annotations

import io

import docx
import httpx

from minutehand.adapters.providers.microsoft import docx as word
from minutehand.adapters.proxy.base_url import base_url
from tests.providers.microsoft.tenant import Intercepted, Tenant, bearer


def paragraphs(content: bytes) -> list[str]:
    return [p.text for p in docx.Document(io.BytesIO(content)).paragraphs]


async def test_every_url_graph_answers_with_leads_back_through_the_base_url(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    login = base_url(microsoft.proxy.url, "login.microsoftonline.com")
    graph = f"{base_url(microsoft.proxy.url, 'graph.microsoft.com')}/v1.0"
    sharepoint = base_url(microsoft.proxy.url, tenant.directory.sharepoint_host)
    async with httpx.AsyncClient(trust_env=False) as http:
        signed = await http.post(
            f"{login}/{tenant.directory.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": tenant.directory.bot_app_id,
                "client_secret": tenant.directory.bot_app_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        assert signed.status_code == 200, signed.text
        auth = bearer(signed.json()["access_token"])
        site = (await http.get(f"{graph}/sites", params={"search": "*"}, headers=auth)).json()["value"][0]
        drive = (await http.get(f"{graph}/sites/{site['id']}/drives", headers=auth)).json()["value"][0]["id"]

        delta = (await http.get(f"{graph}/drives/{drive}/root/delta", headers=auth)).json()
        assert delta["@odata.deltaLink"].startswith(f"{graph}/drives/{drive}/root/delta?")
        assert (await http.get(delta["@odata.deltaLink"], headers=auth)).status_code == 200

        listed = await http.get(f"{graph}/drives/{drive}/items/root/children", params={"$top": "1"}, headers=auth)
        following = listed.json()["@odata.nextLink"]
        assert following.startswith(f"{graph}/drives/{drive}/items/"), following
        assert len((await http.get(following, headers=auth)).json()["value"]) == 1

        plans = (await http.get(f"{graph}/drives/{drive}/root:/Plans:/children", headers=auth)).json()["value"]
        plan = next(i for i in plans if i["name"] == "Vendor Plan.docx")
        assert plan["@microsoft.graph.downloadUrl"].startswith(f"{sharepoint}/")
        assert paragraphs((await http.get(plan["@microsoft.graph.downloadUrl"])).content)[0] == "Three vendors remain."

        redirected = await http.get(f"{graph}/drives/{drive}/items/{plan['id']}/content", headers=auth)
        assert redirected.status_code == 302 and redirected.headers["location"].startswith(f"{sharepoint}/")

        session = await http.post(
            f"{graph}/drives/{drive}/items/{plan['id']}/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            headers=auth,
        )
        upload = session.json()["uploadUrl"]
        assert upload.startswith(f"{sharepoint}/"), upload
        new_version = word.build("Two vendors remain.")
        done = await http.put(
            upload, content=new_version, headers={"Content-Range": f"bytes 0-{len(new_version) - 1}/{len(new_version)}"}
        )
        assert done.status_code in (200, 201), done.text
        again = await http.get(
            f"{graph}/drives/{drive}/items/{plan['id']}/content", headers=auth, follow_redirects=True
        )
    assert paragraphs(again.content) == ["Two vendors remain."]
    hosts = {c.exchange.host for c in tenant.store.calls()}
    assert hosts == {"login.microsoftonline.com", "graph.microsoft.com", tenant.directory.sharepoint_host}
