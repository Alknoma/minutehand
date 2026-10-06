"""The three modes, end to end through the proxy, with a client configured only by the handed-out environment:
an acknowledged send never leaves the machine, a passed-through lookup reaches the real host, and both are kept
with the run; an undeclared host is still refused unless the run captures unknown hosts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from minutehand.adapters.proxy.capture import Capturing, write_recordings
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.outbound import described, outbound_uses, suggested
from minutehand.application.run_clock import RunClock
from minutehand.domain.outbound import Acknowledge, Answer, PassThrough, Route, UnknownHosts
from minutehand.domain.world import AnsweredBy, BodyKept, CaptureMode
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.model import ModelFailed
from tests.capture.support import (
    SECRET_API_KEY,
    SECRET_BODY,
    SECRET_COOKIE,
    SECRET_DECLARED,
    SECRET_HEADER,
    SECRET_QUERY,
    SECRETS,
    START,
    V6,
    Call,
    by_environment,
    stored_bytes,
)
from tests.proxy.support import client
from tests.proxy.upstream import Answer as UpstreamAnswer
from tests.proxy.upstream import Authority, model_api

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _proxy(
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    *declared: Acknowledge | PassThrough,
    capture_unknown: UnknownHosts = UnknownHosts.REFUSE,
    model: LanguageModel | None = None,
) -> Proxy:
    return Proxy(
        Routing(registry),
        store,
        clock,
        confdir=tmp_path / "ca",
        upstream_ca=authority.ca_cert,
        capturing=Capturing(declared),
        capture_unknown=capture_unknown,
        model=model,
    )


async def test_an_acknowledged_send_never_leaves_the_machine_and_is_answered_as_declared(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    clock.begin_wake()
    declared = Acknowledge(
        host=V6,
        answer=Answer(status=202, json_body={"id": "queued"}),
        routes=[Route(method="POST", path="/v3/batch/*", answer=Answer(status=201, text="batched"))],
    )
    async with (
        model_api(authority, host=V6) as real,
        _proxy(registry, store, clock, tmp_path, authority, declared) as proxy,
    ):
        sent, batched, other = await by_environment(
            proxy,
            [
                Call(
                    "POST",
                    f"https://[::1]:{real.port}/v3/mail/send",
                    '{"to": "sofia@example.com"}',
                    {"content-type": "application/json"},
                ),
                Call("POST", f"https://[::1]:{real.port}/v3/batch/7", "{}"),
                Call("GET", f"https://[::1]:{real.port}/v3/batch/7"),
            ],
        )
    # A real server listened on that address the whole time, and heard nothing.
    assert real.received == []
    assert (sent.status, json.loads(sent.body), sent.header("content-type")) == (
        202,
        {"id": "queued"},
        "application/json",
    )
    assert (batched.status, batched.body, batched.header("content-type")) == (201, "batched", "text/plain")
    assert (other.status, json.loads(other.body)) == (202, {"id": "queued"})  # the route names POST only
    first, _, _ = store.calls()
    assert (first.provider, first.wake, first.sim_time, first.refused) == (None, 1, START, False)
    exchange = first.exchange
    assert (exchange.method, exchange.host, exchange.path, exchange.status) == ("POST", V6, "/v3/mail/send", 202)
    assert exchange.request_body == '{"to": "sofia@example.com"}' and exchange.response_body == '{"id": "queued"}'
    captured = exchange.captured
    assert captured is not None
    assert (captured.mode, captured.answered_by, captured.declared_as) == (
        CaptureMode.ACKNOWLEDGE,
        AnsweredBy.DECLARATION,
        V6,
    )
    assert captured.started <= captured.ended and captured.request.kept is BodyKept.WHOLE


async def test_a_passed_through_lookup_reaches_the_real_host_and_both_sides_are_kept(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    async with (
        model_api(authority, host=V6) as real,
        _proxy(registry, store, clock, tmp_path, authority, PassThrough(host=V6)) as proxy,
    ):
        [found] = await by_environment(
            proxy,
            [Call("GET", f"https://[::1]:{real.port}/search?q=partner+pricing", headers={"traceparent": TRACEPARENT})],
        )
    assert (found.status, json.loads(found.body)) == (200, {"received": 1})
    assert [(r.method, r.path) for r in real.received] == [("GET", "/search?q=partner+pricing")]
    [call] = store.calls()
    exchange = call.exchange
    assert (exchange.path, exchange.status, exchange.traceparent) == ("/search?q=partner+pricing", 200, TRACEPARENT)
    assert exchange.response_body == '{"received": 1}'
    assert exchange.captured is not None
    assert (exchange.captured.mode, exchange.captured.answered_by) == (CaptureMode.PASS_THROUGH, AnsweredBy.REAL_HOST)
    assert exchange.captured.response.content_type == "application/json"


async def test_a_streamed_pass_through_answer_reaches_the_agent_as_a_stream_and_is_kept_whole(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    hold = asyncio.Event()
    chunks = [b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"]
    answer = UpstreamAnswer("text/event-stream", chunks, hold=hold)
    async with (
        model_api(authority, answer) as real,
        _proxy(registry, store, clock, tmp_path, authority, PassThrough(host="localhost")) as proxy,
        client(proxy, proxy.ca_cert) as http,
        http.stream("GET", f"https://localhost:{real.port}/feed") as response,
    ):
        raw = response.aiter_raw()
        # The real host holds the rest back until this arrives: a proxy that buffered would never deliver it.
        first = await asyncio.wait_for(anext(raw), timeout=10)
        assert store.calls() == []
        hold.set()
        rest = b"".join([chunk async for chunk in raw])
    assert first + rest == b"".join(chunks)
    [call] = store.calls()
    assert call.exchange.response_body == "data: one\n\ndata: two\n\ndata: three\n\n"
    assert call.exchange.captured is not None and call.exchange.captured.streamed


async def test_an_undeclared_host_is_still_refused_without_being_contacted(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    async with (
        model_api(authority, host=V6) as real,
        _proxy(registry, store, clock, tmp_path, authority, Acknowledge(host="api.mail.test")) as proxy,
    ):
        [refused] = await by_environment(proxy, [Call("GET", f"https://[::1]:{real.port}/search?q=x")])
    assert refused.status == 502 and json.loads(refused.body)["error"] == "no provider claims this host"
    assert real.received == []
    [call] = store.calls()
    assert call.refused and call.exchange.captured is None


async def test_capture_unknown_passes_an_undeclared_host_through_and_keeps_it(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    async with (
        model_api(authority, host=V6) as real,
        _proxy(registry, store, clock, tmp_path, authority, capture_unknown=UnknownHosts.ALL) as proxy,
    ):
        [found] = await by_environment(proxy, [Call("GET", f"https://[::1]:{real.port}/search?q=x")])
    assert found.status == 200 and len(real.received) == 1
    [call] = store.calls()
    assert not call.refused and call.exchange.captured is not None
    assert (call.exchange.captured.mode, call.exchange.captured.declared_as) == (CaptureMode.DISCOVERED, None)


async def test_capturing_unknown_reads_passes_a_get_through_and_a_post_to_the_same_host_is_refused(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    async with (
        model_api(authority, host=V6) as real,
        _proxy(registry, store, clock, tmp_path, authority, capture_unknown=UnknownHosts.READS) as proxy,
    ):
        read, write = await by_environment(
            proxy,
            [
                Call("GET", f"https://[::1]:{real.port}/search?q=x"),
                Call("POST", f"https://[::1]:{real.port}/charges", json.dumps({"amount": 500})),
            ],
        )
    assert (read.status, write.status) == (200, 502)
    assert len(real.received) == 1, "the write never reached the real host"
    kept, refused = store.calls()
    assert kept.exchange.captured is not None and kept.exchange.captured.mode is CaptureMode.DISCOVERED
    assert refused.refused and refused.exchange.method == "POST"


async def test_no_secret_reaches_the_store_from_headers_query_or_bodies(
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    world_dir: Path,
) -> None:
    """The bytes of the world file, its write-ahead log and the recordings file beside them are searched for every
    secret the call carried, in the Authorization header, a cookie, an API-key header, the query string, a
    credential field of the body, and a field only the declaration names; and in the real host's answer."""
    answer_json = {"access_token": SECRET_BODY, "meta": {"signature": SECRET_DECLARED}, "result": "found"}
    answer = UpstreamAnswer("application/json", [json.dumps(answer_json).encode()])
    declared = PassThrough(host=V6, redact=["meta.signature"])
    async with (
        model_api(authority, answer, host=V6) as real,
        _proxy(registry, store, clock, tmp_path, authority, declared) as proxy,
    ):
        [found] = await by_environment(
            proxy,
            [
                Call(
                    "POST",
                    f"https://[::1]:{real.port}/search?q=pricing&api_key={SECRET_QUERY}&key={SECRET_QUERY}",
                    json.dumps({"q": "pricing", "api_key": SECRET_BODY, "meta": {"signature": SECRET_DECLARED}}),
                    {
                        "content-type": "application/json",
                        "authorization": f"Bearer {SECRET_HEADER}",
                        "cookie": f"session={SECRET_COOKIE}",
                        "x-api-key": SECRET_API_KEY,
                    },
                )
            ],
        )
    # Sent on as the agent sent it: the real host still needs its credentials.
    assert json.loads(found.body) == answer_json
    assert real.headers[0]["authorization"] == f"Bearer {SECRET_HEADER}" and SECRET_QUERY in real.received[0].path
    write_recordings(world_dir, store.calls())
    store.close()
    kept = stored_bytes(world_dir)
    assert b"pricing" in kept and b"found" in kept  # what is not a secret is kept
    assert [s for s in SECRETS if s.encode() in kept] == []


async def test_a_body_past_the_limit_is_cut_and_a_binary_one_is_kept_as_its_bytes_or_past_the_limit_its_length(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    declared = Acknowledge(host="api.mail.test", body_limit=40)
    long_text = json.dumps({"text": "x" * 200})
    async with _proxy(registry, store, clock, tmp_path, authority, declared) as proxy:
        await by_environment(
            proxy,
            [
                Call("POST", "https://api.mail.test/send", long_text, {"content-type": "application/json"}),
                Call("POST", "https://api.mail.test/attach", "\x00\x01binary", {"content-type": "image/png"}),
                Call("POST", "https://api.mail.test/attach", "\x00" * 100, {"content-type": "image/png"}),
            ],
        )
    cut, binary, too_long = store.calls()
    assert cut.exchange.captured is not None and binary.exchange.captured is not None
    assert cut.exchange.request_body == long_text[:40]
    assert (cut.exchange.captured.request.kept, cut.exchange.captured.request.size) == (
        BodyKept.TRUNCATED,
        len(long_text),
    )
    assert binary.exchange.request_body is None and binary.exchange.request_bytes == b"\x00\x01binary"
    assert (binary.exchange.captured.request.kept, binary.exchange.captured.request.content_type) == (
        BodyKept.BYTES,
        "image/png",
    )
    assert binary.exchange.captured.request.size == 8
    assert too_long.exchange.captured is not None and too_long.exchange.request_bytes is None
    assert (too_long.exchange.captured.request.kept, too_long.exchange.captured.request.size) == (BodyKept.BINARY, 100)


@pytest.mark.parametrize(
    ("flags", "means"),
    [
        ([], UnknownHosts.REFUSE),
        (["--capture-unknown"], UnknownHosts.ALL),
        (["--capture-unknown", "reads"], UnknownHosts.READS),
    ],
)
def test_the_capture_unknown_flag_says_which_calls_pass(flags: list[str], means: UnknownHosts) -> None:
    from minutehand.cli import _parser

    args = _parser().parse_args(["run", "scenario.yaml", "--agent", "agent.yaml", *flags])
    assert UnknownHosts(args.capture_unknown) is means


class PaymentsModel:
    """Stands in for the model port: answers as a payments API would, from the history it is shown."""

    model_id = "stand-in"

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.shown: list[str] = []

    async def answer(self, system, messages, answer, *, model=None, temperature=None):
        asked = messages[-1].text
        self.shown.append(asked)
        if self.fails:
            raise ModelFailed("the model answered twice with something that is not the answer asked for")
        if asked.rstrip().split("The request to answer now:\n", 1)[1].startswith("POST /charges"):
            return answer(status=201, body=json.dumps({"id": "ch_1", "amount": 500}))
        listed = (
            [{"id": "ch_1", "amount": 500}] if "POST /charges" in asked.split("The request to answer now:")[0] else []
        )
        return answer(status=200, body=json.dumps({"data": listed}))


async def test_a_model_answers_writes_to_an_undeclared_host_and_every_call_after_from_what_it_answered(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    stand_in = PaymentsModel()
    async with (
        model_api(authority, host=V6) as real,
        _proxy(
            registry, store, clock, tmp_path, authority, capture_unknown=UnknownHosts.MODEL, model=stand_in
        ) as proxy,
    ):
        before, made, after = await by_environment(
            proxy,
            [
                Call("GET", f"https://[::1]:{real.port}/charges"),
                Call("POST", f"https://[::1]:{real.port}/charges", json.dumps({"amount": 500})),
                Call("GET", f"https://[::1]:{real.port}/charges"),
            ],
        )
    assert before.status == 200 and len(real.received) == 1, "only the read before any write reached the real host"
    assert (made.status, after.status) == (201, 200) and json.loads(after.body)["data"][0]["id"] == "ch_1"
    modes = [(c.exchange.captured.mode, c.exchange.captured.answered_by) for c in store.calls() if c.exchange.captured]
    assert modes == [
        (CaptureMode.DISCOVERED, AnsweredBy.REAL_HOST),
        (CaptureMode.MODELED, AnsweredBy.MODEL),
        (CaptureMode.MODELED, AnsweredBy.MODEL),
    ]
    assert "(none: the service is empty)" in stand_in.shown[0] and '"amount": 500' in stand_in.shown[1]
    modeled_use = [u for u in outbound_uses(store.calls()) if u.mode is CaptureMode.MODELED]
    assert [described(u) for u in modeled_use] == [
        "::1: 2 calls, answered by a model standing in for it (--capture-unknown model), never sent"
    ]
    assert "host: ::1" in suggested(outbound_uses(store.calls()))


async def test_a_model_that_fails_is_answered_502_and_nothing_is_sent_is_refused(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    async with (
        model_api(authority, host=V6) as real,
        _proxy(
            registry,
            store,
            clock,
            tmp_path,
            authority,
            capture_unknown=UnknownHosts.MODEL,
            model=PaymentsModel(fails=True),
        ) as proxy,
    ):
        [made] = await by_environment(proxy, [Call("POST", f"https://[::1]:{real.port}/charges", "{}")])
    assert made.status == 502 and real.received == []
    [call] = store.calls()
    assert call.exchange.captured is not None and call.exchange.captured.answered_by is AnsweredBy.REFUSAL


async def test_capturing_unknown_by_model_with_no_model_configured_is_refused(tmp_path: Path) -> None:
    from minutehand import session
    from minutehand.application.refusals import RunRefused
    from minutehand.domain.agent import AgentUnderTest, Reported
    from minutehand.domain.scenario import Person, Scenario, Silent

    scn = Scenario.model_validate(
        {
            "name": "modeled",
            "goal": "g",
            "owner": "owen",
            "starts_at": "2026-08-24T09:00:00Z",
            "people": [Person(key="owen", name="Owen", email="owen@example.com", reply=Silent())],
        }
    )
    agent = AgentUnderTest(
        name="a", wakes=[Reported(wake_url="http://127.0.0.1:9/w", report_url="http://127.0.0.1:9/r")]
    )
    with pytest.raises(RunRefused, match="--capture-unknown model needs a model"):
        await session.play(
            scn, agent, state=tmp_path / "state", listen=session.Listen(capture_unknown=UnknownHosts.MODEL), model=None
        )
    assert not (tmp_path / "state").exists() or not any((tmp_path / "state").iterdir())
