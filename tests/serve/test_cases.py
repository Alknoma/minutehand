"""Worlds opened under one case label are one run: stepped together, scored once, listed once, read as one timeline
with one set of people; the model traffic their services make belongs to the case, not to whichever world declared
the model host.

Before, a harness that opened a messaging, a documents and a tracker world for one case got three unrelated runs,
each scored on a third of what happened, and hundreds of tunnelled model calls listed as one provider world's
outbound calls."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from minutehand import session
from minutehand.adapters.control.wire import Claims, CreateWorld, ModelHost
from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import RunsResponse
from minutehand.application.cases import CASE
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor, EntityKind, Operation, TicketState
from minutehand.serve import ServeOptions
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import open_case
from tests.proxy.upstream import Authority, make_authority, model_api
from tests.serve.support import Served, dm, spec

MODEL_HOST = "model.localhost"


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "upstream-ca")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def served(state: Path, authority: Authority) -> Iterator[Served]:
    options = ServeOptions(
        proxy_port=0, control_port=0, telemetry_port=0, receive_telemetry=False, upstream_ca=authority.ca_cert
    )
    with serve_in_background(state, options) as url, MinutehandClient(url) as client:
        yield Served(url=url, client=client, environment=client.environment())


def _tracker(token: str) -> CreateWorld:
    """A tracker world whose seed names Sofia by another key, with the same email: one person in the case."""
    seed = Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "people": [
                {"key": "sofia_r", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
                {"key": "nadia", "name": "Nadia Okafor", "email": "nadia@example.com", "reply": {"kind": "silent"}},
            ],
            "tickets": [{"provider": "asana", "project": "Launch", "title": "Book the venue", "assignee": "sofia_r"}],
            "provider_seeds": [{"provider": "asana", "body": {"tokens": [{"token": token}]}}],
        }
    )
    return CreateWorld(seed=seed, claims=Claims(tokens=[token]))


def test_worlds_under_one_label_are_one_case_with_one_timeline_one_person_and_one_verdict(
    served: Served, state: Path
) -> None:
    client = served.client
    case = open_case(client, "launch week", [spec("xoxb-case-one"), _tracker("asana-case-one")])
    alone = client.create_world(spec("xoxb-case-alone"))
    messaging, tracker = case.worlds
    assert alone.case_id is None and messaging.case_id == tracker.case_id == case.case_id

    with case.step(at=messaging.view.now, reason="first look"):
        channel = dm(served, "xoxb-case-one", "sofia@example.com")
        served.slack("xoxb-case-one").chat_postMessage(channel=channel, text="Could you book the venue, Sofia?")
    case.advance(timedelta(hours=2))
    with case.step(reason="second look"):
        ticket = next(s.entity for s in tracker.entities(provider="asana", kind=EntityKind.TICKET))
        tracker.move_ticket(ticket, TicketState.DONE)

    by_world = messaging.checks().result
    by_case = case.checks().result
    assert by_world == by_case, "a world of a case is checked as its case"
    assert [w.case_id for w in client.worlds() if w.world_id != alone.world_id] == [case.case_id] * 2
    assert client.case(case.case_id).steps.step == 2
    closed = case.close()
    client.close_world(alone.world_id)

    card = closed.result.effectiveness
    assert card.wakes == 2 and card.waits_opened == 1 and card.messages_to_people == 1
    assert [b.person for b in card.burden] == ["owen", "sofia", "nadia"], "Sofia is one person in both worlds"
    assert closed.result.verdict.kind is VerdictKind.UNFINISHED, closed.result.verdict.words

    outcome = session.load(state, case.case_id)
    assert outcome.record.worlds == [messaging.world_id, tracker.world_id]
    assert outcome.result.verdict == closed.result.verdict
    with session.reading(state, case.case_id) as world:
        events = world.events()
    changes = [(e.actor, e.entity.provider, e.operation, e.wake) for e in events if e.actor is not Actor.SCENARIO]
    assert (Actor.AGENT, "slack", Operation.CREATE, 1) in changes
    assert (Actor.PERSON, "asana", Operation.UPDATE, 2) in changes
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [e.sim_time for e in events] == sorted(e.sim_time for e in events)

    listed = [o.record.run_id for o in session.listed(state)]
    assert case.case_id in listed and messaging.world_id not in listed and tracker.world_id not in listed
    assert alone.world_id not in listed, "a world no call reached is a probe, not a run"
    with TestClient(create_app(state)) as viewer:
        rows = RunsResponse.model_validate_json(viewer.get("/api/runs").content).runs
    names = {r.run_id: r.case for r in rows}
    assert names == {case.case_id: "launch week"}


def test_a_label_opens_a_new_case_once_its_last_world_has_closed(served: Served, state: Path) -> None:
    first = open_case(served.client, "repeat", [spec("xoxb-case-repeat-1")])
    first.close()
    second = open_case(served.client, "repeat", [spec("xoxb-case-repeat-2")])
    second.close()
    assert first.case_id != second.case_id
    assert (state / "runs" / first.case_id / CASE).is_file() and (state / "runs" / second.case_id / CASE).is_file()


async def test_model_traffic_of_a_cases_world_is_the_cases_not_the_worlds(
    served: Served, state: Path, authority: Authority
) -> None:
    client = served.client
    declaring = spec("xoxb-case-model").model_copy(update={"model_hosts": [ModelHost(host=MODEL_HOST)]})
    case = open_case(client, "with a model", [declaring, _tracker("asana-case-model")])
    proxy = client.environment()["HTTPS_PROXY"]
    trust = ssl.create_default_context(cafile=str(authority.ca_cert))
    async with model_api(authority) as upstream:
        async with httpx.AsyncClient(proxy=proxy, verify=trust, trust_env=False) as http:
            answered = await http.post(f"https://{MODEL_HOST}:{upstream.port}/v1/chat/completions", json={})
    assert answered.status_code == 200
    assert client.calls(case.worlds[0].world_id).calls == [], "not the declaring world's own call"
    case.close()

    with session.reading(state, case.case_id) as world:
        tunnelled = [c for c in world.calls() if c.exchange.tunnelled is not None]
    assert [c.exchange.host for c in tunnelled] == [MODEL_HOST]
    for world_id in session.load(state, case.case_id).record.worlds:
        with session.reading_file(state / "runs" / world_id / "world.db", world_id) as alone:
            assert not [c for c in alone.calls() if c.exchange.tunnelled is not None]
