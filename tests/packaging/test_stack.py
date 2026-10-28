"""The image as one container in a stack: `minutehand serve` on a Docker network, and a second container standing
in for a service, which finds it by name, downloads its CA from the control API, opens a world, and makes an
HTTPS call through the proxy that the world records. Both containers and the network are removed."""

from __future__ import annotations

import json
import secrets

import pytest

from tests.packaging.conftest import checked, run, tool

pytestmark = [pytest.mark.packaging, pytest.mark.timeout(900)]

SERVICE = r"""
import json, time
import httpx
from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed
from minutehand.testing.client import MinutehandClient

control = MinutehandClient("http://minutehand:8081")
for _ in range(300):
    try:
        if control.health():
            break
    except httpx.HTTPError:
        pass
    time.sleep(0.1)
with open("/tmp/ca.pem", "wb") as ca:
    ca.write(control.ca())
env = control.environment(ca_path="/tmp/ca.pem")
seed = Seed.model_validate({"people": [{"key": "sofia", "name": "Sofia", "email": "sofia@example.com"}]})
world = control.create_world(CreateWorld(seed=seed, claims=Claims(tokens=["xoxb-stack"])))
with httpx.Client(proxy=env["HTTPS_PROXY"], verify="/tmp/ca.pem", trust_env=False) as slack:
    answered = slack.post("https://slack.com/api/auth.test", headers={"Authorization": "Bearer xoxb-stack"})
calls = control.calls(world.world_id).calls
print(json.dumps({
    "proxy": env["HTTPS_PROXY"],
    "status": answered.status_code,
    "ok": answered.json()["ok"],
    "recorded": [[c.exchange.host, c.exchange.path, c.exchange.status] for c in calls],
}))
"""


def test_a_service_container_reaches_minutehand_by_name_and_its_call_is_recorded(image: str) -> None:
    docker = tool("docker")
    tag = secrets.token_hex(4)
    network, server = f"minutehand-pkg-net-{tag}", f"minutehand-pkg-serve-{tag}"
    checked(docker, "network", "create", network)
    try:
        checked(
            docker,
            "run",
            "--detach",
            "--name",
            server,
            "--network",
            network,
            "--network-alias",
            "minutehand",
            image,
            "serve",
            "--host",
            "0.0.0.0",
            "--agent-host",
            "minutehand",
        )
        service = run(
            docker,
            "run",
            "--rm",
            "--network",
            network,
            "--entrypoint",
            "/opt/minutehand/bin/python",
            image,
            "-c",
            SERVICE,
        )
        assert service.returncode == 0, f"{service.stdout}\n{service.stderr}\n{run(docker, 'logs', server).stderr}"
        seen = json.loads(service.stdout.strip().splitlines()[-1])
        assert seen["proxy"] == "http://minutehand:8080"
        assert (seen["status"], seen["ok"]) == (200, True)
        assert seen["recorded"] == [["slack.com", "/api/auth.test", 200]]
    finally:
        run(docker, "rm", "--force", server)
        run(docker, "network", "rm", network)
