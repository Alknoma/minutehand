"""Write a finished run's view (`minutehand.session.recorded_view`) as a fixture: what every check reads of it.

    uv run python tests/data/trial/build.py <state dir> <run id> <name>

The fixtures here are runs of a purchasing agent driven by a real model against `scenario.yaml`'s world. What
the fixture keeps is the run as recorded, with three changes so it names nothing real and stays small: the agent's
calls to its model's API keep their place among the calls but not their host or bodies, chat API answers are cut
to their first 300 characters (no check reads them), and the product and model names the model wrote
are replaced by neutral ones. The agent's own instructions to its model are kept, less the lines its framework writes
into every system prompt.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from minutehand.domain.checks import RunView
from minutehand.domain.world import RecordedCall
from minutehand.session import recorded_view

NAMES = (
    (r"MacBook Pro 16-inch M4", "Model C 16-inch"),
    (r"Dell XPS 13", "Model A 13"),
    (r"Lenovo ThinkPad X1", "Model B X1"),
    (r"(?i)macbook", "Model C"),
    (r"Dell", "Vendor A"),
    (r"Lenovo", "Vendor B"),
    (r"ThinkPad", "Model B"),
    (r"XPS", "Model A"),
    (r"claude[-\w.]*", "model-x"),
    (r"Claude", "Assistant"),
    (r"Anthropic", "Vendor"),
)
FRAMEWORK_LINES = ("x-anthropic-billing-header", "You are Claude Code")
"""Lines the agent's framework writes into its system prompt on its own: not the agent's instructions."""
MODEL_HOST = "model.example"
CHAT_HOST = "slack.com"
CUT = 300


def _kept(call: RecordedCall) -> RecordedCall:
    x = call.exchange
    if x.tunnelled is not None or "anthropic" in x.host or "openai" in x.host:
        x = x.model_copy(update={"host": MODEL_HOST, "path": "/", "request_body": None, "response_body": None})
    elif x.host == CHAT_HOST and x.response_body is not None and len(x.response_body) > CUT:
        x = x.model_copy(update={"response_body": x.response_body[:CUT]})
    return call.model_copy(update={"exchange": x})


def _own(instructions: str) -> str:
    return "\n".join(line for line in instructions.splitlines() if not line.startswith(FRAMEWORK_LINES)).strip()


def fixture(view: RunView) -> str:
    kept = view.model_copy(
        update={
            "calls": [_kept(c) for c in view.calls or []],
            "agent_instructions": [_own(i) for i in view.agent_instructions],
        }
    )
    text = kept.model_dump_json(indent=1)
    for found, neutral in NAMES:
        text = re.sub(found, neutral, text)
    return text


def _readable(state: Path, run_id: str, into: Path) -> Path:
    """A copy of the run's directory whose agent file this version reads: the trial's agent file had a rule using
    `conveys`, which the automatic assessment replaced, and it is left out."""
    copied = into / "runs" / run_id
    shutil.copytree(state / "runs" / run_id, copied)
    agent = json.loads((copied / "agent.json").read_text(encoding="utf-8"))
    rules = []
    for rule in agent["assess"]:
        said = rule["count"]["messages"] if "messages" in rule["count"] else None
        if isinstance(said, dict) and said.pop("conveys", None):
            continue
        rules.append(rule)
    agent["assess"] = rules
    (copied / "agent.json").write_text(json.dumps(agent), encoding="utf-8")
    return into


if __name__ == "__main__":
    state, run_id, name = sys.argv[1:4]
    out = Path(__file__).parent / f"{name}.json"
    with tempfile.TemporaryDirectory() as scratch:
        view = recorded_view(_readable(Path(state), run_id, Path(scratch)), run_id)
    out.write_text(fixture(view) + "\n", encoding="utf-8")
