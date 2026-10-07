"""Transparent capture through a real Linux container: a client that ignores every proxy variable (`curl --noproxy '*'`,
with `NO_PROXY=*` set as Docker Desktop sets it) is still answered by the Slack fake, because the script
`minutehand env --format redirect` prints sends the container's connections to ports 80 and 443 to the proxy's
redirected listener. Before the script runs, the same call goes to the address slack.com resolves to in the container
(a documentation address no packet reaches) and times out: what it reached afterwards is the redirect's doing.

Needs Docker and a package index for the image (alpine with iptables and curl): run with -m docker."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from minutehand.adapters.providers.slack import state
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.redirected import redirect_script
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from tests.providers.slack.slack_workspace import TOKEN, Workspace

pytestmark = pytest.mark.docker

IMAGE = "minutehand-test-redirect:alpine"
DOCKERFILE = b"FROM alpine:3.20\nRUN apk add --no-cache iptables curl\n"
UNREACHABLE = "203.0.113.10"
"""TEST-NET-3: slack.com's address inside the container, which no packet reaches unless the redirect takes it."""


@dataclass
class Listening:
    proxy: Proxy
    port: int


@pytest.fixture
async def listening(workspace: Workspace, tmp_path: Path) -> AsyncIterator[Listening]:
    proxy = Proxy(
        Routing(Registry.installed()),
        workspace.store,
        workspace.clock,
        confdir=tmp_path / "ca",
        host="0.0.0.0",
        redirect_port=0,
    )
    async with proxy:
        proxy.mount(
            workspace.store, workspace.clock, {"slack": workspace.provider.app(workspace.store, workspace.clock)}
        )
        assert proxy.redirect_port is not None
        yield Listening(proxy, proxy.redirect_port)


def _image() -> None:
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, "-"], input=DOCKERFILE, check=True, capture_output=True)


CALL = (
    "curl -sS --noproxy '*' --max-time {limit} --cacert /etc/minutehand/ca-bundle.pem -X POST "
    f"-H 'Authorization: Bearer {TOKEN}' https://slack.com/api/auth.test"
)


async def test_a_client_that_ignores_the_proxy_is_answered_by_the_fake_once_its_container_redirects(
    listening: Listening, tmp_path: Path
) -> None:
    await asyncio.to_thread(_image)
    script = tmp_path / "redirect.sh"
    script.write_text(redirect_script("host.docker.internal", listening.port), encoding="utf-8")
    inside = " ; ".join(
        [
            CALL.format(limit=3) + " && echo BEFORE-REACHED || echo BEFORE-FAILED",
            "sh /etc/minutehand/redirect.sh",
            "echo ANSWER",
            CALL.format(limit=20),
        ]
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--cap-add",
        "NET_ADMIN",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--add-host",
        f"slack.com:{UNREACHABLE}",
        "-e",
        "NO_PROXY=*",
        "-e",
        "no_proxy=*",
        "-v",
        f"{listening.proxy.ca_bundle}:/etc/minutehand/ca-bundle.pem:ro",
        "-v",
        f"{script}:/etc/minutehand/redirect.sh:ro",
        IMAGE,
        "sh",
        "-c",
        inside,
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(process.communicate(), 50)
    said = out.decode()
    assert "BEFORE-FAILED" in said, (said, err.decode())
    answer = json.loads(said.split("ANSWER", 1)[1])
    assert answer["ok"] is True, (answer, err.decode())
    assert (answer["team_id"], answer["user_id"]) == (state.TEAM_ID, state.BOT_USER_ID)
    called = [c.exchange for c in listening.proxy.addon.worlds.lobby.store.calls()]
    assert [(e.host, e.path, e.status) for e in called] == [("slack.com", "/api/auth.test", 200)]
