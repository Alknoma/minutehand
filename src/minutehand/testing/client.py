"""`MinutehandClient` and `AsyncMinutehandClient`: the control API of `minutehand serve`, typed both ways with
the server's own models (`minutehand.adapters.control.wire`). A refusal raises `Refused` with its status and
the server's words."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

import httpx

from minutehand.adapters.control.wire import (
    API,
    Act,
    Acted,
    ActRequest,
    Advance,
    Advanced,
    CallsPage,
    ChangePerson,
    Checked,
    CreateWorld,
    DeclareFaults,
    EntitiesPage,
    Environment,
    EventsPage,
    Fault,
    FurtherSeed,
    Minted,
    MintInbound,
    Permit,
    ProvidersView,
    ProviderView,
    RawState,
    Refusal,
    RefusalKind,
    Seeded,
    SpansPage,
    Unmatched,
    WorldList,
    WorldView,
)
from minutehand.domain.scenario import Model
from minutehand.domain.world import Actor, EntityKind, Operation

TIMEOUT = 30.0


class Refused(Exception):
    """The server refused a request: 404 no such open world, 409 what the world cannot do, 422 a body that is
    not the model, 502 the service an event was pushed to refused it."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(f"{status}: {error}")
        self.status = status
        self.error = error


def _query(
    *,
    provider: str | None = None,
    kind: EntityKind | None = None,
    actor: Actor | None = None,
    operation: Operation | None = None,
    since: int | None = None,
    since_reset: bool = True,
) -> dict[str, str]:
    found: dict[str, str] = {} if since_reset else {"since_reset": "false"}
    if provider is not None:
        found["provider"] = provider
    if kind is not None:
        found["kind"] = kind.value
    if actor is not None:
        found["actor"] = actor.value
    if operation is not None:
        found["operation"] = operation.value
    if since is not None:
        found["since"] = str(since)
    return found


class Unsupported(Refused):
    """The provider cannot do what was asked in any world: a capability it does not have (`GET /v1/providers` says
    which it has)."""


def _read[M: Model](answered: httpx.Response, model: type[M]) -> M:
    if answered.is_success:
        return model.model_validate_json(answered.content)
    try:
        refusal = Refusal.model_validate_json(answered.content)
    except ValueError:
        raise Refused(answered.status_code, answered.text) from None
    if refusal.kind is RefusalKind.UNSUPPORTED:
        raise Unsupported(answered.status_code, refusal.error)
    raise Refused(answered.status_code, refusal.error)


def _environment_query(ca_path: str | None, no_proxy: Sequence[str]) -> httpx.QueryParams:
    found: list[tuple[str, str | int | float | bool | None]] = [("ca_path", ca_path)] if ca_path is not None else []
    return httpx.QueryParams([*found, *(("no_proxy", host) for host in no_proxy)])


def _advance(by: timedelta | None, to: datetime | None) -> str:
    return Advance(by=by, to=to).model_dump_json(exclude_none=True)


def _calls_query(
    *, unmatched: bool, captured: bool, tunnelled: bool = False, since_reset: bool = True
) -> dict[str, str] | None:
    """`GET /calls`'s filters: refused calls, captured calls, calls on tunnels relayed unopened, or with none of
    them every call; since the world's last reset, or, with `since_reset` False, across its whole record."""
    wanted_by = (("unmatched", unmatched), ("captured", captured), ("tunnelled", tunnelled))
    asked = {name: "true" for name, wanted in wanted_by if wanted}
    if not since_reset:
        asked["since_reset"] = "false"
    return asked or None


def _spans_query(since_reset: bool) -> dict[str, str] | None:
    return None if since_reset else {"since_reset": "false"}


class MinutehandClient:
    """Synchronous. `url` is the control API's root, e.g. `http://minutehand:8081`."""

    def __init__(self, url: str, *, timeout: float = TIMEOUT) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(base_url=self.url + API, timeout=timeout, trust_env=False)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MinutehandClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get[M: Model](self, path: str, model: type[M], params: Mapping[str, str] | None = None) -> M:
        return _read(self._http.get(path, params=params), model)

    def _post[M: Model](self, path: str, body: str, model: type[M]) -> M:
        return _read(self._http.post(path, content=body, headers={"content-type": "application/json"}), model)

    def health(self) -> bool:
        return self._http.get("/health").is_success

    def ca(self) -> bytes:
        answered = self._http.get("/ca.pem")
        answered.raise_for_status()
        return answered.content

    def environment(self, ca_path: str | None = None, *, no_proxy: Sequence[str] = ()) -> dict[str, str]:
        """The variables a service needs; `no_proxy` names the stack's own services, which it reaches directly."""
        return _read(
            self._http.get("/environment", params=_environment_query(ca_path, no_proxy)), Environment
        ).variables

    def create_world(self, spec: CreateWorld) -> WorldView:
        return self._post("/worlds", spec.model_dump_json(), WorldView)

    def worlds(self) -> list[WorldView]:
        return self._get("/worlds", WorldList).worlds

    def world(self, world_id: str) -> WorldView:
        return self._get(f"/worlds/{world_id}", WorldView)

    def close_world(self, world_id: str) -> Checked:
        return _read(self._http.delete(f"/worlds/{world_id}"), Checked)

    def events(
        self,
        world_id: str,
        *,
        provider: str | None = None,
        kind: EntityKind | None = None,
        actor: Actor | None = None,
        operation: Operation | None = None,
        since: int | None = None,
        since_reset: bool = True,
    ) -> EventsPage:
        """The log, filtered; since the world's last reset, or, with `since_reset` False, across its whole record
        (`EventsPage.resets` says where each reset falls)."""
        params = _query(
            provider=provider, kind=kind, actor=actor, operation=operation, since=since, since_reset=since_reset
        )
        return self._get(f"/worlds/{world_id}/events", EventsPage, params)

    def entities(self, world_id: str, *, provider: str | None = None, kind: EntityKind | None = None) -> EntitiesPage:
        return self._get(f"/worlds/{world_id}/entities", EntitiesPage, _query(provider=provider, kind=kind))

    def calls(
        self,
        world_id: str,
        *,
        unmatched: bool = False,
        captured: bool = False,
        tunnelled: bool = False,
        since_reset: bool = True,
    ) -> CallsPage:
        return self._get(
            f"/worlds/{world_id}/calls",
            CallsPage,
            _calls_query(unmatched=unmatched, captured=captured, tunnelled=tunnelled, since_reset=since_reset),
        )

    def spans(self, world_id: str, *, since_reset: bool = True) -> SpansPage:
        return self._get(f"/worlds/{world_id}/spans", SpansPage, _spans_query(since_reset))

    def act(self, world_id: str, act: Act) -> Acted:
        return self._post(f"/worlds/{world_id}/act", ActRequest(act=act).model_dump_json(), Acted)

    def advance(self, world_id: str, *, by: timedelta | None = None, to: datetime | None = None) -> Advanced:
        return self._post(f"/worlds/{world_id}/clock", _advance(by, to), Advanced)

    def arm(self, world_id: str, fault: Fault) -> WorldView:
        return self._post(f"/worlds/{world_id}/faults", fault.model_dump_json(), WorldView)

    def declare_faults(self, world_id: str, declared: DeclareFaults) -> WorldView:
        return self._post(f"/worlds/{world_id}/provider-faults", declared.model_dump_json(), WorldView)

    def further_seed(self, world_id: str, added: FurtherSeed) -> Seeded:
        return self._post(f"/worlds/{world_id}/seed", added.model_dump_json(exclude_unset=True), Seeded)

    def change_person(self, world_id: str, change: ChangePerson) -> Acted:
        return self._post(f"/worlds/{world_id}/people", change.model_dump_json(), Acted)

    def permit(self, world_id: str, permit: Permit) -> Acted:
        return self._post(f"/worlds/{world_id}/permissions", permit.model_dump_json(), Acted)

    def inbound_credential(self, world_id: str, mint: MintInbound) -> Minted:
        return self._post(f"/worlds/{world_id}/inbound-credential", mint.model_dump_json(), Minted)

    def reset(self, world_id: str) -> WorldView:
        return self._post(f"/worlds/{world_id}/reset", "{}", WorldView)

    def raw_state(self, world_id: str, provider: str) -> RawState:
        """Every version of every entity `provider` holds in the world. For a person debugging: its shape is the
        provider's own and is not stable."""
        return self._get(f"/worlds/{world_id}/state", RawState, {"provider": provider})

    def providers(self) -> list[ProviderView]:
        return self._get("/providers", ProvidersView).providers

    def checks(self, world_id: str) -> Checked:
        return self._get(f"/worlds/{world_id}/checks", Checked)

    def unmatched(self, *, since: int = 0) -> Unmatched:
        return self._get("/unmatched", Unmatched, {"since": str(since)})


class AsyncMinutehandClient:
    """The same, for an async suite."""

    def __init__(self, url: str, *, timeout: float = TIMEOUT) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.url + API, timeout=timeout, trust_env=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncMinutehandClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _get[M: Model](self, path: str, model: type[M], params: Mapping[str, str] | None = None) -> M:
        return _read(await self._http.get(path, params=params), model)

    async def _post[M: Model](self, path: str, body: str, model: type[M]) -> M:
        return _read(await self._http.post(path, content=body, headers={"content-type": "application/json"}), model)

    async def environment(self, ca_path: str | None = None, *, no_proxy: Sequence[str] = ()) -> dict[str, str]:
        answered = await self._http.get("/environment", params=_environment_query(ca_path, no_proxy))
        return _read(answered, Environment).variables

    async def create_world(self, spec: CreateWorld) -> WorldView:
        return await self._post("/worlds", spec.model_dump_json(), WorldView)

    async def worlds(self) -> list[WorldView]:
        return (await self._get("/worlds", WorldList)).worlds

    async def world(self, world_id: str) -> WorldView:
        return await self._get(f"/worlds/{world_id}", WorldView)

    async def close_world(self, world_id: str) -> Checked:
        return _read(await self._http.delete(f"/worlds/{world_id}"), Checked)

    async def events(
        self,
        world_id: str,
        *,
        provider: str | None = None,
        kind: EntityKind | None = None,
        actor: Actor | None = None,
        operation: Operation | None = None,
        since: int | None = None,
        since_reset: bool = True,
    ) -> EventsPage:
        """The log, filtered; since the world's last reset, or, with `since_reset` False, across its whole record
        (`EventsPage.resets` says where each reset falls)."""
        params = _query(
            provider=provider, kind=kind, actor=actor, operation=operation, since=since, since_reset=since_reset
        )
        return await self._get(f"/worlds/{world_id}/events", EventsPage, params)

    async def entities(
        self, world_id: str, *, provider: str | None = None, kind: EntityKind | None = None
    ) -> EntitiesPage:
        return await self._get(f"/worlds/{world_id}/entities", EntitiesPage, _query(provider=provider, kind=kind))

    async def calls(
        self,
        world_id: str,
        *,
        unmatched: bool = False,
        captured: bool = False,
        tunnelled: bool = False,
        since_reset: bool = True,
    ) -> CallsPage:
        return await self._get(
            f"/worlds/{world_id}/calls",
            CallsPage,
            _calls_query(unmatched=unmatched, captured=captured, tunnelled=tunnelled, since_reset=since_reset),
        )

    async def spans(self, world_id: str, *, since_reset: bool = True) -> SpansPage:
        return await self._get(f"/worlds/{world_id}/spans", SpansPage, _spans_query(since_reset))

    async def act(self, world_id: str, act: Act) -> Acted:
        return await self._post(f"/worlds/{world_id}/act", ActRequest(act=act).model_dump_json(), Acted)

    async def advance(self, world_id: str, *, by: timedelta | None = None, to: datetime | None = None) -> Advanced:
        return await self._post(f"/worlds/{world_id}/clock", _advance(by, to), Advanced)

    async def arm(self, world_id: str, fault: Fault) -> WorldView:
        return await self._post(f"/worlds/{world_id}/faults", fault.model_dump_json(), WorldView)

    async def declare_faults(self, world_id: str, declared: DeclareFaults) -> WorldView:
        return await self._post(f"/worlds/{world_id}/provider-faults", declared.model_dump_json(), WorldView)

    async def further_seed(self, world_id: str, added: FurtherSeed) -> Seeded:
        return await self._post(f"/worlds/{world_id}/seed", added.model_dump_json(exclude_unset=True), Seeded)

    async def change_person(self, world_id: str, change: ChangePerson) -> Acted:
        return await self._post(f"/worlds/{world_id}/people", change.model_dump_json(), Acted)

    async def permit(self, world_id: str, permit: Permit) -> Acted:
        return await self._post(f"/worlds/{world_id}/permissions", permit.model_dump_json(), Acted)

    async def inbound_credential(self, world_id: str, mint: MintInbound) -> Minted:
        return await self._post(f"/worlds/{world_id}/inbound-credential", mint.model_dump_json(), Minted)

    async def reset(self, world_id: str) -> WorldView:
        return await self._post(f"/worlds/{world_id}/reset", "{}", WorldView)

    async def raw_state(self, world_id: str, provider: str) -> RawState:
        return await self._get(f"/worlds/{world_id}/state", RawState, {"provider": provider})

    async def providers(self) -> list[ProviderView]:
        return (await self._get("/providers", ProvidersView)).providers

    async def checks(self, world_id: str) -> Checked:
        return await self._get(f"/worlds/{world_id}/checks", Checked)

    async def unmatched(self, *, since: int = 0) -> Unmatched:
        return await self._get("/unmatched", Unmatched, {"since": str(since)})
