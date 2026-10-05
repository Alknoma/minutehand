"""A run you drive yourself is scored like a run Minutehand drives.

One story: the agent asks Rosa, follows up twice a day apart, Rosa answers the second follow-up a day later, and
the agent relays her answer to Owen and says DONE. Played once by `minutehand run`, and once by a harness of the
test's own against `minutehand serve`, following `docs/serve.md` ("Scoring a run you drive yourself") to the
letter: a case label, the clock moved as a separate act, then a step around each go of the agent.

The one documented difference: the run stops `agent_done` and the case `closed` (a standing world's record says
`stop: closed`, `docs/serve.md`), so the verdict's sentence names a different stop. Seq numbers are positions in two
different logs and are compared with the numbers taken out. Anything else that differs is a failure.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import httpx

from minutehand.adapters.control.wire import CreateWorld
from minutehand.testing.world import open_case
from tests.acceptance.support import (
    OWEN,
    PYTHON,
    ROSA,
    STANDIN,
    TOLD_ONLY,
    free_port,
    minutehand,
    person,
    run_standin,
    scenario,
    scripted,
    served,
    standin,
    started,
    through_proxy,
    viewer,
)

STORY = scenario(
    "relay_story",
    person("owen", OWEN, TOLD_ONLY),
    person("rosa", ROSA, scripted({"to_ask": 3, "text": "The lakeside hall is booked."}, hours=24)),
    expect=[
        {"kind": "person_asked", "person": "rosa"},
        {"kind": "relayed", "said_by": "rosa", "to": "owen", "tell": "lakeside hall"},
    ],
)
BEHAVIOUR = {"ask": ROSA, "relay_to": OWEN, "follow_ups": "2", "every_hours": "24"}
TOKEN = "xoxb-standin"  # the token the stand-in agent's Slack client carries
SEQ = re.compile(r"seq \d+")


def comparable(findings: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return sorted((check, kind, SEQ.sub("seq N", message)) for check, kind, message in findings)


def driven(tmp_path: Path) -> tuple[str, dict[str, object], list[tuple[str, str, str]], Path]:
    """The story through `serve`, driven by this harness: the case's verdict kind, scorecard and findings."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    secret = "driven-signing-secret"
    with served(tmp_path) as server:
        env = {**server.environment(tmp_path / "ca.pem"), "STANDIN_PORT": str(port), "STANDIN_SECRET": secret}
        env.update({f"STANDIN_{k.upper()}": v for k, v in BEHAVIOUR.items()})
        with started([PYTHON, str(STANDIN)], env, tmp_path / "agent.log", ready=f"{base}/report"):
            spec = CreateWorld.model_validate(
                {
                    "seed": STORY,
                    "claims": {"tokens": [TOKEN]},
                    "inbound": [{"provider": "slack", "url": f"{base}/slack/events", "secret": secret}],
                    "scripted_people": True,
                }
            )
            case = open_case(server.client, "relay_story", [spec])
            world = case.worlds[0]
            agent = httpx.Client(base_url=base, trust_env=False, timeout=30)

            def wake(at: datetime, reason: str) -> dict[str, object]:
                agent.post("/wake", json={"now": at.isoformat(), "reason": reason, "goal": STORY["goal"]})
                report = agent.get("/report").json()
                assert isinstance(report, dict)
                return report

            now = world.now()
            with case.step(at=now, reason="start"):
                report = wake(now, "start")
            while report["status"] != "done" and report["next_wake"] is not None:
                due = datetime.fromisoformat(str(report["next_wake"]))
                case.advance(to=due)  # the clock, as before: a separate act
                world.quiet()
                with case.step(at=due, reason="due"):
                    report = wake(due, "due")
            result = case.close().result
            agent.close()
    found = [(f.check, f.kind.value, f.message) for f in result.findings]
    return result.verdict.kind.value, result.effectiveness.model_dump(mode="json"), found, server.state


def test_the_same_story_driven_through_serve_gets_the_verdict_scorecard_and_findings_run_gives(
    tmp_path: Path,
) -> None:
    played = run_standin(tmp_path / "run", STORY, standin(tmp_path / "run", **BEHAVIOUR))
    assert played.verdict["kind"] == "passed", played.explain()
    by_run = [(str(f["check"]), str(f["kind"]), str(f["message"])) for f in played.findings()]

    kind, scorecard, by_case, _ = driven(tmp_path / "serve")

    assert kind == played.verdict["kind"]
    assert scorecard == played.result["effectiveness"]
    assert comparable(by_case) == comparable(by_run)


def test_a_driven_case_across_three_worlds_is_one_run_in_runs_findings_and_the_viewer(tmp_path: Path) -> None:
    """Three worlds, three services (Slack, Asana, GitHub), one case label: one run everywhere a run is listed."""
    seed = {"people": [person("owen", OWEN, TOLD_ONLY)], "expect": [{"kind": "person_asked", "person": "owen"}]}
    tokens = {"slack": "xoxb-three-worlds", "asana": "asana-three-worlds", "github": "ghp_threeworlds"}
    calls = {
        "slack": ("POST", "https://slack.com/api/auth.test"),
        "asana": ("GET", "https://app.asana.com/api/1.0/users/me"),
        "github": ("GET", "https://api.github.com/user"),
    }
    with served(tmp_path) as server:
        specs = [CreateWorld.model_validate({"seed": seed, "claims": {"tokens": [t]}}) for t in tokens.values()]
        case = open_case(server.client, "three_worlds", specs)
        assert len(case.worlds) == 3
        with through_proxy(server, tmp_path / "ca.pem") as http, case.step(reason="one go of the agent"):
            for provider, (method, url) in calls.items():
                answered = http.request(method, url, headers={"Authorization": f"Bearer {tokens[provider]}"})
                assert answered.status_code < 500, (provider, answered.text)
        case.close()
        world_ids = [w.world_id for w in case.worlds]

        listed = minutehand("runs", "--state", str(server.state))
        assert listed.exit == 0, listed.explain()
        assert listed.stdout.count(case.case_id) == 1, listed.stdout
        rows = [line for line in listed.stdout.splitlines() if line and not line.startswith(" ")]
        assert [r.split()[0] for r in rows] == [case.case_id], "its worlds are not listed alone"

        read = minutehand("findings", case.case_id, "--state", str(server.state))
        assert read.exit == 1, read.explain()  # Owen was never asked
        assert "owen asked" in read.stdout

        with viewer(tmp_path, server.state) as view:
            runs = view.get("/api/runs")["runs"]
            assert isinstance(runs, list)
            assert [r["run_id"] for r in runs] == [case.case_id]
            assert sorted(runs[0]["worlds"]) == sorted(world_ids)
            assert runs[0]["case"] == "three_worlds"
            findings = view.get(f"/api/runs/{case.case_id}/findings")
            assert findings["finished"] is True
