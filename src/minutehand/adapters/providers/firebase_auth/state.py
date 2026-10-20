"""The only path to the store: what each thing Firebase Authentication holds is called and listed under.

Every thing is a RECORD entity of this provider. The project (`projects/{id}`), each account
(`projects/{id}/accounts/{uid}`, listed under `projects/{id}/accounts`, its snapshot a `RecordSnapshot` of resource
`user`), and the refresh tokens and out-of-band codes it issued (under the digest of the secret, never the secret
itself, listed under `refresh_tokens` and `oob_codes`, with no snapshot: they are the service's own bookkeeping).
"""

from __future__ import annotations

import hashlib
import string

from minutehand.adapters.providers.firebase_auth import wire
from minutehand.adapters.providers.firebase_auth.manifest import MANIFEST
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.store import Store

PROJECT = "project"
REFRESH_TOKENS = "refresh_tokens"
OOB_CODES = "oob_codes"
USER_RESOURCE = "user"
_ALPHABET = string.ascii_letters + string.digits
_PAGE = 500


def _ref(external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=external_id)


def digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def letters(*parts: str, length: int) -> str:
    """`length` letters and digits drawn from `parts`: the same parts give the same string."""
    drawn: list[str] = []
    round_ = 0
    while len(drawn) < length:
        number = int(digest(*parts, str(round_)), 16)
        for _ in range(40):
            number, at = divmod(number, len(_ALPHABET))
            drawn.append(_ALPHABET[at])
        round_ += 1
    return "".join(drawn[:length])


def accounts_of(project: str) -> str:
    return f"projects/{project}/accounts"


def user_said(user: wire.StoredUser) -> str:
    return " ".join(s for s in (user.localId, user.email, user.displayName, user.photoUrl) if s)


class Accounts:
    """The project and its accounts as the world log holds them."""

    def __init__(self, world: Store) -> None:
        self.world = world

    def project(self) -> wire.Project | None:
        stored = self.world.children(MANIFEST.key, EntityKind.RECORD, PROJECT, limit=1)
        return wire.Project.model_validate_json(stored[0].body) if stored else None

    def keep_project(self, project: wire.Project) -> None:
        self.world.apply(
            Change(
                entity=_ref(f"projects/{project.project_id}"),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=project.model_dump_json(),
                parent=PROJECT,
            )
        )

    def user(self, project: str, uid: str) -> wire.StoredUser | None:
        stored = self.world.get(_ref(f"{accounts_of(project)}/{uid}"))
        return wire.StoredUser.model_validate_json(stored.body) if stored is not None else None

    def users(self, project: str) -> list[wire.StoredUser]:
        found: list[wire.StoredUser] = []
        after: str | None = None
        while True:
            page = self.world.children(MANIFEST.key, EntityKind.RECORD, accounts_of(project), after=after, limit=_PAGE)
            found += [wire.StoredUser.model_validate_json(s.body) for s in page]
            if len(page) < _PAGE:
                return found
            after = page[-1].entity.external_id

    def by_email(self, project: str, email: str) -> wire.StoredUser | None:
        """The account holding `email`: Firebase matches addresses whatever their letter case."""
        return next((u for u in self.users(project) if u.email is not None and u.email.lower() == email.lower()), None)

    def by_link(self, project: str, provider: str, raw_id: str) -> wire.StoredUser | None:
        return next(
            (u for u in self.users(project) if any(k.providerId == provider and k.rawId == raw_id for k in u.links)),
            None,
        )

    def put_user(self, project: str, user: wire.StoredUser, *, actor: Actor, operation: Operation) -> None:
        self.world.apply(
            Change(
                entity=_ref(f"{accounts_of(project)}/{user.localId}"),
                operation=operation,
                actor=actor,
                body=user.model_dump_json(exclude_none=True),
                parent=accounts_of(project),
                after=RecordSnapshot(resource=USER_RESOURCE, text=user_said(user)),
            )
        )

    def delete_user(self, project: str, user: wire.StoredUser, *, actor: Actor) -> None:
        self.world.apply(
            Change(
                entity=_ref(f"{accounts_of(project)}/{user.localId}"),
                operation=Operation.DELETE,
                actor=actor,
                parent=accounts_of(project),
            )
        )

    def refresh(self, token: str) -> wire.StoredRefresh | None:
        stored = self.world.get(_ref(f"{REFRESH_TOKENS}/{digest(token)}"))
        return wire.StoredRefresh.model_validate_json(stored.body) if stored is not None else None

    def keep_refresh(self, token: str, issued: wire.StoredRefresh, *, actor: Actor) -> None:
        self.world.apply(
            Change(
                entity=_ref(f"{REFRESH_TOKENS}/{digest(token)}"),
                operation=Operation.CREATE,
                actor=actor,
                body=issued.model_dump_json(exclude_none=True),
                parent=REFRESH_TOKENS,
            )
        )

    def code(self, code: str) -> wire.StoredCode | None:
        stored = self.world.get(_ref(f"{OOB_CODES}/{digest(code)}"))
        return wire.StoredCode.model_validate_json(stored.body) if stored is not None else None

    def keep_code(self, code: str, kept: wire.StoredCode, *, operation: Operation) -> None:
        self.world.apply(
            Change(
                entity=_ref(f"{OOB_CODES}/{digest(code)}"),
                operation=operation,
                actor=Actor.AGENT,
                body=kept.model_dump_json(),
                parent=OOB_CODES,
            )
        )
