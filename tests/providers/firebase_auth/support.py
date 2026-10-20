"""A Firebase project per test, in one `minutehand serve` per module, reached the way a service reaches it: stock
`firebase_admin` in a process of its own, configured by nothing but the environment Minutehand hands a service, on
Application Default Credentials of type `authorized_user` (gcloud's well-known file, found through
`CLOUDSDK_CONFIG`), and plain REST callers through the proxy. `offline/` refuses any connection that is not to this
machine, so a library that ignored the proxy fails here instead of reaching Google.

A client program talks back one JSON line per `say(...)`.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld

OFFLINE = Path(__file__).parents[1] / "google_workspace" / "offline"
RUNTIME = "runtime@{project}.iam.gserviceaccount.com"

PRELUDE = """
import json, sys
import firebase_admin
from firebase_admin import auth, exceptions

app = firebase_admin.initialize_app(options={{"projectId": {project!r}}})

def say(**found):
    print(json.dumps(found), flush=True)

def refused(call):
    try:
        call()
    except Exception as error:
        return type(error).__name__
    return None
"""


@dataclass
class Project:
    """One world: a Firebase project, its web API key, and the ADC refresh token its services sign in with."""

    client: MinutehandClient
    world: OpenWorld
    project_id: str
    api_key: str
    refresh: str
    environment: dict[str, str]
    home: Path

    def http(self) -> httpx.Client:
        trust = ssl.create_default_context(cafile=self.environment["SSL_CERT_FILE"])
        return httpx.Client(proxy=self.environment["HTTPS_PROXY"], verify=trust, trust_env=False, timeout=30)

    def rest(self, method: str, body: dict[str, object]) -> httpx.Response:
        """An end user's call: `?key=` names the project."""
        with self.http() as http:
            return http.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:{method}?key={self.api_key}", json=body
            )

    def admin(self, path: str, body: dict[str, object]) -> httpx.Response:
        """An admin call with no Authorization, as a test process sends one: its path names the project."""
        with self.http() as http:
            return http.post(f"https://identitytoolkit.googleapis.com/v1/projects/{self.project_id}/{path}", json=body)

    def firebase_admin(self, program: str, *, timeout: float = 60) -> list[dict[str, object]]:
        """`program` after the prelude, under stock firebase_admin in a process of its own; every `say` it made."""
        done = subprocess.run(
            [sys.executable, "-c", PRELUDE.format(project=self.project_id) + program],
            env=self.child_environment(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if done.returncode != 0:
            raise AssertionError(f"the firebase_admin program failed:\n{done.stderr[-4000:]}")
        return [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]

    def child_environment(self) -> dict[str, str]:
        variables = {k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE_", "FIREBASE_", "CLOUDSDK_"))}
        variables.update(self.environment)
        variables["PYTHONPATH"] = str(OFFLINE)
        variables["CLOUDSDK_CONFIG"] = str(self.home)
        variables["HOME"] = str(self.home)
        return variables


def open_project(
    client: MinutehandClient,
    environment: dict[str, str],
    home: Path,
    tag: str,
    *,
    users: list[dict[str, object]] | None = None,
    people: list[dict[str, object]] | None = None,
    metadata: bool = False,
    enumeration_protection: bool = True,
    starts_at: str | None = None,
) -> Project:
    """A world that is one Firebase project, unique to `tag`; with `metadata`, it also claims the metadata server's
    host and seeds its runtime service account."""
    project_id = f"proj-{tag}"
    api_key = f"web-key-{tag}"
    refresh = f"1//adc-refresh-{tag}"
    seed: dict[str, object] = {
        "name": f"firebase_{tag}",
        "people": people or [{"key": "owen", "name": "Owen Owner", "email": f"owen@{tag}.example.com"}],
        "provider_seeds": [
            {
                "provider": "firebase_auth",
                "body": {
                    "project_id": project_id,
                    "api_key": api_key,
                    "email_enumeration_protection": enumeration_protection,
                    "users": users or [],
                },
            },
            {
                "provider": "google_workspace",
                "body": {"runtime_service_account": RUNTIME.format(project=project_id)} if metadata else {},
            },
        ],
    }
    if starts_at is not None:
        seed["starts_at"] = starts_at
    claims = Claims(
        keys=[project_id, api_key],
        tokens=[refresh],
        hosts=["metadata.google.internal"] if metadata else [],
    )
    view = client.create_world(CreateWorld(seed=Seed.model_validate(seed), claims=claims))
    home.mkdir(parents=True, exist_ok=True)
    (home / "application_default_credentials.json").write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": f"client-{tag}.apps.googleusercontent.com",
                "client_secret": f"secret-{tag}",
                "refresh_token": refresh,
            }
        )
    )
    return Project(client, OpenWorld(client, view), project_id, api_key, refresh, environment, home)
