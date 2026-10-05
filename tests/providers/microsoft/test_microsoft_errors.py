"""What a Microsoft client sees when the fake cannot answer as Microsoft would: an operation Graph or the connector
has and the fake does not is a 501, and Minutehand's own failure a 500, both in Graph's `{"error": {"code",
"message"}}`, so `httpx` (what the services call Graph and the connector with) raises the error it raises for any
failure, with the message intact where a Graph client reads it. A faithful refusal carries no mark of Minutehand's,
and one the scenario armed (a file held open) is recorded as an injected fault."""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.domain.errors import AnswerKind
from minutehand.domain.scenario import Scenario
from tests.providers.microsoft.tenant import (
    CONNECTOR,
    GRAPH,
    SCENARIO,
    Intercepted,
    Tenant,
    bearer,
    seeded,
    token,
)


def _failed(response: httpx.Response) -> tuple[str, str]:
    """The error httpx raises for the answer, and the code and message Graph's body carries."""
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    body = raised.value.response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}, body
    return body["error"]["code"], body["error"]["message"]


async def test_a_graph_resource_the_fake_does_not_serve_is_refused_501_naming_the_operation(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Deliberately lists the tenant's groups: Graph has `GET /groups`, the fake does not."""
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        response = await http.get(f"{GRAPH}/groups", headers=auth)
    assert response.status_code == 501
    assert response.headers["x-minutehand-answer"] == "not_implemented"
    code, message = _failed(response)
    assert code == "minutehand_not_implemented"
    assert "does not implement this operation: GET graph.microsoft.com/v1.0/groups" in message
    [call] = [c for c in tenant.store.calls() if c.exchange.path == "/v1.0/groups"]
    assert call.exchange.answer is AnswerKind.NOT_IMPLEMENTED


async def test_a_connector_method_the_fake_does_not_serve_is_refused_501_naming_the_closest(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Deliberately lists the bot's conversations: the connector has `GET /v3/conversations`, the fake only creates
    one there."""
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
        response = await http.get(f"{CONNECTOR}v3/conversations", headers=auth)
    assert response.status_code == 501
    assert response.headers["x-minutehand-answer"] == "not_implemented"
    _, message = _failed(response)
    assert "does not implement this operation: GET smba.trafficmanager.net/teams/v3/conversations" in message
    assert "The closest it has is POST /{region}/v3/conversations." in message


async def test_a_write_to_a_held_file_is_refused_as_an_injected_fault_and_a_missing_item_as_refused(
    tmp_path: Path,
) -> None:
    """Deliberately renames a file a person holds open (423), and reads an item that is not there (404)."""
    held = Scenario.model_validate(
        {
            **SCENARIO.model_dump(),
            "provider_seeds": [
                {"provider": "microsoft", "body": {"holds": [{"document": "Vendor Plan", "by": "sofia"}]}}
            ],
        }
    )
    tenant = seeded(tmp_path / "held.db", held)
    plan = tenant.world.seeded("Vendor Plan")
    stored = tenant.world.item(plan) if plan is not None else None
    assert plan is not None and stored is not None
    drive = stored.item.parentReference.driveId
    async with Proxy(Routing(Registry.installed()), tenant.store, tenant.clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(tenant.store, tenant.clock, {"microsoft": tenant.provider.app(tenant.store, tenant.clock)})
        trust = ssl.create_default_context(cafile=str(proxy.ca_bundle))
        async with httpx.AsyncClient(proxy=proxy.url, verify=trust, trust_env=False) as http:
            auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
            locked = await http.patch(f"{GRAPH}/drives/{drive}/items/{plan}", json={"name": "Other.docx"}, headers=auth)
            missing = await http.get(f"{GRAPH}/drives/{drive}/items/01NOPE", headers=auth)
    assert locked.status_code == 423 and "x-minutehand-answer" not in locked.headers
    assert locked.json()["error"]["code"] == "resourceLocked" and "innerError" in locked.json()["error"]
    assert missing.status_code == 404 and "x-minutehand-answer" not in missing.headers
    answers = {c.exchange.path: c.exchange.answer for c in tenant.store.calls()}
    assert answers[f"/v1.0/drives/{drive}/items/{plan}"] is AnswerKind.INJECTED_FAULT
    assert answers[f"/v1.0/drives/{drive}/items/01NOPE"] is AnswerKind.REFUSED


async def test_minutehands_own_failure_is_a_500_in_graphs_shape(tmp_path: Path) -> None:
    """Deliberately closes the store under the app, so the first read of the world fails."""
    tenant = seeded(tmp_path / "world.db")
    app = tenant.provider.app(tenant.store, tenant.clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        tenant.store.close()
        response = await http.get(f"{GRAPH}/users", headers=auth)
    assert response.status_code == 500
    assert response.headers["x-minutehand-answer"] == "internal_error"
    code, message = _failed(response)
    assert code == "minutehand_internal_error"
    assert message.startswith("minutehand internal error while answering microsoft GET /v1.0/users: ")
    assert "ProgrammingError" in message
