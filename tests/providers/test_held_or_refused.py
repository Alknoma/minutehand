"""Every fact the shared seed can state, against every provider: held, or refused at load, never dropped in silence.

Three things are walked. Every field of the shared seed models is named in `FIELDS`, as the `Holds` fact that answers
for it or as a reason no provider answers for it alone; a field added to a shared model without one fails here.
Every `Holds` fact is reached by `FIELDS` or by a happening or people-change kind. And each fact is seeded into each
provider beside a seed without it: held, the provider must store something different; not held, `unheld` must
refuse it, naming it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.refusals import unheld
from minutehand.application.run_clock import RunClock
from minutehand.domain.provider import Holds, Manifest
from minutehand.domain.scenario import (
    Access,
    Person,
    PersonAccount,
    Scenario,
    SeededChannel,
    SeededComment,
    SeededDocument,
    SeededFile,
    SeededPost,
    SeededTicket,
    SharedSpace,
    SignIn,
)
from minutehand.domain.world import EntityKind

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
REGISTRY = Registry.installed()
MANIFESTS = {m.key: m for m in REGISTRY.manifests}

ALWAYS = "required, or the thing itself: held by every provider that holds the thing at all"
SIMULATION = "how the person behaves as a simulated colleague; no service holds it"
NAMES = "names something else in the seed; checked when the scenario loads"

FIELDS: dict[type, dict[str, str]] = {
    Person: {
        "key": NAMES,
        "name": ALWAYS,
        "email": "person_without_email",
        "title": "person_title",
        "account": "person_guest person_deactivated person_bot",
        "accounts": "account_login account_id account_name account_email_hidden",
        "facts": SIMULATION,
        "stale_facts": SIMULATION,
        "reply": SIMULATION,
        "working_hours": "person_working_hours",
        "absences": "person_absences",
        "early_follow_ups": SIMULATION,
    },
    PersonAccount: {
        "provider": NAMES,
        "login": "account_login",
        "id": "account_id",
        "name": "account_name",
        "email_visible": "account_email_hidden",
    },
    SeededTicket: {
        "provider": NAMES,
        "project": ALWAYS,
        "title": ALWAYS,
        "body": ALWAYS,
        "assignee": ALWAYS,
        "state": ALWAYS,
        "key": "ticket_key",
        "labels": "ticket_labels",
        "comments": "ticket_comments",
        "number": "ticket_number",
        "id": "ticket_id",
    },
    SeededComment: {"by": ALWAYS, "text": ALWAYS},
    SeededDocument: {
        "provider": NAMES,
        "title": ALWAYS,
        "text": ALWAYS,
        "kind": "document_spreadsheet document_presentation document_file",
        "rows": "document_spreadsheet",
        "mime_type": "document_file",
        "folder": "document_folder",
        "owner": "document_owner",
        "space": "document_space",
        "shared_with": "document_shared_with",
        "modified_before_start": "document_modified_before_start",
        "modified_by": "document_modified_by",
        "id": "document_id",
    },
    Access: {"person": ALWAYS, "role": ALWAYS},
    SharedSpace: {"provider": NAMES, "name": "space_spaces", "members": "space_spaces", "id": "space_id"},
    SignIn: {"provider": NAMES, "credential": "sign_ins", "person": "sign_ins"},
    SeededChannel: {
        "provider": NAMES,
        "name": "channel_named channel_direct",
        "id": "channel_id",
        "private": "channel_private",
        "archived": "channel_archived",
        "topic": "channel_topic",
        "purpose": "channel_purpose",
        "members": ALWAYS,
        "agent_member": "channel_without_agent",
        "history": "channel_history",
    },
    SeededPost: {
        "by": ALWAYS,
        "text": ALWAYS,
        "ago": ALWAYS,
        "key": NAMES,
        "id": "channel_post_id",
        "files": "channel_files",
        "replies": "channel_threads",
    },
    SeededFile: {"name": ALWAYS, "title": ALWAYS, "mime_type": ALWAYS, "text": ALWAYS},
}

REASONS = {ALWAYS, SIMULATION, NAMES}
NAMING_ONLY = {"ticket_key": "the scenario's name for a ticket, read by the provider's own seed: stores nothing alone"}
HAPPENINGS = ("ticket_action_", "messaging_", "document_change_", "people_change_")
LANDED_ELSEWHERE = {"faults": "declared through `DeclaresFaults`, held by the providers that implement it"}


def test_every_field_of_every_shared_seed_model_is_answered_for() -> None:
    unnamed = [f"{m.__name__}.{f}" for m, named in FIELDS.items() for f in m.model_fields if f not in named]
    stale = [f"{m.__name__}.{f}" for m, named in FIELDS.items() for f in named if f not in m.model_fields]
    facts = {
        fact
        for named in FIELDS.values()
        for answer in named.values()
        if answer not in REASONS
        for fact in answer.split()
    }
    assert not unnamed, f"a shared seed field no fact answers for: {unnamed}"
    assert not stale, f"named here and no longer a field: {stale}"
    assert facts <= set(Holds.model_fields), f"facts that are no Holds field: {sorted(facts - set(Holds.model_fields))}"


def test_every_holds_fact_is_reached_by_a_seed_field_or_a_kind() -> None:
    facts = {fact for named in FIELDS.values() for answer in named.values() for fact in answer.split()}
    unreached = [
        f for f in Holds.model_fields if f not in facts and not f.startswith(HAPPENINGS) and f not in LANDED_ELSEWHERE
    ]
    assert not unreached, f"Holds facts nothing in the shared seed states: {unreached}"


# -- each fact seeded into each provider ------------------------------------------------------------------------

Seed = dict[str, object]


def _people(provider: str) -> list[dict[str, object]]:
    account = [{"provider": provider, "login": "sofia-r"}] if provider == "github" else []
    return [
        {"key": "owen", "name": "Owen Hart", "email": "owen@example.com", "reply": {"kind": "silent"},
         "accounts": [{"provider": provider, "login": "owen-h"}] if provider == "github" else []},
        {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"},
         "accounts": account},
        {"key": "tess", "name": "Tess Lund", "email": "tess@example.com", "reply": {"kind": "silent"},
         "accounts": [{"provider": provider, "login": "tess-l"}] if provider == "github" else []},
    ]  # fmt: skip


def _base(provider: str) -> Seed:
    manifest = MANIFESTS[provider]
    seed: Seed = {
        "name": "held",
        "goal": "g",
        "owner": "owen",
        "starts_at": START.isoformat(),
        "people": _people(provider),
    }
    if EntityKind.TICKET in manifest.kinds:
        seed["tickets"] = [{"provider": provider, "project": "Launch", "title": "Book venue", "assignee": "sofia"}]
    if EntityKind.DOCUMENT in manifest.kinds:
        seed["documents"] = [{"provider": provider, "title": "Plan", "text": "hello", "owner": "owen"}]
    if EntityKind.CHANNEL in manifest.kinds:
        seed["channels"] = [{"provider": provider, "name": "launch", "members": ["owen", "sofia"],
                             "history": [{"by": "owen", "text": "hi", "ago": "PT2H"}]}]  # fmt: skip
    return seed


def _sofia(seed: Seed) -> dict[str, object]:
    people = seed["people"]
    assert isinstance(people, list)
    found = people[1]
    assert isinstance(found, dict)
    return found


def _first(seed: Seed, what: str) -> dict[str, object]:
    found = seed[what]
    assert isinstance(found, list) and found and isinstance(found[0], dict)
    return found[0]


def _seeded(seed: Seed, what: str) -> list[object]:
    found = seed[what]
    assert isinstance(found, list)
    return found


def _account(provider: str, **facts: object) -> Callable[[Seed], None]:
    def change(seed: Seed) -> None:
        sofia = _sofia(seed)
        held = sofia["accounts"]
        assert isinstance(held, list)
        entry = held[0] if held else {"provider": provider}
        assert isinstance(entry, dict)
        sofia["accounts"] = [{**entry, **facts}]

    return change


def _ids(provider: str) -> str:
    """An id in each provider's own format, for the facts that declare one."""
    return {
        "slack": "U0SOFIA77",
        "jira": "5b10ac8d82e05b22cc7d4ef5",
        "youtrack": "1-77",
        "asana": "120000000077",
        "microsoft": "6e7b768e-07e2-4810-8459-485f84f8f204",
        "notion": "6e7b768e-07e2-4810-8459-485f84f8f204",
        "google_drive": "11177",
        "github": "583231",
    }.get(provider, "77")


TICKET_IDS = {"jira": "10777", "youtrack": "2-777", "asana": "120000000777"}
DOCUMENT_IDS = {
    "google_drive": "1aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
    "notion": "6e7b768e-07e2-4810-8459-485f84f8f2aa",
    "microsoft": "01ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",
}
SPACE_IDS = {
    "google_drive": "0AbCdEfGhIjKlMnOpQ",
    "microsoft": "held.sharepoint.com,6e7b768e-07e2-4810-8459-485f84f8f2bb,6e7b768e-07e2-4810-8459-485f84f8f2cc",
}
LOGINS = {"microsoft": "sofia.romano@held.onmicrosoft.com"}
CHANNEL_IDS = {"slack": "C0LAUNCH77", "microsoft": "19:launch77@thread.tacv2"}
POST_IDS = {"slack": "1788000000.000100", "microsoft": "1"}


def _variants(provider: str) -> dict[str, Callable[[Seed], None]]:
    def person(**facts: object) -> Callable[[Seed], None]:
        """On a person nothing else in the seed names, so a fact a service holds by refusing what names them (a bot
        is no channel member in Teams) is seen as held."""

        def change(seed: Seed) -> None:
            people = seed["people"]
            assert isinstance(people, list) and isinstance(people[2], dict)
            people[2].update(facts)

        return change

    def ticket(**facts: object) -> Callable[[Seed], None]:
        return lambda seed: _first(seed, "tickets").update(facts)

    def document(**facts: object) -> Callable[[Seed], None]:
        return lambda seed: _first(seed, "documents").update(facts)

    def channel(**facts: object) -> Callable[[Seed], None]:
        return lambda seed: _first(seed, "channels").update(facts)

    def post(**facts: object) -> Callable[[Seed], None]:
        def change(seed: Seed) -> None:
            history = _first(seed, "channels")["history"]
            assert isinstance(history, list) and isinstance(history[0], dict)
            history[0].update(facts)

        return change

    def space(seed: Seed) -> None:
        seed["spaces"] = [{"provider": provider, "name": "Team", "members": [{"person": "owen"}]}]
        _first(seed, "documents")["space"] = "Team"

    def spaced(seed: Seed) -> None:
        space(seed)
        spaces = seed["spaces"]
        assert isinstance(spaces, list) and isinstance(spaces[0], dict)
        spaces[0]["id"] = SPACE_IDS.get(provider, "space-77")

    def direct(seed: Seed) -> None:
        seed["channels"] = [
            {"provider": provider, "members": ["sofia"], "history": [{"by": "sofia", "text": "hi", "ago": "PT1H"}]}
        ]

    def sign_in(seed: Seed) -> None:
        seed["sign_ins"] = [{"provider": provider, "credential": "sign-in-credential-77", "person": "owen"}]

    return {
        "person_without_email": person(email=None),
        "person_title": person(title="Head of Legal"),
        "person_guest": person(account="guest"),
        "person_deactivated": person(account="deactivated"),
        "person_bot": person(account="bot"),
        "person_working_hours": person(working_hours={"timezone": "Europe/Rome"}),
        "person_absences": person(absences=[{"lasts": "P2D", "reason": "on leave"}]),
        "account_login": _account(provider, login=LOGINS.get(provider, "sofia-romano")),
        "account_id": _account(provider, id=_ids(provider)),
        "account_name": _account(provider, name="Sofia R."),
        "account_email_hidden": _account(provider, email_visible=False),
        "ticket_key": ticket(key="venue"),
        "ticket_labels": ticket(labels=["events"]),
        "ticket_comments": ticket(comments=[{"by": "owen", "text": "which one?"}]),
        "ticket_number": ticket(number=142),
        "ticket_id": ticket(id=TICKET_IDS.get(provider, "777")),
        "document_spreadsheet": document(kind="spreadsheet", text="", rows=[["a", "b"]]),
        "document_presentation": document(kind="presentation", text="One\n\nTwo"),
        "document_file": document(kind="file", text="raw", mime_type="text/csv"),
        "document_folder": document(folder="Plans/2026"),
        "document_owner": document(owner="sofia"),
        "document_space": space,
        "document_shared_with": document(shared_with=[{"person": "sofia", "role": "reader"}]),
        "document_modified_before_start": document(modified_before_start="P3D"),
        "document_modified_by": document(modified_by="sofia"),
        "document_id": document(id=DOCUMENT_IDS.get(provider, "doc-77")),
        "space_spaces": space,
        "space_id": spaced,
        "sign_ins": sign_in,
        "channel_named": lambda seed: _seeded(seed, "channels").append(
            {"provider": provider, "name": "ops", "members": ["owen"]}
        ),
        "channel_direct": direct,
        "channel_private": channel(private=True),
        "channel_archived": channel(archived=True),
        "channel_topic": channel(topic="Launch week"),
        "channel_purpose": channel(purpose="Plan the launch"),
        "channel_without_agent": channel(agent_member=False),
        "channel_history": channel(history=[{"by": "sofia", "text": "the venue is booked", "ago": "PT3H"}]),
        "channel_threads": post(replies=[{"by": "sofia", "text": "noted", "ago": "PT1H"}]),
        "channel_files": post(files=[{"name": "a.txt", "text": "x"}]),
        "channel_id": channel(id=CHANNEL_IDS.get(provider, "chan-77")),
        "channel_post_id": post(id=POST_IDS.get(provider, "post-77")),
    }


NEEDS = {
    "ticket_": EntityKind.TICKET,
    "document_": EntityKind.DOCUMENT,
    "space_": EntityKind.DOCUMENT,
    "channel_": EntityKind.CHANNEL,
}
"""A fact about a thing a provider holds none of is answered by refusing the thing itself."""

PERSON_WIDE = "person_"
"""A person is one across every service, so a person fact a service has no place for is not refused: it shows none."""

CASES = [(m.key, fact) for m in REGISTRY.manifests for fact in _variants(m.key)]


def _stored(seed: Seed, provider: str, tmp_path: Path, name: str) -> list[tuple[str, str]]:
    scenario = Scenario.model_validate(json.loads(json.dumps(seed)))
    store = SqliteStore(tmp_path / f"{name}.db", "held", RunClock(START))
    REGISTRY.provider(MANIFESTS[provider]).seed(scenario, store)
    return sorted(
        (f"{e.entity.kind.value}:{e.entity.external_id}", (store.get(e.entity) or e).model_dump_json())
        for e in store.events()
    )


@pytest.mark.parametrize(("provider", "fact"), CASES, ids=[f"{p}-{f}" for p, f in CASES])
def test_a_fact_is_held_or_refused_at_load(provider: str, fact: str, tmp_path: Path) -> None:
    manifest: Manifest = MANIFESTS[provider]
    held = bool(getattr(manifest.holds, fact))
    needs = next((kind for prefix, kind in NEEDS.items() if fact.startswith(prefix)), None)
    if needs is not None and needs not in manifest.kinds:
        assert not held, f"{provider} holds no {needs.value} and says it holds {fact}"
        return
    base = _base(provider)
    varied = json.loads(json.dumps(base))
    _variants(provider)[fact](varied)
    scenario = Scenario.model_validate(varied)
    refused = unheld(scenario, MANIFESTS)
    if not held:
        if fact.startswith(PERSON_WIDE):
            return
        assert refused, f"{provider} does not hold {fact}, and a seed stating it loads"
        return
    assert not refused, f"{provider} holds {fact}, and the load refuses it: {refused}"
    if fact in NAMING_ONLY:
        return
    before = _stored(base, provider, tmp_path, "before")
    after = _stored(varied, provider, tmp_path, "after")
    assert before != after, f"{provider} says it holds {fact}, and seeding it stores nothing different"
