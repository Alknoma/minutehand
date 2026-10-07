"""Property 5 — ids are stable.

Open a world from a seed and read every seeded thing's vendor id through the vendor's API. Then (a) add one more of
each kind to the live world, and (b) separately, open a second world whose seed is the first plus one more of each
kind inserted FIRST in each list: every original thing has the same vendor id in all three readings. Every id
matches the vendor's documented format (the driver's `id_formats`). A seed that declares a thing's vendor-visible id,
where the provider's docs say it can, gets exactly that.
"""

from __future__ import annotations

from collections.abc import Callable

from minutehand.adapters.control.wire import FurtherSeed
from tests.conformance.contract import Documents, Driver, Family, IdKind, Messaging, Property, Session, Tickets
from tests.conformance.harness import (
    MANIFESTS,
    PEOPLE,
    START,
    Case,
    Harness,
    absent,
    cases,
    families,
    holds_channels,
    parametrize,
    require,
    tag,
)

type Record = Callable[[str, object], None]
type Inventory = dict[tuple[IdKind, str], str]


def _original(provider: str) -> dict[str, list[dict[str, object]]]:
    held = families(MANIFESTS[provider])
    found: dict[str, list[dict[str, object]]] = {"people": [dict(p) for p in PEOPLE]}
    if Family.TICKETS in held:
        found["tickets"] = [
            {"provider": provider, "project": "Launch", "title": "Original one", "assignee": "sofia"},
            {"provider": provider, "project": "Launch", "title": "Original two"},
        ]
    if Family.MESSAGING in held and holds_channels(provider):
        found["channels"] = [
            {"provider": provider, "name": "launch", "members": ["owen", "sofia", "mila"],
             "history": [{"by": "sofia", "text": "Original post", "ago": "PT2H"},
                         {"by": "owen", "text": "Original second post", "ago": "PT1H"}]},
            {"provider": provider, "name": "second", "members": ["owen", "sofia"]},
        ]  # fmt: skip
    if Family.DOCUMENTS in held:
        found["documents"] = [
            {"provider": provider, "title": "Original doc", "text": "one"},
            {"provider": provider, "title": "Original second doc", "text": "two"},
        ]
    return found


def _one_more(provider: str) -> dict[str, list[dict[str, object]]]:
    held = families(MANIFESTS[provider])
    found: dict[str, list[dict[str, object]]] = {
        "people": [{"key": "adam", "name": "Adam Added", "email": "adam@example.com", "reply": {"kind": "silent"}}]
    }
    if Family.TICKETS in held:
        found["tickets"] = [{"provider": provider, "project": "Launch", "title": "Added ticket"}]
    if Family.MESSAGING in held and holds_channels(provider):
        found["channels"] = [{"provider": provider, "name": "added", "members": ["owen"],
                              "history": [{"by": "owen", "text": "Added post", "ago": "PT3H"}]}]  # fmt: skip
    if Family.DOCUMENTS in held:
        found["documents"] = [{"provider": provider, "title": "Added doc", "text": "three"}]
    return found


def inventory(session: Session, driver: Driver, record: Record) -> Inventory:
    """Every seeded thing's vendor id, read through the vendor's API, by what it is."""
    found: Inventory = {}
    if not absent(driver, "accounts.people", record):
        for person in session.people():
            found[(IdKind.PERSON, person.email or person.name)] = person.id
    if isinstance(session, Tickets):
        for title, ticket in session.ticket_ids().items():
            found[(IdKind.TICKET, title)] = ticket
    if isinstance(session, Messaging) and not absent(driver, "messaging.channels", record):
        for channel in session.channels():
            if channel.name is not None:
                found[(IdKind.CHANNEL, channel.name)] = channel.id
        for name in ("launch", "added"):
            if (IdKind.CHANNEL, name) in found:
                for message in session.history(found[(IdKind.CHANNEL, name)]):
                    found[(IdKind.MESSAGE, f"#{name} {message.text}")] = message.id
    if isinstance(session, Documents):
        for document in session.documents():
            found[(IdKind.DOCUMENT, document.title)] = document.id
    return found


def _seed(lists: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    """The owner named, so a person listed before the first does not change whose world it is."""
    return {"starts_at": START, "owner": "owen", **lists}


def _first(original: dict[str, list[dict[str, object]]], more: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    return _seed({name: [*more.get(name, []), *items] for name, items in original.items()})


def _moved(before: Inventory, after: Inventory) -> list[str]:
    return sorted(f"{k.value} {what}: {before[(k, what)]} -> {after.get((k, what))}"
                  for k, what in before if after.get((k, what)) != before[(k, what)])  # fmt: skip


@parametrize(cases(Property.IDS, Family.ACCOUNTS, ["stable_under_additions_and_reordering"]))
def test_every_seeded_things_vendor_id_survives_additions_live_and_in_a_reordered_seed(
    case: Case, harness: Harness, record_property: Record
) -> None:
    """The ids read first are read again after one more of each kind is added to the live world, and again in a
    second world whose seed lists one more of each kind before the originals."""
    driver = require(case.provider, Family.ACCOUNTS)
    original, more = _original(case.provider), _one_more(case.provider)
    claims = tag()
    with harness.world(driver, {}, spec=driver.world(_seed(original), claims)) as world:
        with harness.session(driver, world) as session:
            first = inventory(session, driver, record_property)
        if not first and absent(driver, "accounts.people", record_property):
            record_property("conformance_not_applicable", f"{case.provider}: nothing of the shared seed is its to show")
            return
        assert first, "the vendor's API shows no seeded thing at all"
        world.seed(FurtherSeed.model_validate(more))
        with harness.session(driver, world) as session:
            live = inventory(session, driver, record_property)
    with harness.world(driver, {}, spec=driver.world(_first(original, more), claims)) as world:
        with harness.session(driver, world) as session:
            reordered = inventory(session, driver, record_property)
    broken = [f"after a live addition, {m}" for m in _moved(first, live)]
    broken += [f"in a seed listing one more first, {m}" for m in _moved(first, reordered)]
    assert not broken, "\n".join(broken)


@parametrize(cases(Property.IDS, Family.ACCOUNTS, ["documented_format"]))
def test_every_vendor_id_matches_the_vendors_documented_format(
    case: Case, harness: Harness, record_property: Record
) -> None:
    driver = require(case.provider, Family.ACCOUNTS)
    with harness.world(driver, _seed(_original(case.provider))) as world, harness.session(driver, world) as session:
        found = inventory(session, driver, record_property)
    wrong = sorted(
        f"{k.value} {what}: {value!r} does not match {driver.id_formats[k].pattern}"
        for (k, what), value in found.items()
        if not driver.id_formats[k].fullmatch(value)
    )
    assert not wrong, "\n".join(wrong)


@parametrize(cases(Property.IDS, Family.ACCOUNTS, ["declared_ids"]))
def test_a_seed_that_declares_a_vendor_visible_id_gets_exactly_that(
    case: Case, harness: Harness, record_property: Record
) -> None:
    """Where the provider's docs say a seed may name an id or number, the vendor's API answers exactly it."""
    driver = require(case.provider, Family.ACCOUNTS)
    declared = driver.declared_ids()
    if not declared:
        record_property("conformance_not_applicable", f"{case.provider}: its docs let a seed declare no id")
        return
    wrong: list[str] = []
    for one in declared:
        seeded = _seed(_original(case.provider)) | dict(one.seed)
        with harness.world(driver, seeded) as world, harness.session(driver, world) as session:
            found = one.read(session)
            if found != one.expected:
                wrong.append(f"{one.name}: declared {one.expected!r}, the API answers {found!r}")
    assert not wrong, "\n".join(wrong)
