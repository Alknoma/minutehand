"""Every installed provider has a conformance driver that drives every family its manifest puts it in, and every
vendor exception a driver declares names a known capability and gives a reason. A provider merged without a driver
fails here, by name, so a new provider cannot be merged untested."""

from __future__ import annotations

import pytest

from tests.conformance.contract import CAPABILITIES, Family, IdKind
from tests.conformance.harness import MANIFESTS, PROVIDERS, driver_of, families, require

PAIRS = [(p, f) for p in PROVIDERS for f in families(MANIFESTS[p])]


@pytest.mark.parametrize(("provider", "family"), PAIRS, ids=[f"{p}-{f.value}" for p, f in PAIRS])
def test_every_provider_has_a_driver_for_every_family_its_manifest_puts_it_in(provider: str, family: Family) -> None:
    """A provider is in the accounts family, and in the tickets, messaging or documents family for each entity kind
    its manifest maps; its driver's session implements each."""
    require(provider, family)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_declared_vendor_exception_names_a_known_capability_and_a_reason(provider: str) -> None:
    """`Driver.absent` is the only way a property does not apply, so each entry must be a capability the suite
    knows and say why the vendor has no such thing."""
    driver = driver_of(provider)
    unknown = sorted(set(driver.absent) - set(CAPABILITIES))
    assert not unknown, f"{provider} declares capabilities the suite does not know: {unknown}"
    bare = sorted(k for k, reason in driver.absent.items() if len(reason.split()) < 4)
    assert not bare, f"{provider} declares these absent without a reason: {bare}"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_driver_states_the_id_format_of_each_kind_its_families_hold(provider: str) -> None:
    """Property 5 checks ids against the vendor's documented format, so the driver must state one for each kind of
    thing its families hold."""
    driver = driver_of(provider)
    held = families(MANIFESTS[provider])
    wanted = [] if "accounts.people" in driver.absent else [IdKind.PERSON]
    wanted += [IdKind.TICKET] if Family.TICKETS in held else []
    if Family.MESSAGING in held:
        wanted += [IdKind.MESSAGE] if "messaging.channels" in driver.absent else [IdKind.CHANNEL, IdKind.MESSAGE]
    wanted += [IdKind.DOCUMENT] if Family.DOCUMENTS in held else []
    missing = [k.value for k in wanted if k not in driver.id_formats]
    assert not missing, f"{provider}'s driver states no id format for {missing}"
