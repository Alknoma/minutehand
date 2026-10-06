"""What `GET /v1/environment` hands a service in a stack, and what a stack can read before any world is open: the
CA bundle and the health a Compose `healthcheck` waits on. No world is opened in this module, so the server every
test here reads has never held one."""

from __future__ import annotations

import ssl
from collections.abc import Iterator

import httpx
import pytest

from minutehand.serve import ServeOptions
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient

SERVED_AS = "minutehand"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MinutehandClient]:
    """A server named in its stack as `minutehand`, receiving no telemetry, so nothing but the environment's own
    rule puts its name among the hosts reached directly."""
    options = ServeOptions(
        proxy_port=0, control_port=0, telemetry_port=0, receive_telemetry=False, agent_host=SERVED_AS
    )
    with serve_in_background(tmp_path_factory.mktemp("serve"), options) as url, MinutehandClient(url) as found:
        yield found


def test_the_stack_names_its_services_and_they_are_reached_directly_by_every_library(
    client: MinutehandClient,
) -> None:
    variables = client.environment("/etc/minutehand/minutehand-ca-bundle.pem", no_proxy=["platform", "firestore"])

    direct = variables["NO_PROXY"].split(",")
    assert {"platform", "firestore", SERVED_AS} <= set(direct)
    assert variables["no_proxy"] == variables["no_grpc_proxy"] == variables["NO_PROXY"]
    assert variables["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] == "/etc/minutehand/minutehand-ca-bundle.pem"
    assert variables["HTTPLIB2_CA_CERTS"] == variables["SSL_CERT_FILE"] == variables["REQUESTS_CA_BUNDLE"]


def test_the_server_is_reached_directly_even_with_no_service_named(client: MinutehandClient) -> None:
    assert SERVED_AS in client.environment()["NO_PROXY"].split(",")


def test_the_ca_bundle_and_health_answer_before_any_world_is_open(client: MinutehandClient) -> None:
    assert client.worlds() == []
    assert client.health()
    bundle = client.ca().decode()
    assert bundle.count("-----BEGIN CERTIFICATE-----") > 1, "public roots and the proxy's CA"
    trust = ssl.create_default_context(cadata=bundle)
    assert trust.get_ca_certs()
    answered = httpx.get(f"{client.url}/v1/health", trust_env=False)
    assert answered.status_code == 200 and answered.text == "ok"
