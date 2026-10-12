"""`minutehand replay`: a finished run played again, from its start or from a checkpoint, the agent's model calls
answered from its record, so an agent that keeps its state only in its process (`tests/agents/remembers`) rebuilds
it exactly; from `live_from` on, its model answers, and what it says builds on the state the replay rebuilt. Frozen,
nothing goes to a model: the first call the record does not hold stops the run, naming it. The model is a local
HTTPS server that answers otherwise the second time."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.files import load_agent, load_scenario
from minutehand.application.model_export import batch_lines
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import Reported
from minutehand.domain.experiment import ReplayCase, ReplayCases
from minutehand.domain.run import ReplayRecord, StopReason, VerdictKind
from minutehand.domain.world import Actor, MessageSnapshot
from minutehand.replay_all import CaseOutcome, described, play_all
from tests.e2e.support import free_port
from tests.proxy.upstream import Answer, Authority, make_authority, model_api

AGENT = Path(__file__).resolve().parents[1] / "agents" / "remembers"
PORT: dict[Path, int] = {}


def _said(text: str) -> Answer:
    body = {"object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": text}}]}
    return Answer("application/json", [json.dumps(body).encode()])


async def _play(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: Authority,
    says: str,
    replay_of: str | None = None,
    live_from: datetime | None = None,
    replay_at: int | None = None,
    frozen: bool = False,
    prompt_prefix: str = "",
) -> tuple[str, list[str], int]:
    """The run's id, what the agent posted at each wake, and how many calls reached the model. A replay plays the
    agent file its run kept, so every play of a test listens on the test's one port; `prompt_prefix` is a change
    to what the agent asks its model, as a changed prompt in its code would be."""
    outcome, reached = await _played(
        tmp_path, monkeypatch, authority, says, replay_of, live_from, replay_at, frozen, prompt_prefix
    )
    run_id = outcome.record.run_id
    with session.reading(tmp_path / "state", run_id) as kept:
        posted = [
            e.after.text for e in kept.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
        ]
    return run_id, posted, reached


async def _played(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: Authority,
    says: str,
    replay_of: str | None,
    live_from: datetime | None,
    replay_at: int | None,
    frozen: bool,
    prompt_prefix: str,
) -> tuple[session.Outcome, int]:
    port = PORT.setdefault(tmp_path, free_port())
    base = f"http://127.0.0.1:{port}"
    agent = load_agent(AGENT / "agent.yaml").model_copy(
        update={"wakes": [Reported(wake_url=f"{base}/wake", report_url=f"{base}/report")]}
    )
    listen = session.Listen(upstream_ca=authority.ca_cert, receive_telemetry=False, model_hosts=["localhost"])
    state, command = tmp_path / "state", [sys.executable, str(AGENT / "agent.py")]
    async with model_api(authority, _said(says)) as model:
        monkeypatch.setenv("PORT", str(port))
        monkeypatch.setenv("MODEL_URL", f"https://localhost:{model.port}/v1/chat/completions")
        monkeypatch.setenv("PROMPT_PREFIX", prompt_prefix)
        if replay_of is None:
            [outcome] = await session.play(
                load_scenario(AGENT / "scenario.yaml"), agent, state=state, command=command, listen=listen
            )
        else:
            outcome = await session.replay(
                replay_of,
                state=state,
                command=command,
                listen=listen,
                live_from=live_from,
                at_seq=replay_at,
                frozen=frozen,
            )
        return outcome, len(model.received)


async def test_a_replay_rebuilds_state_kept_only_in_the_agents_process_then_plays_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = make_authority(tmp_path / "upstream-ca")
    first, posted, reached = await _play(tmp_path, monkeypatch, authority, "then")
    assert posted == ["then", "then | then", "then | then | then"] and reached == 3

    again, replayed, reached = await _play(tmp_path, monkeypatch, authority, "now", replay_of=first)
    assert replayed == posted and reached == 0, "every model call answered from the record"
    assert session.replayed(tmp_path / "state", again) == ReplayRecord(
        replay_of=first, model_calls_replayed=3, parted=None
    )

    live_from = datetime.fromisoformat("2026-08-26T00:00:00+00:00")
    _, onward, reached = await _play(tmp_path, monkeypatch, authority, "now", replay_of=first, live_from=live_from)
    assert onward == ["then", "then | then", "then | then | now"] and reached == 1, "live on the state it rebuilt"


async def test_the_model_calls_are_written_out_as_openai_batch_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = make_authority(tmp_path / "upstream-ca")
    first, _, _ = await _play(tmp_path, monkeypatch, authority, "then")

    with session.reading(tmp_path / "state", first) as kept:
        lines = [json.loads(line) for line in batch_lines(kept.spans())]

    assert [line["method"] for line in lines] == ["POST"] * 3
    assert {line["url"] for line in lines} == {"/v1/chat/completions"}
    assert lines[1]["body"]["messages"][0]["content"] == "note 2; so far: ['then']"
    assert lines[0]["response"] == {
        "status_code": 200,
        "body": {"object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "then"}}]},
    }
    assert [line["minutehand"]["at"][:10] for line in lines] == ["2026-08-24", "2026-08-25", "2026-08-26"]


async def test_a_replay_from_a_checkpoint_has_its_parents_model_answers_until_the_agent_asks_something_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewound to the checkpoint after the first wake and played on with nothing changed (a fork with no
    overrides), the agent's model calls after it are answered as its parent's model answered them: the child
    differs from its parent only by what its code now asks."""
    monkeypatch.setenv("IN_MEMORY", "1")
    authority = make_authority(tmp_path / "upstream-ca")
    first, posted, _ = await _play(tmp_path, monkeypatch, authority, "then")
    after_first_wake = next(p for p in session.fork_points(tmp_path / "state", first) if p.wake == 1)

    again, onward, reached = await _play(
        tmp_path, monkeypatch, authority, "now", replay_of=first, replay_at=after_first_wake.seq
    )

    assert onward == posted and reached == 0, "the parent's answers, not the model's new ones"
    assert session.replayed(tmp_path / "state", again) == ReplayRecord(
        replay_of=first, model_calls_replayed=2, parted=None
    )
    assert session.load(tmp_path / "state", again).record.parent_run == first


async def test_a_frozen_replay_the_record_holds_whole_sends_nothing_to_a_model_and_ends_as_its_parent_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IN_MEMORY", "1")
    authority = make_authority(tmp_path / "upstream-ca")
    first, posted, _ = await _play(tmp_path, monkeypatch, authority, "then")
    after_first_wake = next(p for p in session.fork_points(tmp_path / "state", first) if p.wake == 1)

    again, onward, reached = await _play(
        tmp_path, monkeypatch, authority, "now", replay_of=first, replay_at=after_first_wake.seq, frozen=True
    )

    parent, child = session.load(tmp_path / "state", first), session.load(tmp_path / "state", again)
    assert onward == posted and reached == 0
    assert child.record.stop is parent.record.stop and child.result.verdict.kind is parent.result.verdict.kind
    replayed = session.replayed(tmp_path / "state", again)
    assert replayed is not None and (replayed.frozen, replayed.parted) == (True, None)


async def test_a_frozen_replay_stops_where_the_agents_prompt_changed_naming_the_call_sent_and_the_one_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's code now asks its model differently (a changed prompt): the first changed call is refused, no
    model is reached, and the run says what was sent beside what the record held next."""
    monkeypatch.setenv("IN_MEMORY", "1")
    authority = make_authority(tmp_path / "upstream-ca")
    first, posted, _ = await _play(tmp_path, monkeypatch, authority, "then")
    after_first_wake = next(p for p in session.fork_points(tmp_path / "state", first) if p.wake == 1)

    again, onward, reached = await _play(
        tmp_path,
        monkeypatch,
        authority,
        "now",
        replay_of=first,
        replay_at=after_first_wake.seq,
        frozen=True,
        prompt_prefix="Be brief. ",
    )

    child = session.load(tmp_path / "state", again)
    assert reached == 0, "nothing went to the model"
    assert onward == posted[:1], "the second wake's call was refused, so the agent posted nothing more"
    assert child.record.stop is StopReason.REPLAY_PARTED
    assert child.result.verdict.kind is VerdictKind.REPLAY_PARTED and child.result.exit_code == 7
    assert child.record.failure is not None and "a call " + first + " never made" in child.record.failure
    replayed = session.replayed(tmp_path / "state", again)
    assert replayed is not None and replayed.frozen and replayed.model_calls_replayed == 0
    assert replayed.parted_at is not None and replayed.parted_at.expected is not None
    sent, expected = replayed.parted_at.sent, replayed.parted_at.expected
    assert sent.path == expected.path == "/v1/chat/completions"
    assert json.loads(sent.body)["messages"][0]["content"] == "Be brief. note 2; so far: ['then']"
    assert json.loads(expected.body)["messages"][0]["content"] == "note 2; so far: ['then']"


async def test_a_replay_from_a_checkpoint_with_live_from_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunRefused, match="--live-from is for a replay from the start"):
        await session.replay(
            "run1", state=tmp_path, at_seq=5, live_from=datetime.fromisoformat("2026-08-26T00:00+00:00")
        )


async def test_a_sweep_says_which_cases_a_changed_prompt_touched_and_at_which_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two cases of one recorded run, frozen: with the agent unchanged every case is answered from the record whole;
    with its prompt changed, each parts at its first call, named, and the sweep exits 1."""
    monkeypatch.setenv("IN_MEMORY", "1")
    authority = make_authority(tmp_path / "upstream-ca")
    first, _, _ = await _play(tmp_path, monkeypatch, authority, "then")
    points = {p.wake: p.seq for p in session.fork_points(tmp_path / "state", first)}
    cases = ReplayCases(cases=[ReplayCase(run=first, at=points[1]), ReplayCase(run=first, at=points[2])])
    cases_file = tmp_path / "cases.yaml"
    cases_file.write_text(f"cases:\n  - {{run: {first}, at: {points[1]}}}\n  - {{run: {first}, at: {points[2]}}}\n")
    flags = ["--upstream-ca", str(authority.ca_cert), "--model-host", "localhost", "--no-receive-telemetry"]
    command = [sys.executable, str(AGENT / "agent.py")]

    async def sweep(prefix: str):
        async with model_api(authority, _said("now")) as model:
            monkeypatch.setenv("MODEL_URL", f"https://localhost:{model.port}/v1/chat/completions")
            monkeypatch.setenv("PROMPT_PREFIX", prefix)
            done = await play_all(
                cases,
                cases_file=cases_file,
                state=tmp_path / "state",
                command=command,
                jobs=1,
                frozen=True,
                passed_on=flags,
            )
            return done, len(model.received)

    same, reached = await sweep("")
    assert reached == 0 and same.exit_code == 0
    assert [p.outcome for p in same.played] == [CaseOutcome.SAME, CaseOutcome.SAME]
    assert [p.words for p in same.played] == [
        "2 model call(s) from the record, passed as its parent",
        "1 model call(s) from the record, passed as its parent",
    ]
    assert "every case as its record has it" in described(same)

    touched, reached = await sweep("Be brief. ")
    assert reached == 0 and touched.exit_code == 1
    assert [p.outcome for p in touched.played] == [CaseOutcome.PARTED, CaseOutcome.PARTED]
    assert all(p.verdict is VerdictKind.REPLAY_PARTED for p in touched.played)
    assert all(p.replayed is not None and p.replayed.parted_at is not None for p in touched.played)
    assert "2 case(s) the change touched" in described(touched)
    assert f"PARTED         {first} at {points[1]}" in described(touched)
