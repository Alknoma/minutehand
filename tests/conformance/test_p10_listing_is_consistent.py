"""Property 10 — listing is consistent.

Every list operation a driver has, paged at the smallest page size the vendor allows over a seeded set larger than
one page, returns each item exactly once across the pages, in an order that is the same on a second reading, and the
same set as the unpaged read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tests.conformance.contract import Documents, Driver, Family, Messaging, Property, Session, Tickets
from tests.conformance.harness import Case, Harness, absent, cases, parametrize, require, seed
from tests.conformance.neutral import merged

type Record = Callable[[str, object], None]


@dataclass(frozen=True)
class Listing:
    name: str
    family: Family
    seeded: Callable[[str, int], dict[str, object]]
    paged: Callable[[Session, int], list[list[str]]]
    whole: Callable[[Session], list[str]]


def _people(provider: str, count: int) -> dict[str, object]:
    return {"people": [{"key": f"lister{n}", "name": f"Lister {n}", "email": f"lister{n}@example.com",
                        "reply": {"kind": "silent"}} for n in range(count)]}  # fmt: skip


def _tickets(provider: str, count: int) -> dict[str, object]:
    return {"tickets": [{"provider": provider, "project": "Launch", "title": f"Listed {n}"} for n in range(count)]}


def _history(provider: str, count: int) -> dict[str, object]:
    posts = [{"by": "sofia", "text": f"Listed post {n}", "ago": f"PT{count - n}H"} for n in range(count)]
    return {"channels": [{"provider": provider, "name": "listed", "members": ["owen", "sofia"], "history": posts}]}


def _documents(provider: str, count: int) -> dict[str, object]:
    return {"documents": [{"provider": provider, "title": f"Listed doc {n}", "text": "x"} for n in range(count)]}


def _ticket_session(s: Session) -> Tickets:
    assert isinstance(s, Tickets)
    return s


def _messaging_session(s: Session) -> Messaging:
    assert isinstance(s, Messaging)
    return s


def _document_session(s: Session) -> Documents:
    assert isinstance(s, Documents)
    return s


def _listed_pages(session: Messaging, size: int) -> list[list[str]]:
    return session.history_pages(session.channel("listed"), size)


def _listed_whole(session: Messaging) -> list[str]:
    return [m.id for m in session.history(session.channel("listed")) if m.thread_of is None]


LISTINGS = {
    lt.name: lt
    for lt in [
        Listing(
            "people", Family.ACCOUNTS, _people, lambda s, n: s.people_pages(n), lambda s: [p.id for p in s.people()]
        ),
        Listing(
            "tickets",
            Family.TICKETS,
            _tickets,
            lambda s, n: _ticket_session(s).ticket_pages("Launch", n),
            lambda s: list(_ticket_session(s).ticket_ids().values()),
        ),
        Listing(
            "history",
            Family.MESSAGING,
            _history,
            lambda s, n: _listed_pages(_messaging_session(s), n),
            lambda s: _listed_whole(_messaging_session(s)),
        ),
        Listing(
            "documents",
            Family.DOCUMENTS,
            _documents,
            lambda s, n: _document_session(s).document_pages(n),
            lambda s: [d.id for d in _document_session(s).documents()],
        ),
    ]
}


def _floor(driver: Driver, name: str) -> int:
    assert name in driver.page_floor, f"{driver.provider}'s driver states no smallest page size for {name}"
    return driver.page_floor[name]


@parametrize([c for lt in LISTINGS.values() for c in cases(Property.LISTING, lt.family, [lt.name])])
def test_a_listing_paged_at_the_smallest_page_returns_each_item_once_in_a_stable_order(
    case: Case, harness: Harness, record_property: Record
) -> None:
    listing = LISTINGS[case.case]
    driver = require(case.provider, listing.family)
    if absent(driver, f"listing.{listing.name}", record_property):
        return
    if listing.name == "people" and absent(driver, "accounts.people", record_property):
        return
    if listing.name == "history" and absent(driver, "messaging.channels", record_property):
        return
    floor = _floor(driver, listing.name)
    seeded = merged(seed(case.provider), listing.seeded(case.provider, 2 * floor + 1))
    with harness.world(driver, seeded) as world, harness.session(driver, world) as session:
        pages = listing.paged(session, floor)
        again = listing.paged(session, floor)
        whole = listing.whole(session)
    flat = [item for page in pages for item in page]
    broken: list[str] = []
    if len(pages) < 2:
        broken.append(f"{len(flat)} items at a page size of {floor} came in {len(pages)} page(s)")
    if any(len(page) > floor for page in pages):
        broken.append(f"a page holds more than {floor}: {[len(p) for p in pages]}")
    twice = sorted({i for i in flat if flat.count(i) > 1})
    if twice:
        broken.append(f"listed more than once across pages: {twice[:5]}")
    if flat != [item for page in again for item in page]:
        broken.append("a second paged reading answers another order")
    if set(flat) != set(whole):
        broken.append(f"paged and unpaged differ: only paged {sorted(set(flat) - set(whole))[:5]}, "
                      f"only unpaged {sorted(set(whole) - set(flat))[:5]}")  # fmt: skip
    assert not broken, "\n".join(broken)
