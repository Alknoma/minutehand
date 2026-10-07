"""Node's built-in `fetch`, which `@slack/web-api` v8 calls, reaches the Slack fake with nothing but the environment
Minutehand hands out: `NODE_USE_ENV_PROXY=1` makes it read `HTTPS_PROXY` (Node 24 and later). Without that variable it
ignores the proxy and goes to the real slack.com. A real `node` process; skipped where none of 24 or later is
installed."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from minutehand.adapters.providers.slack import state
from minutehand.session import Listen, agent_environment
from tests.providers.slack.intercepted import Intercepted
from tests.providers.slack.slack_workspace import TOKEN

NODE = shutil.which("node")


def _node_major() -> int:
    if NODE is None:
        return 0
    said = subprocess.run([NODE, "--version"], capture_output=True, text=True, check=True).stdout
    return int(said.strip().lstrip("v").split(".")[0])


FETCH = f"""
const answer = await fetch("https://slack.com/api/auth.test", {{
  method: "POST",
  headers: {{ authorization: "Bearer {TOKEN}" }},
}});
console.log(JSON.stringify(await answer.json()));
"""


@pytest.mark.skipif(_node_major() < 24, reason="node 24 or later is not installed")
async def test_nodes_own_fetch_reaches_the_fake_through_the_environment_handed_out(slack: Intercepted) -> None:
    assert NODE is not None
    handed = agent_environment(Listen(), slack.proxy.port, slack.proxy.ca_bundle, {}, telemetry_port=None)
    process = await asyncio.create_subprocess_exec(
        NODE,
        "--input-type=module",
        "-e",
        FETCH,
        env={"PATH": os.environ["PATH"], **handed},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, err.decode()
    said = json.loads(out)
    assert said["ok"] is True, said
    assert (said["team_id"], said["user_id"]) == (state.TEAM_ID, state.BOT_USER_ID)
    called = [c.exchange for c in slack.proxy.addon.worlds.lobby.store.calls()]
    assert [(e.host, e.path) for e in called] == [("slack.com", "/api/auth.test")]
