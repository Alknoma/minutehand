"""Property 9 — faults.

Every fault kind a provider declares can be declared on a live world (`POST /provider-faults`), produces the vendor's
documented error status and shape through the vendor's REAL client library (which raises its own typed error where
it has one), is consumed after the calls it declares (or expires after the time it declares), and is recorded in the
world's calls. A fault armed on any provider through `POST /faults` answers the next matching call with its status,
is recorded, and is consumed.
"""

from __future__ import annotations

from collections.abc import Callable

from minutehand.adapters.control.wire import Fault
from tests.conformance.contract import Family, FaultCase, Property, VendorRefused
from tests.conformance.harness import PROVIDERS, Case, Harness, NoDriver, cases, driver_of, parametrize, require, seed

type Record = Callable[[str, object], None]


def _fault_names(provider: str) -> list[str]:
    try:
        return [f.name for f in driver_of(provider).faults()]
    except NoDriver:
        return ["declared_faults"]


FAULT_CASES = [Case(p, Property.FAULTS, f"declared_{name}") for p in PROVIDERS for name in _fault_names(p)]


def _fault(provider: str, name: str) -> FaultCase:
    found = [f for f in require(provider, Family.ACCOUNTS).faults() if f"declared_{f.name}" == name]
    assert found, f"{provider}'s driver declares no fault case {name}"
    return found[0]


@parametrize(FAULT_CASES)
def test_a_declared_fault_answers_as_the_vendor_documents_through_its_client_and_is_consumed_and_recorded(
    case: Case, harness: Harness
) -> None:
    fault = _fault(case.provider, case.case)
    driver = require(case.provider, Family.ACCOUNTS)
    with harness.world(driver, seed(case.provider)) as world:
        world.declare_faults(case.provider, dict(fault.fragment))
        held = len(world.calls())
        broken: list[str] = []
        for n in range(fault.uses):
            raised = fault.trigger(harness.api, world.view)
            if raised is None:
                broken.append(f"faulted call {n + 1} of {fault.uses} raised nothing")
            elif fault.typed is not None and not isinstance(raised, fault.typed):
                broken.append(
                    f"faulted call {n + 1} raised {type(raised).__name__}, not {fault.typed.__name__}: {raised}"
                )
        recorded = world.calls()[held:]
        faulted = [
            c for c in recorded if c.exchange.status == fault.status and fault.holds in (c.exchange.response_body or "")
        ]
        if len(faulted) < fault.uses:
            shown = [(c.exchange.status, (c.exchange.response_body or "")[:80]) for c in recorded]
            broken.append(
                f"the world recorded {len(faulted)} answers of {fault.status} holding {fault.holds!r}, not "
                f"{fault.uses}: {shown}"
            )
        if fault.expires_after is not None:
            world.advance(fault.expires_after)
        if fault.then is not None:
            try:
                fault.then(harness.api, world.view)
            except Exception as still:
                broken.append(f"the fault was not consumed: the same call afterwards raised {still}")
        else:
            broken.append("the driver gives no call to show the fault is consumed")
    assert not broken, "\n".join(broken)


@parametrize(cases(Property.FAULTS, Family.ACCOUNTS, ["armed_fault_answers_once_and_is_recorded"]))
def test_a_fault_armed_through_the_control_api_answers_the_next_call_is_recorded_and_consumed(
    case: Case, harness: Harness
) -> None:
    driver = require(case.provider, Family.ACCOUNTS)
    with harness.world(driver, seed(case.provider)) as world, harness.session(driver, world) as session:
        held = len(world.calls())
        world.arm(Fault(provider=case.provider, status=503, body='{"error": "armed"}', times=1))
        try:
            session.observe()
        except VendorRefused as refused:
            assert refused.status == 503, refused
        else:
            raise AssertionError("the armed fault answered nothing: the next call succeeded")
        assert 503 in [c.exchange.status for c in world.calls()[held:]], "the armed fault is not in the record"
        session.observe()


def test_every_provider_that_declares_faults_has_its_fault_cases_in_its_driver(harness: Harness) -> None:
    """`GET /v1/providers` says which providers declare typed faults; each one's driver must exercise them."""
    missing = [
        p.key
        for p in harness.client.providers()
        if p.faults and p.key in PROVIDERS and _fault_names(p.key) in ([], ["declared_faults"])
    ]
    assert not missing, f"providers declaring faults whose drivers exercise none: {missing}"
