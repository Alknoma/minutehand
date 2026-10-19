"""A backend signed in with Application Default Credentials of type `authorized_user` reaches its own world: the
refresh grant at `oauth2.googleapis.com` is routed by the refresh token the world claims and answered by the Google
Workspace provider from the world's `SignIn`, and the access token it mints is claimed by that world, so the
reCAPTCHA Enterprise assessment and the FCM sends that carry it as Bearer reach the same world.

Each backend runs in a process of its own configured by nothing but the environment Minutehand hands a service and
its gcloud configuration directory (`CLOUDSDK_CONFIG`, holding `application_default_credentials.json`), with stock
`google-auth` (`google.auth.default()`) and `firebase-admin` (`credentials.ApplicationDefault()`). Two worlds are
open at once, each claiming only its own refresh token."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor
from minutehand.testing.world import OpenWorld
from tests.providers.google_workspace.proxied import OFFLINE
from tests.serve.support import Served

BACKEND = """
import json
import google.auth
from google.auth.transport.requests import AuthorizedSession
import firebase_admin
from firebase_admin import credentials, messaging

PROJECT = "shop-app"
found, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
session = AuthorizedSession(found)
assessed = session.post(f"https://recaptchaenterprise.googleapis.com/v1/projects/{PROJECT}/assessments",
                        json={"event": {"token": "03AFcWeA-checkout", "expectedAction": "checkout"}})
firebase_admin.initialize_app(credentials.ApplicationDefault(), {"projectId": PROJECT})
sent = messaging.send_each_for_multicast(messaging.MulticastMessage(tokens=["fGcm-phone", "fGcm-gone"],
                                                                    data={"order": "A-1043"}))
print(json.dumps({"assessed": [assessed.status_code, assessed.json()["tokenProperties"]],
                  "sent": [r.success for r in sent.responses],
                  "unregistered": type(sent.responses[1].exception).__name__}), flush=True)
"""


def _world(served: Served, refresh: str, person: str) -> OpenWorld:
    seed = Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "people": [
                {"key": person, "name": person.title(), "email": f"{person}@example.com", "reply": {"kind": "silent"}}
            ],
            "sign_ins": [{"provider": "google_workspace", "credential": refresh, "person": person}],
            "provider_seeds": [
                {
                    "provider": "recaptcha_enterprise",
                    "body": json.dumps(
                        {"tokens": [{"token": "03AFcWeA-checkout", "action": "checkout", "score": 0.9}]}
                    ),
                },
                {
                    "provider": "firebase_cloud_messaging",
                    "body": json.dumps(
                        {"tokens": [{"token": "fGcm-phone"}, {"token": "fGcm-gone", "state": "unregistered"}]}
                    ),
                },
            ],
        }
    )
    return OpenWorld(served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=[refresh]))))


def _backend(served: Served, home: Path, refresh: str) -> subprocess.Popen[str]:
    """A backend whose gcloud configuration holds `refresh` as its user's Application Default Credentials."""
    home.mkdir(parents=True)
    (home / "application_default_credentials.json").write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": "1234.apps.googleusercontent.com",
                "client_secret": "client-secret",
                "refresh_token": refresh,
            }
        )
    )
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.lower().endswith("_proxy")
        and "CERT" not in name
        and "CA_BUNDLE" not in name
        and not name.startswith(("GOOGLE_", "CLOUDSDK_", "GCE_"))
    }
    environment = {**inherited, **served.environment, "CLOUDSDK_CONFIG": str(home), "PYTHONPATH": str(OFFLINE)}
    return subprocess.Popen(
        [sys.executable, "-c", BACKEND], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def test_two_backends_signed_in_with_user_credentials_each_reach_their_own_world(
    served: Served, tmp_path: Path
) -> None:
    worlds = {"1//ada-refresh-token": _world(served, "1//ada-refresh-token", "ada")}
    worlds["1//ben-refresh-token"] = _world(served, "1//ben-refresh-token", "ben")
    try:
        backends = {r: _backend(served, tmp_path / r.removeprefix("1//"), r) for r in worlds}
        for refresh, backend in backends.items():
            out, err = backend.communicate(timeout=60)
            assert backend.returncode == 0, err
            assert json.loads(out) == {
                "assessed": [200, {"valid": True, "createTime": "2026-09-01T09:00:00Z", "action": "checkout"}],
                "sent": [True, False],
                "unregistered": "UnregisteredError",
            }, refresh
        for world in worlds.values():
            hosts = [c.exchange.host for c in world.calls()]
            assert hosts.count("oauth2.googleapis.com") >= 1
            assert hosts.count("recaptchaenterprise.googleapis.com") == 1
            assert hosts.count("fcm.googleapis.com") == 2
            assert world.unmatched_calls() == []
            assessed = world.events(provider="recaptcha_enterprise", actor=Actor.AGENT)
            delivered = world.events(provider="firebase_cloud_messaging", actor=Actor.AGENT)
            assert len(assessed) == 1 and len(delivered) == 1
    finally:
        for world in worlds.values():
            served.client.close_world(world.world_id)
