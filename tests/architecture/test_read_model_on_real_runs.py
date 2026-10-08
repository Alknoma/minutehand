"""The read model on real runs played by the installed `minutehand run`, read back with `minutehand query`, `trace` and
`explain` and the MCP tool `query_run`: the follow-up example asking Rosa in Slack, its people written by the
recipes' stand-in model, and the reference agent on the library's `person_answers_late`, emailing, following up and
with its model calls recorded on the wire. What each view says is held against what the agent did and against the
run's own record, and every query docs/querying.md offers is run on both."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from minutehand import session
from minutehand.adapters.mcp.results import QueryAnswer
from minutehand.adapters.mcp.server import build
from minutehand.adapters.query.reader import Record, open_model, query
from minutehand.application.checkpoint import checkpoints
from minutehand.application.library import entry, write
from minutehand.domain.agent import AgentStatus
from minutehand.domain.checks import FindingKind
from minutehand.domain.prices import Price, Prices
from minutehand.domain.telemetry import SpanSource
from minutehand.domain.world import Actor, MessageSnapshot
from tests.architecture.support import MINUTEHAND, ROOT, Rig, free_port
from tests.architecture.test_library import FOLLOW_UP, REFERENCE_TEAM
from tests.mcp.test_mcp_tools import call
from tests.support.people import MODEL as PEOPLE_MODEL
from tests.support.people import people_environment

ASK = "Hi Rosa, could you confirm the venue for the team offsite, please?"
"""What the follow-up example asks Rosa (examples/follow_up/agent.py)."""
ROSA_SAYS = "The lakeside hall, booked for the 14th."
"""The one fact her scripted answer carries (examples/follow_up/scenario.yaml); the stand-in writes it as it is."""
REFERENCE_SAYS = "Yes, that is confirmed. The reference is RF-4410."
PRICES = Prices(prices=[Price(model="planner-small", input_per_million=1.0, output_per_million=4.0)])
DOCS = ROOT / "docs" / "querying.md"
QUERIES = dict(re.findall(r"^### ([^\n]+)\n\n```sql\n(.*?)```", DOCS.read_text(encoding="utf-8"), flags=re.M | re.S))


@dataclass(frozen=True)
class Played:
    state: Path
    run_id: str

    def rows(self, sql: str, prices: Prices | None = None) -> list[Record]:
        db = open_model(self.state, self.run_id, prices)
        try:
            return query(db, sql).records()
        finally:
            db.close()

    def cli(self, *args: str) -> str:
        done = subprocess.run(
            [str(MINUTEHAND), *args, "--state", str(self.state)], capture_output=True, text=True, timeout=120
        )
        assert done.returncode == 0, done.stderr
        return done.stdout


@pytest.fixture(scope="module")
def follow_up(tmp_path_factory: pytest.TempPathFactory) -> Played:
    """examples/follow_up as its README and CI run it: diligent, Rosa answering after a day and a half."""
    base = tmp_path_factory.mktemp("follow_up")
    port = free_port()
    agent = base / "agent.yaml"
    agent.write_text((FOLLOW_UP / "agent.yaml").read_text().replace("8700", str(port)))
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")} | people_environment()
    state = base / "state"
    command = ("--", sys.executable, FOLLOW_UP / "agent.py")
    done = subprocess.run(
        [*(MINUTEHAND, "run", FOLLOW_UP / "scenario.yaml", "--agent", agent, "--state", state), *command],
        env={**env, "PORT": str(port), "AGENT_BEHAVIOUR": "diligent"},
        cwd=base,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, done.stdout + done.stderr[-3000:]
    return Played(state, done.stdout.split()[1].rstrip(":"))


@pytest.fixture(scope="module")
def reference(outside: object, tmp_path_factory: pytest.TempPathFactory) -> Played:
    """The reference agent on the library's `person_answers_late`: it emails Rosa, follows up two days on, hears her
    answer on the third and tells Owen; its calls to its model API are recorded (--record-model-calls)."""
    rig = Rig(tmp_path_factory.mktemp("reference"), outside.model, outside.search, str(outside.ca))  # type: ignore[attr-defined]
    path = write(entry("person_answers_late"), REFERENCE_TEAM, rig.base, replace=False)
    done = rig.run(str(path), "--record-model-calls", env={"REFERENCE_BEHAVIOUR": "diligent"}, policy=False)
    assert done.code == 0, done.out + done.err[-3000:]
    return Played(rig.state, done.run_id)


def _agent_messages(played: Played) -> list[tuple[int, str]]:
    with session.reading(played.state, played.run_id) as world:
        return [
            (e.seq, e.after.text)
            for e in world.events()
            if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
        ]


def test_the_examples_messages_are_what_the_agent_sent_and_rosa_said(follow_up: Played) -> None:
    said = follow_up.rows("SELECT * FROM messages ORDER BY seq")
    assert [(m["seq"], m["text"]) for m in said if m["from_actor"] == "agent"] == _agent_messages(follow_up)
    ask, answer, thanks, email = said
    assert (ask["provider"], ask["to_people"], ask["text"], ask["is_ask"], ask["wake"]) == (
        "slack",
        '["rosa"]',
        ASK,
        1,
        1,
    )
    assert (answer["from_actor"], answer["from_person"], answer["text"], answer["is_reply"], answer["answers_seq"]) == (
        "person",
        "rosa",
        ROSA_SAYS,
        1,
        ask["seq"],
    )
    assert (thanks["text"], thanks["is_ask"], thanks["is_follow_up"]) == ("Thank you!", 0, 0)
    assert (email["provider"], email["to_people"], email["from_actor"]) == ("api_mail_example", '["owen"]', "agent")
    assert ROSA_SAYS in str(email["text"])


def test_the_examples_calls_carry_their_bodies_and_who_answered(follow_up: Played) -> None:
    calls = follow_up.rows("SELECT * FROM calls ORDER BY call_id")
    with session.reading(follow_up.state, follow_up.run_id) as world:
        recorded = world.calls()
    assert [(c["method"], c["host"], c["path"], c["status"]) for c in calls] == [
        (r.exchange.method, r.exchange.host, r.exchange.path, r.exchange.status) for r in recorded
    ]
    assert [c["request_body"] for c in calls] == [r.exchange.request_body for r in recorded]
    assert [c["response_body"] for c in calls] == [r.exchange.response_body for r in recorded]
    [posted] = [c for c in calls if c["path"] == "/api/chat.postMessage" and c["wake"] == 1]
    assert json.loads(str(posted["request_body"]))["text"] == ASK
    assert posted["answered_by"] == "provider"
    [mail] = [c for c in calls if c["host"] == "api.mail.example"]
    assert (mail["answered_by"], mail["capture_mode"], mail["status"]) == ("declaration", "acknowledge", 202)
    [email] = follow_up.rows("SELECT seq FROM messages WHERE provider = 'api_mail_example'")
    assert (mail["first_seq"], mail["last_seq"]) == (email["seq"], email["seq"])


def test_the_examples_wakes_dispatch_and_replies(follow_up: Played) -> None:
    assert follow_up.rows("SELECT wake, reason, woken_by, reported_status FROM wakes ORDER BY wake") == [
        {"wake": 1, "reason": "start", "woken_by": None, "reported_status": "idle"},
        {"wake": 2, "reason": "person_replied", "woken_by": "reply", "reported_status": "done"},
    ]
    assert follow_up.rows(
        "SELECT kind, source, closed, drawn_from, drawn_offset_seconds FROM dispatch ORDER BY due_id"
    ) == [
        {
            "kind": "agent_wake",
            "source": "reported",
            "closed": "cancelled",
            "drawn_from": None,
            "drawn_offset_seconds": None,
        },
        {
            "kind": "person_reply",
            "source": "reply",
            "closed": "fired",
            "drawn_from": "delay",
            "drawn_offset_seconds": 36 * 3600.0,
        },
    ]
    [ask] = follow_up.rows("SELECT seq FROM messages WHERE is_ask = 1")
    [answer] = follow_up.rows("SELECT seq FROM messages WHERE is_reply = 1")
    [reply] = follow_up.rows("SELECT * FROM replies")
    assert {k: reply[k] for k in ("person", "kind", "writing", "written_by", "model", "prompt_version", "text")} == {
        "person": "rosa",
        "kind": "message",
        "writing": "script",
        "written_by": "model",
        "model": PEOPLE_MODEL,
        "prompt_version": "person-step/1",
        "text": ROSA_SAYS,
    }
    assert (json.loads(str(reply["facts"])), reply["answers_seq"], reply["seq"], reply["landed"]) == (
        [ROSA_SAYS],
        ask["seq"],
        answer["seq"],
        1,
    )
    [written] = follow_up.rows(f"SELECT * FROM model_calls WHERE person_call_id = {reply['person_call_id']}")
    assert (written["side"], written["person"], written["wrote"], written["model"]) == (
        "person",
        "rosa",
        "reply",
        PEOPLE_MODEL,
    )


def test_the_examples_findings_are_the_runs(follow_up: Played) -> None:
    result = session.load(follow_up.state, follow_up.run_id).result
    assert follow_up.rows("SELECT check_id, kind, message, evidence FROM findings ORDER BY finding_id") == [
        {"check_id": f.check, "kind": f.kind.value, "message": f.message, "evidence": json.dumps(f.evidence)}
        for f in result.findings
    ]
    assert result.findings, "the example's expectations are each reported met"
    [row] = follow_up.rows("SELECT verdict, stop FROM run")
    assert row == {"verdict": result.verdict.kind.value, "stop": "agent_done"}


def test_the_example_read_with_the_installed_commands(follow_up: Played) -> None:
    out = follow_up.cli(
        "query", follow_up.run_id, "SELECT seq, text FROM messages WHERE is_ask = 1", "--format", "json"
    )
    [ask] = json.loads(out)
    assert ask["text"] == ASK
    traced = json.loads(follow_up.cli("trace", follow_up.run_id, "--person", "rosa", "--json"))
    assert [(a["kind"], a["summary"]) for a in traced["actions"]] == [("message", ASK), ("message", "Thank you!")]
    [answer] = follow_up.rows("SELECT seq FROM messages WHERE is_reply = 1")
    explained = json.loads(follow_up.cli("explain", follow_up.run_id, str(answer["seq"]), "--json"))
    assert (explained["wake"]["reason"], explained["answers"]["text"]) == ("person_replied", ASK)
    assert [d["source"] for d in explained["woken_by"]] == ["reply"]
    assert [r["text"] for r in explained["replies_landed"]] == [ROSA_SAYS]
    assert [a["summary"] for a in explained["after"] if a["kind"] == "message"] == ["Thank you!"]
    text = follow_up.cli("explain", follow_up.run_id, str(ask["seq"]))
    assert f"  reply: rosa at 2026-08-25T21:00:00.000Z: {ROSA_SAYS} (model, reply 1)" in text.splitlines()


async def test_the_example_read_over_mcp(follow_up: Played) -> None:
    async with create_connected_server_and_client_session(build(follow_up.state)) as client:
        found = await call(
            client,
            "query_run",
            QueryAnswer,
            run_id=follow_up.run_id,
            sql="SELECT person, written_by, text FROM replies",
        )
    assert found.rows == [["rosa", "model", ROSA_SAYS]]


def test_the_reference_agents_follow_up_answer_and_report(reference: Played) -> None:
    said = reference.rows("SELECT * FROM messages ORDER BY seq")
    assert [(m["seq"], m["text"]) for m in said if m["from_actor"] == "agent"] == _agent_messages(reference)
    [ask] = [m for m in said if m["is_ask"] == 1]
    [chase] = [m for m in said if m["is_follow_up"] == 1]
    [answer] = [m for m in said if m["is_reply"] == 1]
    assert (ask["to_people"], chase["to_people"], chase["ask_seq"], chase["wake"]) == (
        '["rosa"]',
        '["rosa"]',
        ask["seq"],
        2,
    )
    assert (answer["from_person"], answer["text"], answer["answers_seq"]) == ("rosa", REFERENCE_SAYS, ask["seq"])
    told = [m for m in said if m["to_people"] == '["owen"]' and "RF-4410" in str(m["text"])]
    assert len(told) == 1 and int(str(told[0]["seq"])) > int(str(answer["seq"]))
    assert reference.rows("SELECT wake, reason, woken_by, reported_status FROM wakes ORDER BY wake") == [
        {"wake": 1, "reason": "start", "woken_by": None, "reported_status": "idle"},
        {"wake": 2, "reason": "due", "woken_by": "reported", "reported_status": "idle"},
        {"wake": 3, "reason": "person_replied", "woken_by": "reply", "reported_status": "done"},
    ]
    assert reference.rows("SELECT source, closed FROM dispatch ORDER BY due_id") == [
        {"source": "reported", "closed": "fired"},
        {"source": "reply", "closed": "fired"},
        {"source": "reported", "closed": "cancelled"},
    ]
    [reply] = reference.rows("SELECT person, written_by, model, text, answers_seq FROM replies")
    assert reply == {
        "person": "rosa",
        "written_by": "model",
        "model": PEOPLE_MODEL,
        "text": REFERENCE_SAYS,
        "answers_seq": ask["seq"],
    }


def test_the_reference_agents_calls_and_recorded_model_calls(reference: Played) -> None:
    calls = reference.rows("SELECT * FROM calls ORDER BY call_id")
    with session.reading(reference.state, reference.run_id) as world:
        recorded = world.calls()
        spans = world.spans()
    assert [(c["host"], c["path"], c["request_body"], c["response_body"]) for c in calls] == [
        (r.exchange.host, r.exchange.path, r.exchange.request_body, r.exchange.response_body) for r in recorded
    ]
    mails = [c for c in calls if c["host"] == "api.mail.example"]
    assert len(mails) == 4
    assert all(c["answered_by"] == "declaration" and c["first_seq"] is not None for c in mails)
    [search] = [c for c in calls if c["host"] == "search.localhost"]
    assert (search["answered_by"], search["capture_mode"]) == ("pass_through", "pass_through")
    agent_calls = reference.rows(
        "SELECT wake, model, input_tokens, output_tokens, cost, currency FROM model_calls"
        " WHERE side = 'agent' ORDER BY wake",
        PRICES,
    )
    assert [m["wake"] for m in agent_calls] == [1, 2, 3]
    assert len(agent_calls) == sum(1 for s in spans if s.source is SpanSource.WIRE)
    for m in agent_calls:
        assert (m["model"], m["currency"]) == ("planner-small", "USD")
        tokens_in, tokens_out = int(str(m["input_tokens"])), int(str(m["output_tokens"]))
        assert m["cost"] == pytest.approx((tokens_in * 1.0 + tokens_out * 4.0) / 1e6)
    assert reference.rows("SELECT COUNT(*) AS n FROM actions WHERE kind = 'model_call'") == [{"n": 3}]


def test_the_reference_agent_read_with_the_installed_commands(reference: Played) -> None:
    [chase] = reference.rows("SELECT seq FROM messages WHERE is_follow_up = 1")
    explained = json.loads(reference.cli("explain", reference.run_id, str(chase["seq"]), "--json"))
    assert (explained["wake"]["reason"], explained["answers"]["is_ask"]) == ("due", 1)
    assert [d["source"] for d in explained["woken_by"]] == ["reported"]
    assert explained["read_before"], "the agent reads its memory before it follows up"
    traced = json.loads(reference.cli("trace", reference.run_id, "--kind", "model_call", "--json"))
    assert [a["wake"] for a in traced["actions"]] == [1, 2, 3]
    out = reference.cli("query", reference.run_id, "SELECT check_id, kind FROM findings", "--format", "csv")
    result = session.load(reference.state, reference.run_id).result
    assert out.splitlines()[1:] == [f"{f.check},{f.kind.value}" for f in result.findings]
    assert result.findings and all(f.kind is not FindingKind.FAIL for f in result.findings)


def _after_done(played: Played) -> list[int]:
    """The agent's events after the checkpoint at which it reported done, read from the world, not the read model."""
    with session.reading(played.state, played.run_id) as world:
        done = max(
            seq
            for seq, c in checkpoints(world).items()
            if c.agent.report is not None and c.agent.report.status is AgentStatus.DONE
        )
        return [e.seq for e in world.events() if e.actor is Actor.AGENT and e.seq > done]


def test_what_the_reference_agent_did_after_it_reported_done(reference: Played) -> None:
    """Its worker polls its job queue in its memory on a clock of its own, so whether a poll lands after the last
    report varies from run to run; whatever does is a read, never a write, so that checkpoint stays one a fork can
    start from."""
    late = reference.rows(
        "SELECT seq, kind, operation FROM actions"
        " WHERE seq > (SELECT checkpoint_seq FROM wakes WHERE reported_status = 'done') ORDER BY position"
    )
    assert [a["seq"] for a in late] == _after_done(reference)
    assert all((a["kind"], a["operation"]) == ("memory", "read") for a in late)


VARIES = "its worker's polls of its queue may or may not land after its last report"

NO_DATA: dict[str, dict[str, str]] = {
    "follow_up": {
        "Asks never answered": "Rosa answered the one ask",
        "One memory key over time": "the query names `asks/sofia`; this agent keeps `answer`, `status`, `next_wake`",
        "Model tokens, and cost, per wake": "the example calls no model",
        "The model call behind each message": "the example calls no model",
        "Actions after the agent reported it was done": "the agent acted on nothing after its last report",
        "Messages sent outside a person's working hours": "nobody in the scenario declares working hours",
        "The chain behind each failed finding": "the run passed",
        "The agent's own spans behind each call it made": "the example exports no telemetry",
    },
    "reference": {
        "Asks never answered": "Rosa answered the one ask",
        "Actions after the agent reported it was done": VARIES,
        "One memory key over time": "the query names `asks/sofia`; this agent keeps its `facts`, `sent` and `jobs`",
        "What the agent sent, read out of the request bodies": "the reference agent sends email, not Slack",
        "Messages sent outside a person's working hours": "nobody in the scenario declares working hours",
        "The chain behind each failed finding": "the run passed",
        "The agent's own spans behind each call it made": "it exports no spans; its model calls are recorded on the wire",
    },
}
"""Each documented query a run has no data for, and why; every other one must find rows on it."""


@pytest.mark.parametrize("title", sorted(QUERIES))
@pytest.mark.parametrize("which", sorted(NO_DATA))
def test_a_documented_query_on_a_real_run(follow_up: Played, reference: Played, which: str, title: str) -> None:
    played = follow_up if which == "follow_up" else reference
    found = played.rows(QUERIES[title], PRICES)
    if NO_DATA[which].get(title) == VARIES:
        assert bool(found) == bool(_after_done(played)), title
    elif title in NO_DATA[which]:
        assert found == [], f"{title!r} finds rows on the {which} run, though {NO_DATA[which][title]}"
    else:
        assert found, f"{title!r} found nothing on the {which} run"


def test_every_query_said_to_find_nothing_is_a_documented_one() -> None:
    assert len(QUERIES) >= 12
    assert all(title in QUERIES for titles in NO_DATA.values() for title in titles)
