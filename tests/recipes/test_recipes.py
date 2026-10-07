"""examples/recipes, each run as its README runs it: an agent written with a framework, its model the recipes' fake
model, through both scenarios. A late answer passes; silence draws one follow-up, and no follows_up_when_due finding.

Run with `uv sync --group recipes` and `uv run pytest -m recipes`. The Node recipe runs when `node` is on the PATH
and `npm ci` succeeds in its folder; with RECIPES_NODE_REQUIRED set (as CI sets it) either failing fails the test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from examples.recipes import fake_model
from minutehand import session
from minutehand.application.files import load_agent, load_scenario
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import VerdictKind
from minutehand.domain.world import Actor, MessageSnapshot
from tests.e2e.support import free_port

pytestmark = pytest.mark.recipes

RECIPES = Path(__file__).resolve().parents[2] / "examples" / "recipes"
ROSA, OWEN = "rosa@example.com", "owen@example.com"


@dataclass(frozen=True)
class Recipe:
    folder: str
    port: int  # the port its agent.yaml names
    program: str  # what runs its agent file
    model_variable: str  # where the agent reads its model API's base URL
    model_path: str  # the API's path on the fake model: chat completions under /v1, messages at the root
    agent_file: str = "agent.py"

    @property
    def wire(self) -> str:
        return "/v1/chat/completions" if self.model_path == "/v1" else "/v1/messages"


PYTHON = [
    Recipe("langgraph", 8711, sys.executable, "MODEL_BASE_URL", "/v1"),
    Recipe("openai_agents", 8712, sys.executable, "MODEL_BASE_URL", "/v1"),
    Recipe("claude_agent_sdk", 8713, sys.executable, "ANTHROPIC_BASE_URL", ""),
    Recipe("pydantic_ai", 8714, sys.executable, "MODEL_BASE_URL", "/v1"),
]
NODE = Recipe("vercel_ai_sdk", 8715, "node", "MODEL_BASE_URL", "/v1", agent_file="agent.ts")


@dataclass(frozen=True)
class Played:
    verdict: VerdictKind
    words: str
    checks_failed: list[str]
    said: list[tuple[str, str]]  # (recipient, text) for every message the agent sent, in order
    model_calls: list[str]  # the path of every request the fake model answered


async def play(recipe: Recipe, scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Played:
    """The recipe's agent.yaml as written, moved to a port of the test's own, run through the scenario."""
    folder = RECIPES / recipe.folder
    port = free_port()
    written = load_agent(folder / "agent.yaml").model_dump_json()
    agent = AgentUnderTest.model_validate_json(written.replace(f"127.0.0.1:{recipe.port}", f"127.0.0.1:{port}"))
    server, served = fake_model.start()
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.setenv(recipe.model_variable, f"http://127.0.0.1:{server.server_port}{recipe.model_path}")
    try:
        [outcome] = await session.play(
            load_scenario(folder / scenario),
            agent,
            state=tmp_path / "state",
            command=[recipe.program, str(folder / recipe.agent_file)],
            listen=session.Listen(receive_telemetry=False),
        )
    finally:
        server.shutdown()
    with session.reading(tmp_path / "state", outcome.record.run_id) as world:
        said = [
            (", ".join(e.after.recipient_emails), e.after.text)
            for e in world.events()
            if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
        ]
    verdict = outcome.result.verdict
    failed = [f.check for f in outcome.result.findings if f.kind is FindingKind.FAIL]
    return Played(verdict.kind, verdict.words, failed, said, [path for path, _ in served.received])


def assert_late_answer_passes(played: Played, recipe: Recipe) -> None:
    assert played.verdict is VerdictKind.PASSED, (played.words, played.checks_failed)
    assert [text.startswith("Following up") for to, text in played.said if to == ROSA] == [False, True, False]
    assert any(to == OWEN and "lakeside hall" in text for to, text in played.said), played.said
    assert played.model_calls and set(played.model_calls) == {recipe.wire}


def assert_silence_draws_one_follow_up(played: Played, recipe: Recipe) -> None:
    assert "follows_up_when_due" not in played.checks_failed
    assert played.verdict is VerdictKind.PASSED, (played.words, played.checks_failed)
    assert [text.startswith("Following up") for to, text in played.said if to == ROSA] == [False, True]
    assert [text.startswith("No answer from") for to, text in played.said if to == OWEN] == [True]
    assert set(played.model_calls) == {recipe.wire}


@pytest.mark.parametrize("recipe", PYTHON, ids=[r.folder for r in PYTHON])
async def test_a_python_recipe_passes_when_the_answer_comes_after_its_follow_up(
    recipe: Recipe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_late_answer_passes(await play(recipe, "scenario_late.yaml", tmp_path, monkeypatch), recipe)


@pytest.mark.parametrize("recipe", PYTHON, ids=[r.folder for r in PYTHON])
async def test_a_python_recipe_follows_up_once_on_silence_and_then_tells_the_owner(
    recipe: Recipe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_silence_draws_one_follow_up(await play(recipe, "scenario_silent.yaml", tmp_path, monkeypatch), recipe)


def installed_node_recipe() -> None:
    """`npm ci` in the Node recipe's folder, or a skip saying why it could not run; a failure under CI."""
    required = "RECIPES_NODE_REQUIRED" in os.environ
    npm = shutil.which("npm")
    if shutil.which("node") is None or npm is None:
        if required:
            pytest.fail("node and npm are required (RECIPES_NODE_REQUIRED) and not on the PATH")
        pytest.skip("node or npm is not on the PATH")
    done = subprocess.run(
        [npm, "ci", "--no-audit", "--no-fund"], cwd=RECIPES / NODE.folder, capture_output=True, text=True, timeout=200
    )
    if done.returncode != 0:
        if required:
            pytest.fail(f"npm ci failed:\n{done.stderr}")
        pytest.skip(f"npm ci failed, so the Node recipe cannot run here:\n{done.stderr[-2000:]}")


@pytest.mark.timeout(300)
async def test_the_vercel_ai_sdk_recipe_passes_a_late_answer_and_follows_up_once_on_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_node_recipe()
    assert_late_answer_passes(await play(NODE, "scenario_late.yaml", tmp_path / "late", monkeypatch), NODE)
    assert_silence_draws_one_follow_up(await play(NODE, "scenario_silent.yaml", tmp_path / "silent", monkeypatch), NODE)
