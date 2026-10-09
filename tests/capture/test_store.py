"""A host declared `store`, through the proxy, with a client configured only by the handed-out environment: what
the agent writes is kept as sent and read back unchanged, and nothing leaves the machine. Then a host a provider
claims, where a call the provider does not serve falls through to the declaration, or without one is refused by
name."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand.adapters.proxy.capture import Capturing
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.outbound import described, outbound_uses
from minutehand.application.run_clock import RunClock
from minutehand.domain.outbound import (
    Answer,
    Collection,
    DeclaredStore,
    IdFormat,
    ItemId,
    Listing,
    Route,
    Stamp,
    StampFormat,
    StampOn,
)
from minutehand.domain.world import AnsweredBy, CallOutcome, CaptureMode, EntityKind, Operation, StoredSnapshot
from tests.capture.support import V6, Call, by_environment
from tests.proxy.upstream import Authority, model_api

CONTACTS = Collection(
    path="/v1/contacts",
    id=ItemId(at="id", format=IdFormat.PREFIXED, prefix="ct_"),
    stamps=[Stamp(at="created_at"), Stamp(at="meta.updated", on=StampOn.WRITE, format=StampFormat.EPOCH_SECONDS)],
    listing=Listing(
        items_at="results",
        envelope={"object": "list"},
        limit_param="limit",
        cursor_param="after",
        next_at="paging.next.after",
    ),
)
JSON_SENT = {"content-type": "application/json", "authorization": "Bearer nobody-checks-this"}


def _proxy(
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    *declared: DeclaredStore,
) -> Proxy:
    return Proxy(
        Routing(registry),
        store,
        clock,
        confdir=tmp_path / "ca",
        upstream_ca=authority.ca_cert,
        capturing=Capturing(declared),
    )


async def test_a_stored_contact_reads_back_as_sent_lists_patches_and_deletes_and_never_leaves_the_machine(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    clock.begin_wake()
    declared = DeclaredStore(
        host=V6,
        name="crm",
        collections=[CONTACTS],
        routes=[Route(method="POST", path="/v1/contacts/search", answer=Answer(json_body={"total": 0}))],
        answer=Answer(status=404, json_body={"message": "no such route"}),
    )
    # Fields named like credentials are data like any other: kept and returned verbatim.
    ana = {
        "name": "Ana Ruiz",
        "email": "ana@example.com",
        "tags": ["vip", "lisbon"],
        "score": 1.5,
        "notes": None,
        "token": "tok-ana-1",
        "password": "hunter2",
        "portal": {"api_key": "k-123", "secret": "s-456"},
    }
    ben = {"name": "Ben Ode", "email": "ben@example.com"}
    async with (
        model_api(authority, host=V6) as real,
        _proxy(registry, store, clock, tmp_path, authority, declared) as proxy,
    ):
        base = f"https://[::1]:{real.port}/v1/contacts"
        created, created_ben = await by_environment(
            proxy,
            [Call("POST", base, json.dumps(ana), JSON_SENT), Call("POST", base, json.dumps(ben), JSON_SENT)],
        )
        first, second = json.loads(created.body)["id"], json.loads(created_ben.body)["id"]
        clock.jump(clock.now().replace(hour=11))
        got, page_one, page_two, patched, deleted, gone, searched, unknown, after_all = await by_environment(
            proxy,
            [
                Call("GET", f"{base}/{first}"),
                Call("GET", f"{base}?limit=1"),
                Call("GET", f"{base}?limit=1&after={first}"),
                Call(
                    "PATCH", f"{base}/{first}", json.dumps({"email": "ana@new.example", "id": "ct_forged"}), JSON_SENT
                ),
                Call("DELETE", f"{base}/{second}"),
                Call("GET", f"{base}/{second}"),
                Call("POST", f"{base}/search", "{}", JSON_SENT),
                Call("GET", f"https://[::1]:{real.port}/v2/elsewhere"),
                Call("GET", base),
            ],
        )

    assert real.received == []  # nothing reached the real host: every call was answered here
    assert created.status == 201 and created.header("content-type") == "application/json"
    # Kept as sent, with only what the collection says the API assigns added.
    assert json.loads(created.body) == {
        **ana,
        "id": first,
        "created_at": "2026-08-24T10:00:00Z",
        "meta": {"updated": 1787565600},
    }
    assert first.startswith("ct_") and first != second
    assert (got.status, got.body) == (200, created.body)  # read back exactly as it was answered on create
    assert json.loads(page_one.body) == {
        "object": "list",
        "results": [json.loads(created.body)],
        "paging": {"next": {"after": first}},
    }
    assert json.loads(page_two.body) == {"object": "list", "results": [json.loads(created_ben.body)]}
    # A patch merges what it sends; the id and the creation stamp stay the API's, the write stamp moves.
    assert patched.status == 200
    assert json.loads(patched.body) == {
        **json.loads(created.body),
        "email": "ana@new.example",
        "meta": {"updated": 1787569200},
    }
    assert (deleted.status, deleted.body, gone.status) == (204, "", 404)
    assert json.loads(searched.body) == {"total": 0}  # a declared route answers before the collection
    assert (unknown.status, json.loads(unknown.body)) == (404, {"message": "no such route"})
    assert [c["id"] for c in json.loads(after_all.body)["results"]] == [first]

    writes = [e for e in store.events() if e.entity.kind is EntityKind.STORED]
    assert [(e.operation, e.entity.provider, e.entity.external_id) for e in writes] == [
        (Operation.CREATE, "crm", f"/v1/contacts/{first}"),
        (Operation.CREATE, "crm", f"/v1/contacts/{second}"),
        (Operation.UPDATE, "crm", f"/v1/contacts/{first}"),
        (Operation.DELETE, "crm", f"/v1/contacts/{second}"),
    ]
    snapshot = writes[2].after
    assert isinstance(snapshot, StoredSnapshot) and (snapshot.host, snapshot.collection) == (V6, "contacts")
    assert snapshot.item == patched.body
    assert snapshot.item is not None
    stored_ana = json.loads(snapshot.item)
    assert (stored_ana["token"], stored_ana["password"], stored_ana["portal"]) == (
        "tok-ana-1",
        "hunter2",
        {"api_key": "k-123", "secret": "s-456"},
    )
    calls = store.calls()
    assert all(c.exchange.captured is not None for c in calls)
    assert {c.exchange.captured.mode for c in calls if c.exchange.captured} == {CaptureMode.STORE}
    assert [u.calls for u in outbound_uses(calls)] == [11]
    assert described(outbound_uses(calls)[0]) == f"{V6}: 11 calls, kept and read back as declared, never sent"


async def test_a_sent_id_is_kept_and_a_second_create_with_it_is_refused(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    clock.begin_wake()
    declared = DeclaredStore(
        host=V6,
        collections=[Collection(path="/v1/teams/{team}/members", id=ItemId(at="key", format=IdFormat.SENT))],
    )
    async with (
        model_api(authority, host=V6) as real,
        _proxy(registry, store, clock, tmp_path, authority, declared) as proxy,
    ):
        base = f"https://[::1]:{real.port}/v1/teams"
        sent = '{"key": "ana", "role": "lead"}'
        made, again, other_team, without, listed = await by_environment(
            proxy,
            [
                Call("POST", f"{base}/red/members", sent, JSON_SENT),
                Call("POST", f"{base}/red/members", sent, JSON_SENT),
                Call("POST", f"{base}/blue/members", sent, JSON_SENT),
                Call("POST", f"{base}/red/members", '{"role": "none"}', JSON_SENT),
                Call("GET", f"{base}/red/members"),
            ],
        )
    assert (made.status, json.loads(made.body)) == (201, {"key": "ana", "role": "lead"})
    assert again.status == 409 and other_team.status == 201  # each team is a collection of its own
    assert without.status == 400 and "its own id at key" in without.body
    assert json.loads(listed.body) == [{"key": "ana", "role": "lead"}]
    refused = [c for c in store.calls() if c.exchange.status >= 400]
    assert [c.exchange.captured.note for c in refused if c.exchange.captured] == [
        "a members item ana is already stored at /v1/teams/red/members",
        "a members item is created with its own id at key",
    ]


@pytest.mark.parametrize(
    ("collections", "refusal"),
    [
        ([{"path": "/v1/contacts/{id}"}], "names a segment twice, or `{id}`"),
        ([{"path": "v1/contacts"}], "is not `/segment/...`"),
        ([{"path": "/{who}"}], "has no literal segment"),
        ([{"path": "/v1/contacts", "listing": {"next_at": "next"}}], "a bare list has no envelope"),
        ([{"path": "/v1/contacts", "listing": {"items_at": "r", "next_at": "n"}}], "reads the next page by"),
        ([{"path": "/v1/contacts", "id": {"prefix": "c_"}}], "goes with `format: prefixed`"),
        ([{"path": "/v1/contacts", "stamps": [{"at": "id"}]}], "id is assigned twice"),
        ([{"path": "/v1/contacts"}, {"path": "/v2/contacts"}], "collection name declared twice: contacts"),
        ([], "at least 1 item"),
    ],
)
def test_a_store_declaration_that_cannot_be_read_one_way_is_refused(
    collections: list[dict[str, object]], refusal: str
) -> None:
    with pytest.raises(ValidationError, match=r".") as refused:
        DeclaredStore.model_validate({"host": "api.crm.example", "kind": "store", "collections": collections})
    assert refusal in str(refused.value)


def test_a_store_declaring_redact_is_refused_naming_the_removed_key() -> None:
    with pytest.raises(ValidationError, match="`redact` was removed from `store`"):
        DeclaredStore.model_validate(
            {"host": V6, "kind": "store", "collections": [{"path": "/v1/contacts"}], "redact": ["token"]}
        )


# -- a host a provider claims -------------------------------------------------------------------------------------

REMINDERS = DeclaredStore(host="slack.com", name="slack_extra", collections=[Collection(path="/api/reminders.add")])


@pytest.fixture
def with_slack() -> Registry:
    found = Registry()
    found.discover("minutehand.adapters.providers")
    return found


async def test_a_method_slack_does_not_serve_falls_through_to_the_declared_store_and_what_it_serves_does_not(
    with_slack: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    clock.begin_wake()
    async with _proxy(with_slack, store, clock, tmp_path, authority, REMINDERS) as proxy:
        added, listed, served, unknown = await by_environment(
            proxy,
            [
                Call("POST", "https://slack.com/api/reminders.add", '{"text": "call Sofia"}', JSON_SENT),
                Call("GET", "https://slack.com/api/reminders.add"),
                Call("POST", "https://slack.com/api/auth.test", "", {"authorization": "Bearer xoxb-unknown"}),
                Call("POST", "https://slack.com/api/foo.bar"),
            ],
        )
    reminder = json.loads(added.body)
    assert added.status == 201 and set(reminder) == {"text", "id"} and reminder["text"] == "call Sofia"
    assert json.loads(listed.body) == [reminder]
    assert served.status == 200 and json.loads(served.body)["ok"] is True  # no credential is ever refused
    # A name Slack lists nowhere is Slack's own `unknown_method`, never handed to the declaration.
    assert json.loads(unknown.body) == {"ok": False, "error": "unknown_method", "req_method": "foo.bar"}
    kept, read, answered, refused = store.calls()
    for call in (kept, read):
        captured = call.exchange.captured
        assert call.provider is None and captured is not None
        assert (captured.mode, captured.answered_by, captured.not_served_by) == (
            CaptureMode.STORE,
            AnsweredBy.DECLARATION,
            "slack",
        )
    assert (answered.provider, answered.exchange.captured) == ("slack", None)  # Slack served it
    assert (refused.provider, refused.exchange.captured) == ("slack", None)
    [item] = [e for e in store.events() if e.entity.kind is EntityKind.STORED]
    assert item.entity.provider == "slack_extra" and isinstance(item.after, StoredSnapshot)
    [use] = outbound_uses(store.calls())
    assert described(use).endswith("; 2 not served by slack, answered as declared")


async def test_a_method_slack_does_not_serve_without_a_declaration_is_refused_by_name(
    with_slack: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    clock.begin_wake()
    async with _proxy(with_slack, store, clock, tmp_path, authority) as proxy:
        [refused] = await by_environment(
            proxy, [Call("POST", "https://slack.com/api/reminders.add", '{"text": "call Sofia"}', JSON_SENT)]
        )
    assert refused.status == 501
    said = json.loads(refused.body)
    assert said["error"] == "not_implemented"
    assert said["response_metadata"]["messages"] == [
        "minutehand's slack fake does not implement POST /api/reminders.add: reminders.add, a Slack Web API method this fake does not serve"
    ]
    [call] = store.calls()
    assert (call.provider, call.exchange.outcome, call.exchange.captured) == (
        "slack",
        CallOutcome.NOT_IMPLEMENTED,
        None,
    )


COLORS = DeclaredStore(
    host="www.googleapis.com", name="calendar_extra", collections=[Collection(path="/calendar/v3/colors")]
)


async def test_a_method_google_workspace_refuses_in_its_own_envelope_still_falls_through_to_the_declaration(
    with_slack: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    """Google Workspace renders its own 501 for a Calendar method it does not serve (`colors.get`); that refusal
    is `NotServed` too, so a declaration for the host answers it instead, and without one Google's envelope stands."""
    clock.begin_wake()
    colors = Call("GET", "https://www.googleapis.com/calendar/v3/colors", "", JSON_SENT)
    async with _proxy(with_slack, store, clock, tmp_path, authority, COLORS) as proxy:
        [declared] = await by_environment(proxy, [colors])
    assert declared.status == 200 and json.loads(declared.body) == []
    [call] = store.calls()
    captured = call.exchange.captured
    assert captured is not None and (captured.answered_by, captured.not_served_by) == (
        AnsweredBy.DECLARATION,
        "google_workspace",
    )


async def test_a_method_google_workspace_does_not_serve_without_a_declaration_is_refused_in_googles_envelope(
    with_slack: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    clock.begin_wake()
    async with _proxy(with_slack, store, clock, tmp_path, authority) as proxy:
        [refused] = await by_environment(
            proxy, [Call("GET", "https://www.googleapis.com/calendar/v3/colors", "", JSON_SENT)]
        )
    assert refused.status == 501
    error = json.loads(refused.body)["error"]
    assert (error["status"], error["errors"][0]["reason"]) == ("UNIMPLEMENTED", "notImplemented")
    [call] = store.calls()
    assert (call.provider, call.exchange.outcome) == ("google_workspace", CallOutcome.NOT_IMPLEMENTED)
