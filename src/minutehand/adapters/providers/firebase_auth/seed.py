"""What a scenario seeds of Firebase Authentication: one project and its accounts, as the body of the scenario's
`ProviderSeed` for `firebase_auth`."""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field, JsonValue, model_validator

from minutehand.domain.scenario import Model


class SeededUser(Model):
    """An account the project holds when the world opens."""

    uid: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Its `localId`; None: derived from what the account is (its person, address or name)",
    )
    person: str | None = Field(
        default=None,
        description="Person.key whose address and name the account takes, unless `email` or "
        "`display_name` says otherwise",
    )
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    photo_url: str | None = None
    password: str | None = Field(
        default=None, min_length=6, description="Signs in with `signInWithPassword`; None: no password provider"
    )
    disabled: bool = False
    custom_claims: dict[str, JsonValue] | None = Field(
        default=None, description="What `set_custom_user_claims` would set: in every ID token it is issued"
    )
    google: str | None = Field(
        default=None,
        description="Person.key whose Google account (the world's `google_workspace` account of their address) is "
        "linked to it as the `google.com` provider, so a Google ID token of theirs signs in as this account",
    )
    created_before_start: timedelta = Field(default=timedelta(0), description="How long before the world's start")


class FirebaseAuthSeed(Model):
    """`ProviderSeed.body` for `firebase_auth`: the project the world is."""

    project_id: str = Field(
        pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$",
        description="The Firebase project's id, as the Admin SDK's `projectId` option names it and admin calls name "
        "it in their path (`/v1/projects/{project_id}/`)",
    )
    api_key: str = Field(
        min_length=1,
        description="The project's web API key, which end users' calls carry as `?key=`",
    )
    email_enumeration_protection: bool = Field(
        default=True,
        description="Whether a failed password sign-in says only INVALID_LOGIN_CREDENTIALS, as every project made "
        "since 15 September 2023 does by default",
    )
    users: list[SeededUser] = []

    @model_validator(mode="after")
    def _unique(self) -> FirebaseAuthSeed:
        uids = [u.uid for u in self.users if u.uid is not None]
        if len(uids) != len(set(uids)):
            raise ValueError("two seeded Firebase users declare one uid")
        return self
