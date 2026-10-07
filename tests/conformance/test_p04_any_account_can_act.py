"""Property 4 — any account can act.

For every kind of account a seed can declare (a person, a person without an email, an account known by a vendor
login with dots, dashes and uppercase, a bot), the account can sign in when it has a credential, is listed, can be
assigned, messaged or shared with where the vendor allows, can act through the control API, and can be deactivated —
or that one operation is refused with its reason. No operation fails with "no such person" for an account the seed
declared.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import pydantic
from pydantic import TypeAdapter

from minutehand.domain.scenario import AccessRole, Happening
from minutehand.testing.client import Refused, Unsupported
from minutehand.testing.world import OpenWorld
from tests.conformance.contract import (
    AccountKind,
    Documents,
    Driver,
    Family,
    Messaging,
    PersonSeen,
    Property,
    Session,
    Tickets,
    VendorRefused,
)
from tests.conformance.harness import MANIFESTS, Case, Harness, absent, cases, families, parametrize, require, seed
from tests.conformance.neutral import merged

type Record = Callable[[str, object], None]

NO_SUCH_PERSON = re.compile(r"no such (person|user|account)|unknown (person|user)|user not found", re.IGNORECASE)
HAPPENING: TypeAdapter[Happening] = TypeAdapter(Happening)


@dataclass(frozen=True)
class Account:
    kind: AccountKind
    person: dict[str, object]
    login: str | None = None
    capability: str | None = None

    @property
    def key(self) -> str:
        return str(self.person["key"])

    @property
    def name(self) -> str:
        return str(self.person["name"])

    @property
    def email(self) -> str | None:
        return str(self.person["email"]) if "email" in self.person else None


ACCOUNTS = {
    a.kind: a
    for a in [
        Account(AccountKind.MEMBER, {"key": "ava", "name": "Ava Member", "email": "ava@example.com"}),
        Account(AccountKind.NO_EMAIL, {"key": "nora", "name": "Nora Nomail"}, capability="accounts.no_email"),
        Account(
            AccountKind.VENDOR_LOGIN,
            {"key": "lena", "name": "Lena Ortiz", "email": "lena@example.com"},
            login="",
            capability="accounts.vendor_login",
        ),
        Account(
            AccountKind.BOT,
            {"key": "botty", "name": "Botty Service", "email": "botty@example.com", "account": "bot"},
            capability="accounts.bot",
        ),
    ]
}


class Steps:
    """Each step's failure, kept so one case reports every operation that broke."""

    def __init__(self, record: Record) -> None:
        self.broken: list[str] = []
        self.record = record

    def run(self, name: str, step: Callable[[], None]) -> None:
        try:
            step()
        except VendorRefused as refused:
            if refused.status == 404 or NO_SUCH_PERSON.search(str(refused)):
                self.broken.append(f"{name}: failed as if the account did not exist: {refused}")
            else:
                self.record("conformance_refused", f"{name}: {refused}")
        except Refused as refused:
            if NO_SUCH_PERSON.search(refused.error):
                self.broken.append(f"{name}: the control API answered no such person: {refused.error}")
            elif refused.status != 409:
                self.broken.append(f"{name}: refused {refused.status}: {refused.error}")
            else:
                self.record("conformance_refused", f"{name}: {refused.error}")
        except (AssertionError, NotImplementedError, StopIteration, KeyError) as failed:
            self.broken.append(f"{name}: {type(failed).__name__} {failed}")


def _find(people: list[PersonSeen], account: Account) -> PersonSeen:
    found = [p for p in people if p.name == account.name or (account.login is not None and p.login == account.login)]
    assert len(found) == 1, f"the vendor lists {len(found)} accounts for {account.name}: {[p.name for p in people]}"
    return found[0]


def _seed(provider: str, account: Account) -> dict[str, object]:
    more: dict[str, object] = {"people": [account.person | {"reply": {"kind": "silent"}}]}
    if Family.DOCUMENTS in families(MANIFESTS[provider]):
        more["documents"] = [
            {"provider": provider, "title": "Account doc", "text": "x",
             "shared_with": [{"person": account.key, "role": "writer"}]}
        ]  # fmt: skip
    return merged(seed(provider), more)


def _act_through_control(world: OpenWorld, provider: str, account: Account) -> None:
    held = families(MANIFESTS[provider])
    if Family.TICKETS in held:
        body: dict[str, object] = {"kind": "ticket", "person": account.key, "ticket": "Book the venue", "after": "PT0S",
                                   "action": {"kind": "comments", "text": f"{account.name} comments"}}  # fmt: skip
    elif Family.MESSAGING in held:
        body = {"kind": "posts", "provider": provider, "person": account.key, "text": f"{account.name} posts"}
    elif Family.DOCUMENTS in held:
        body = {"kind": "document", "person": account.key, "document": "Account doc", "after": "PT1M",
                "action": {"kind": "edited", "append": f"{account.name} edits"}}  # fmt: skip
    else:
        return
    world.happen(HAPPENING.validate_python(body))


def _family_operation(session: Session, driver: Driver, account: Account, found: PersonSeen, record: Record) -> None:
    """Assigned, messaged or shared with: whichever the provider's families do."""
    if isinstance(session, Tickets):
        ticket = session.ticket_ids()["Book the venue"]
        session.assign(ticket, found.id)
        seen = session.read_ticket(ticket)
        assert seen.assignee_id == found.id, f"assigned {found.id}, the API shows {seen.assignee_id}"
    if isinstance(session, Messaging):
        session.post(session.direct(found.id), f"hello {account.name}")
    if isinstance(session, Documents) and account.email is not None and not absent(driver, "documents.sharing", record):
        document = next(d.id for d in session.documents() if d.title == "Account doc")
        session.share(document, account.email, AccessRole.READER)


@parametrize(cases(Property.ACCOUNTS, Family.ACCOUNTS, [k.value for k in AccountKind]))
def test_any_account_the_seed_declares_can_sign_in_be_listed_be_named_act_and_be_deactivated(
    case: Case, harness: Harness, record_property: Record
) -> None:
    """Every operation on an account the seed declared works, or is refused with its reason; none fails as if the
    account did not exist."""
    driver = require(case.provider, Family.ACCOUNTS)
    account = ACCOUNTS[AccountKind(case.case)]
    if absent(driver, "accounts.people", record_property):
        return
    if account.capability is not None and absent(driver, account.capability, record_property):
        return
    if account.kind is AccountKind.VENDOR_LOGIN:
        account = Account(account.kind, account.person, login=driver.vendor_login, capability=account.capability)
    logins = {account.key: account.login} if account.login else None
    try:
        spec = harness.spec(driver, _seed(case.provider, account), logins=logins)
    except pydantic.ValidationError as refused:
        raise AssertionError(
            f"the shared seed model cannot declare a {account.kind.value} account: {refused}"
        ) from None
    steps = Steps(record_property)
    with harness.world(driver, {}, spec=spec) as world, harness.session(driver, world) as session:
        listed: list[PersonSeen] = []
        steps.run("listed", lambda: listed.append(_find(session.people(), account)))
        if driver.credential_of(world.view, account.key) is not None:
            steps.run("signs in", lambda: _signs_in(harness, driver, world, account))
        elif not absent(driver, "accounts.person_credentials", record_property):
            steps.broken.append("signs in: the driver gives the account no credential and declares no reason")
        if listed:
            steps.run(
                "assigned, messaged or shared with",
                lambda: _family_operation(session, driver, account, listed[0], record_property),
            )
        steps.run("acts through the control API", lambda: _act_through_control(world, case.provider, account))
        steps.run("deactivated", lambda: _deactivated(world, case.provider, account, session))
    assert not steps.broken, "\n".join(steps.broken)


def _signs_in(harness: Harness, driver: Driver, world: OpenWorld, account: Account) -> None:
    with harness.session(driver, world, person=account.key) as theirs:
        who = theirs.whoami().casefold()
        names = [account.name, account.email or "", account.login or "", account.key]
        assert any(n and n.casefold() in who for n in names), f"signed in as {account.key}, answered as {who!r}"


def _deactivated(world: OpenWorld, provider: str, account: Account, session: Session) -> None:
    try:
        world.deactivate_person(provider, account.key)
    except Unsupported as refused:
        assert provider in refused.error, refused.error
        return
    found = _find(session.people(), account)
    assert not found.active, f"deactivated, the API lists {found}"
