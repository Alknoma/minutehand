"""The control API of `minutehand serve`, under `/v1`: open and close worlds, read them, act in them as a person,
move their clocks, arm faults, and run the checks. Every body is a model of `wire`; see docs/serve.md.

    GET    /v1/health
    GET    /v1/ca.pem                                  the CA bundle a service trusts
    GET    /v1/environment[?ca_path=P][&no_proxy=H]... the variables a service needs (`Environment`)
    GET    /v1/worlds                                  `WorldList`
    POST   /v1/worlds                                  `CreateWorld` -> 201 `WorldView`
    GET    /v1/worlds/{id}                             `WorldView`
    DELETE /v1/worlds/{id}[?quiet=false][&quiet_for=S][&quiet_at_most=S]
                                                close it once it is quiet (`Quiet`, its defaults unless the
                                                query says otherwise): `Checked`, as it stood when closed
    POST   /v1/worlds/{id}/quiet                       `Quiet` -> `Quieted`: wait until the world goes quiet
    GET    /v1/worlds/{id}/events?provider&kind&actor&operation&since&since_reset    `EventsPage`
    GET    /v1/worlds/{id}/entities?provider&kind      `EntitiesPage`: each entity's latest version
    GET    /v1/worlds/{id}/calls[?unmatched=true][?captured=true][?tunnelled=true][?since_reset=false]
                                                `CallsPage`: every call; those refused because nobody claims or
                                                declares their host; those captured (`Exchange.captured`); or
                                                those relayed unopened on a tunnel (`Exchange.tunnelled`)
    GET    /v1/worlds/{id}/spans[?since_reset=false]   `SpansPage`

`events`, `calls` and `spans` read since the world's last reset; `since_reset=false` reads its whole record, every
stretch before each reset first, and says in `resets` where each reset falls.
    POST   /v1/worlds/{id}/act                         `ActRequest` -> `Acted`
    GET    /v1/worlds/{id}/clock                       `WorldView` (its `now` and `owed`)
    POST   /v1/worlds/{id}/clock                       `Advance` -> `Advanced`
    POST   /v1/worlds/{id}/faults                      `Fault` -> `WorldView`
    POST   /v1/worlds/{id}/provider-faults             `DeclareFaults` -> `WorldView`: a provider's own typed faults
    POST   /v1/worlds/{id}/seed                        `FurtherSeed` -> `Seeded`: more seeded into the open world
    POST   /v1/worlds/{id}/people                      `ChangePerson` -> `Acted`: an account removed, deactivated...
    POST   /v1/worlds/{id}/permissions                 `Permit` -> `Acted`: a named permission granted or withheld
    POST   /v1/worlds/{id}/inbound-credential          `MintInbound` -> `Minted`: headers for a request a test builds
    POST   /v1/worlds/{id}/reset                       -> `WorldView`: back to its seed, same id and claims
    GET    /v1/worlds/{id}/state?provider=P            `RawState`: every version of every entity (unstable)
    GET    /v1/worlds/{id}/checks                      `Checked`
    GET    /v1/providers                               `ProvidersView`: what each provider can do while open
    GET    /v1/unmatched?since=N[&late_for=W][&kind=K]... `Unmatched`: the lobby; by default only `unclaimed` calls
                                                (no world claimed them, nothing declared them); `kind` names others
                                                (`model_host`, `pass_through`); `late_for`: those for world W after
                                                it closed

A refusal is `Refusal`: 404 for a world that is not open, 409 for what a world cannot do (with `kind`
`unsupported` when the provider cannot do it in any world), 422 for a body that is not the model or a query
parameter that is not what its route takes, 502 when the service an event was pushed to refused it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.control.wire import (
    API,
    QUIET_AT_MOST,
    QUIET_FOR,
    Acted,
    ActRequest,
    Advance,
    Advanced,
    CallsPage,
    ChangePerson,
    Checked,
    CreateWorld,
    DeclareFaults,
    DeleteTicket,
    EditTicket,
    EntitiesPage,
    Environment,
    EventsPage,
    Fault,
    FiredView,
    FurtherSeed,
    Happen,
    LobbyKind,
    Minted,
    MintInbound,
    MoveTicket,
    OwedView,
    Permit,
    PressControl,
    ProvidersView,
    Quiet,
    RawEntity,
    RawState,
    Refusal,
    RefusalKind,
    Reply,
    Say,
    Seeded,
    SpansPage,
    Unmatched,
    WorldList,
    WorldView,
    lobby_kind,
)
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.application.standing import Unsupported
from minutehand.domain.scenario import Model
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation, RecordedCall, Stored, WorldEvent
from minutehand.ports.store import Store
from minutehand.session import reading_file

if TYPE_CHECKING:
    from minutehand.serve import Serving, World

Handler = Callable[[Request], Awaitable[Response]]


def _json(model: Model, status: int = 200) -> Response:
    return Response(model.model_dump_json(), status_code=status, media_type="application/json")


def _refused(status: int, error: str, kind: RefusalKind | None = None) -> Response:
    return _json(Refusal(error=error, kind=kind), status)


def _guarded(handler: Handler) -> Handler:
    async def guarded(request: Request) -> Response:
        try:
            return await handler(request)
        except ValidationError as e:
            return _refused(422, str(e))
        except _BadQuery as e:
            return _refused(422, str(e))
        except LookupError as e:
            return _refused(404, str(e.args[0]) if e.args else str(e))
        except AgentFailed as e:
            return _refused(502, str(e))
        except Unsupported as e:
            return _refused(409, str(e), RefusalKind.UNSUPPORTED)
        except (RunRefused, ValueError) as e:
            return _refused(409, str(e))

    return guarded


def _view(world: World) -> WorldView:
    standing = world.standing
    return WorldView(
        world_id=world.world_id,
        name=world.name,
        open=world.open,
        claims=world.spec.claims,
        now=standing.clock.now(),
        head=world.store.head(),
        owed=[OwedView(at=at, what=what) for at, what in standing.owed()],
        resets=world.resets,
    )


def _query(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


class _BadQuery(Exception):
    """A query parameter that is not what its route takes: refused 422, as a body that is not the model is."""


def _flag(request: Request, name: str, *, default: bool) -> bool:
    """A `true`/`false` query parameter; anything else is refused."""
    given = _query(request, name)
    if given is None:
        return default
    if given not in ("true", "false"):
        raise _BadQuery(f"?{name}= is true or false, not {given!r}")
    return given == "true"


def _count(request: Request, name: str) -> int:
    """A query parameter that is a whole number at least 0; 0 when it is not given."""
    given = _query(request, name)
    if given is None:
        return 0
    if not given.isdigit():
        raise _BadQuery(f"?{name}= is a whole number at least 0, not {given!r}")
    return int(given)


def _member[E: StrEnum](request: Request, name: str, kind: type[E]) -> E | None:
    """A query parameter that names a member of `kind`, or None when it is not given."""
    given = _query(request, name)
    if given is None:
        return None
    try:
        return kind(given)
    except ValueError:
        raise _BadQuery(f"?{name}= is one of {', '.join(m.value for m in kind)}, not {given!r}") from None


def _seconds(request: Request, name: str, default: timedelta) -> timedelta:
    """A query parameter in seconds, a number at least 0."""
    given = _query(request, name)
    if given is None:
        return default
    try:
        found = float(given)
    except ValueError:
        raise _BadQuery(f"?{name}= is a number of seconds, not {given!r}") from None
    if found < 0:
        raise _BadQuery(f"?{name}= is at least 0 seconds, not {given}")
    return timedelta(seconds=found)


def _across[T](stretches: Sequence[Path], world: World, read: Callable[[Store], list[T]]) -> tuple[list[T], list[int]]:
    """What `read` finds in each stretch of the world's record before a reset, oldest first, then in the world as
    it stands; and, for each reset, the index of the first item after it."""
    found: list[T] = []
    resets: list[int] = []
    for path in stretches:
        with reading_file(path, world.world_id) as before:
            found += read(before)
        resets.append(len(found))
    return found + read(world.store), resets


def create_app(serving: Serving) -> Starlette:
    standing = serving.standing

    def world_of(request: Request) -> World:
        return standing.get(request.path_params["world_id"])

    def earlier(request: Request, world: World) -> list[Path]:
        """The stretches of the world's record before its resets, when the read asks for them (`since_reset=false`)."""
        return [] if _flag(request, "since_reset", default=True) else standing.stretches(world.world_id)

    def provider_of(request: Request, world: World) -> str | None:
        """The provider a read names, seeded into the world first if nothing has called it yet: the world a
        service would find on its first call is the world a test reads. A name an outbound declaration of the
        world records its sends under is read as it is: nothing seeds a captured host."""
        provider = _query(request, "provider")
        captured = {d.key for d in world.mounted.capturing.declared}
        if provider is not None and provider not in captured:
            world.standing.provider(standing.installed(provider))
        return provider

    async def health(_: Request) -> Response:
        return Response("ok", media_type="text/plain")

    async def ca(_: Request) -> Response:
        return Response(serving.proxy.ca_bundle.read_bytes(), media_type="application/x-pem-file")

    async def environment(request: Request) -> Response:
        direct = request.query_params.getlist("no_proxy")
        return _json(Environment(variables=serving.environment(_query(request, "ca_path"), direct)))

    async def worlds(_: Request) -> Response:
        return _json(WorldList(worlds=[_view(w) for w in standing.worlds.values()]))

    async def create(request: Request) -> Response:
        spec = CreateWorld.model_validate_json(await request.body())
        await standing.start_emulators(spec)
        return _json(_view(standing.create(spec)), 201)

    async def world(request: Request) -> Response:
        return _json(_view(world_of(request)))

    async def close(request: Request) -> Response:
        found = world_of(request)
        quiet: Quiet | None = None
        if _flag(request, "quiet", default=True):
            quiet = Quiet(
                quiet_for=_seconds(request, "quiet_for", QUIET_FOR),
                at_most=_seconds(request, "quiet_at_most", QUIET_AT_MOST),
            )
        result, quieted = await standing.close(found.world_id, quiet=quiet)
        return _json(Checked(result=result, quiet=quieted))

    async def quiet(request: Request) -> Response:
        found = world_of(request)
        body = await request.body()
        asked = Quiet.model_validate_json(body) if body.strip() else Quiet()
        return _json(await standing.quiet(found.world_id, asked))

    async def events(request: Request) -> Response:
        found = world_of(request)
        since = _count(request, "since")
        provider = provider_of(request, found)
        wanted_kind = _member(request, "kind", EntityKind)
        wanted_actor = _member(request, "actor", Actor)
        wanted_operation = _member(request, "operation", Operation)

        def keep(e: WorldEvent) -> bool:
            return (
                (provider is None or e.entity.provider == provider)
                and (wanted_kind is None or e.entity.kind is wanted_kind)
                and (wanted_actor is None or e.actor is wanted_actor)
                and (wanted_operation is None or e.operation is wanted_operation)
            )

        def read(store: Store) -> list[WorldEvent]:
            return [e for e in store.events(since=since if store is found.store else 0) if keep(e)]

        found_events, resets = _across(earlier(request, found), found, read)
        return _json(EventsPage(events=found_events, head=found.store.head(), resets=resets))

    async def entities(request: Request) -> Response:
        found = world_of(request)
        provider = provider_of(request, found)
        wanted_kind = _member(request, "kind", EntityKind)
        refs: dict[EntityRef, None] = {}
        for event in found.store.events():
            ref = event.entity
            if (provider is None or ref.provider == provider) and (wanted_kind is None or ref.kind is wanted_kind):
                refs[ref] = None
        current: list[Stored] = [s for s in (found.store.get(r) for r in refs) if s is not None]
        return _json(EntitiesPage(entities=current))

    async def calls(request: Request) -> Response:
        found = world_of(request)
        unmatched_only = _query(request, "unmatched") == "true"
        captured_only = _query(request, "captured") == "true"
        tunnelled_only = _query(request, "tunnelled") == "true"

        def read(store: Store) -> list[RecordedCall]:
            return [
                c
                for c in store.calls()
                if (not unmatched_only or c.refused)
                and (not captured_only or c.exchange.captured is not None)
                and (not tunnelled_only or c.exchange.tunnelled is not None)
            ]

        recorded, resets = _across(earlier(request, found), found, read)
        return _json(CallsPage(calls=recorded, resets=resets))

    async def spans(request: Request) -> Response:
        found = world_of(request)
        kept, resets = _across(earlier(request, found), found, lambda store: store.spans())
        return _json(SpansPage(spans=kept, resets=resets))

    async def act(request: Request) -> Response:
        found = world_of(request)
        asked = ActRequest.model_validate_json(await request.body()).act
        live = found.standing
        if isinstance(asked, Say):
            event = await live.say(asked.person, asked.text, provider=asked.provider)
        elif isinstance(asked, Reply):
            event = await live.reply(asked.person, asked.text, to=asked.to)
        elif isinstance(asked, MoveTicket):
            event = live.move_ticket(asked.ticket, asked.to)
        elif isinstance(asked, EditTicket):
            event = live.edit_ticket(asked.ticket, state=asked.state, assignee=asked.assignee)
        elif isinstance(asked, Happen):
            event = await live.happen_now(asked.happening)
        elif isinstance(asked, DeleteTicket):
            event = live.delete_ticket(asked.ticket)
        else:
            assert isinstance(asked, PressControl)
            event = await live.press(asked.person, asked.on, asked.press)
        return _json(Acted(event=event))

    async def further_seed(request: Request) -> Response:
        found = world_of(request)
        added = FurtherSeed.model_validate_json(await request.body())
        written = standing.extend(found.world_id, added)
        return _json(Seeded(view=_view(found), written=written))

    async def people(request: Request) -> Response:
        found = world_of(request)
        asked = ChangePerson.model_validate_json(await request.body())
        live = found.standing
        return _json(Acted(event=live.change_person(standing.installed(asked.provider), asked.person, asked.change)))

    async def permissions(request: Request) -> Response:
        found = world_of(request)
        asked = Permit.model_validate_json(await request.body())
        return _json(Acted(event=found.standing.permit(standing.installed(asked.provider), asked.grant)))

    async def inbound_credential(request: Request) -> Response:
        found = world_of(request)
        asked = MintInbound.model_validate_json(await request.body())
        minted = found.standing.credential(standing.installed(asked.provider), asked.ask)
        return _json(Minted(credential=minted))

    async def reset(request: Request) -> Response:
        return _json(_view(standing.reset(world_of(request).world_id)))

    async def state(request: Request) -> Response:
        found = world_of(request)
        provider = provider_of(request, found)
        if provider is None:
            return _refused(422, "name the provider whose state to read: ?provider=")
        refs: dict[EntityRef, None] = {}
        for event in found.store.events():
            if event.entity.provider == provider and event.operation not in (Operation.READ, Operation.SEARCH):
                refs[event.entity] = None
        return _json(
            RawState(
                provider=provider,
                entities=[
                    RawEntity(entity=r, deleted=found.store.get(r) is None, versions=found.store.versions(r))
                    for r in refs
                ],
            )
        )

    async def providers(_: Request) -> Response:
        return _json(ProvidersView(providers=standing.capabilities()))

    async def advance(request: Request) -> Response:
        found = world_of(request)
        asked = Advance.model_validate_json(await request.body())
        live = found.standing
        if asked.to is not None:
            to = asked.to
        else:
            assert asked.by is not None
            to = live.clock.now() + asked.by
        fired = await live.advance(to)
        return _json(
            Advanced(now=live.clock.now(), fired=[FiredView(at=f.at, what=f.what, events=f.events) for f in fired])
        )

    async def faults(request: Request) -> Response:
        found = world_of(request)
        standing.arm(found.world_id, Fault.model_validate_json(await request.body()))
        return _json(_view(found))

    async def declare(request: Request) -> Response:
        found = world_of(request)
        asked = DeclareFaults.model_validate_json(await request.body())
        found.standing.declare_faults(standing.installed(asked.provider), asked.seed)
        return _json(_view(found))

    async def checks(request: Request) -> Response:
        return _json(Checked(result=await world_of(request).standing.checks(stop=None)))

    async def unmatched(request: Request) -> Response:
        since = _count(request, "since")
        late_for = _query(request, "late_for")
        asked = request.query_params.getlist("kind")
        wanted: set[LobbyKind] = set()
        for given in asked:
            try:
                wanted.add(LobbyKind(given))
            except ValueError:
                raise _BadQuery(f"?kind= is one of {', '.join(k.value for k in LobbyKind)}, not {given!r}") from None
        wanted = wanted or {LobbyKind.UNCLAIMED}
        recorded = standing.lobby_store.calls()
        kept = [
            c
            for c in recorded[since:]
            if not standing.shared(c.exchange.host) and (late_for is None or c.exchange.late_for == late_for)
        ]
        kinds = {k: sum(1 for c in kept if lobby_kind(c) is k) for k in LobbyKind}
        listed = [c for c in kept if lobby_kind(c) in wanted]
        return _json(Unmatched(calls=listed, head=len(recorded), kinds=kinds))

    def route(path: str, handler: Handler, methods: list[str]) -> Route:
        return Route(f"{API}{path}", _guarded(handler), methods=methods)

    return Starlette(
        routes=[
            route("/health", health, ["GET"]),
            route("/ca.pem", ca, ["GET"]),
            route("/environment", environment, ["GET"]),
            route("/worlds", worlds, ["GET"]),
            route("/worlds", create, ["POST"]),
            route("/worlds/{world_id}", world, ["GET"]),
            route("/worlds/{world_id}", close, ["DELETE"]),
            route("/worlds/{world_id}/quiet", quiet, ["POST"]),
            route("/worlds/{world_id}/events", events, ["GET"]),
            route("/worlds/{world_id}/entities", entities, ["GET"]),
            route("/worlds/{world_id}/calls", calls, ["GET"]),
            route("/worlds/{world_id}/spans", spans, ["GET"]),
            route("/worlds/{world_id}/act", act, ["POST"]),
            route("/worlds/{world_id}/clock", world, ["GET"]),
            route("/worlds/{world_id}/clock", advance, ["POST"]),
            route("/worlds/{world_id}/faults", faults, ["POST"]),
            route("/worlds/{world_id}/provider-faults", declare, ["POST"]),
            route("/worlds/{world_id}/seed", further_seed, ["POST"]),
            route("/worlds/{world_id}/people", people, ["POST"]),
            route("/worlds/{world_id}/permissions", permissions, ["POST"]),
            route("/worlds/{world_id}/inbound-credential", inbound_credential, ["POST"]),
            route("/worlds/{world_id}/reset", reset, ["POST"]),
            route("/worlds/{world_id}/state", state, ["GET"]),
            route("/worlds/{world_id}/checks", checks, ["GET"]),
            route("/providers", providers, ["GET"]),
            route("/unmatched", unmatched, ["GET"]),
        ]
    )
