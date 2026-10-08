"""Property 6 — the record is complete and verbatim.

Every request a driver makes through the vendor's API appears exactly once in the world's calls, in order, with the
method, host and path it was sent with and a body byte-identical to what was sent and to what was answered
(a binary upload and download included, where the vendor has them), with every credential absent from the stored
bytes (the one place they may differ from what crossed the wire is where a credential was). A request with a
credential nobody seeded, naming no world, is answered with the vendor's documented refusal and is kept in the
unclaimed list, in no world.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from minutehand.domain.world import RecordedCall
from tests.conformance.contract import Documents, Driver, Family, Messaging, Property, Sent, Session, Tickets
from tests.conformance.harness import Case, Harness, absent, cases, parametrize, require, seed

type Record = Callable[[str, object], None]

BINARY = bytes(range(256)) * 8 + b"\x00\xff\xfe end"


def _script(session: Session, driver: Driver, record: Record) -> None:
    """A little of everything the session's families do: reads, creates, updates."""
    if not absent(driver, "accounts.whoami", record):
        session.whoami()
    if "accounts.people" not in driver.absent:
        session.people()
    else:
        session.observe()
    if isinstance(session, Tickets):
        made = session.create_ticket("Launch", "Recorded ticket", "recorded body ünïcode")
        session.comment(made, "recorded comment")
        session.read_ticket(made)
    if isinstance(session, Messaging):
        channel = (
            session.direct(session.person_id("sofia@example.com"))
            if absent(driver, "messaging.channels", record)
            else session.channel("launch")
        )
        session.post(channel, "recorded message ünïcode")
        session.history(channel)
    if isinstance(session, Documents):
        made = session.create_document("Recorded doc", None)
        session.write(made, "recorded text ünïcode")
        session.read_text(made)


def _stored(call: RecordedCall, side: str) -> bytes:
    exchange = call.exchange
    text = exchange.request_body if side == "request" else exchange.response_body
    raw = exchange.request_bytes if side == "request" else exchange.response_bytes
    if text is not None:
        return text.encode()
    return raw or b""


def _same(sent: bytes, stored: bytes, secrets: Sequence[bytes]) -> bool:
    """Byte-identical, except that where `sent` held a credential `stored` may hold anything that is not it."""
    if any(s and s in stored for s in secrets):
        return False
    held = [s for s in secrets if s and s in sent]
    if not held:
        return sent == stored
    pattern = b"|".join(re.escape(s) for s in sorted(held, key=len, reverse=True))
    pieces = re.split(pattern, sent)
    return re.fullmatch(b".*?".join(re.escape(p) for p in pieces), stored, re.DOTALL) is not None


def _compare(sent: Sequence[Sent], calls: Sequence[RecordedCall], secrets: Sequence[str]) -> list[str]:
    wanted = [s.encode() for s in secrets]
    broken: list[str] = []
    if len(sent) != len(calls):
        broken.append(
            f"{len(sent)} requests were sent and the world recorded {len(calls)} calls:\n"
            + "\n".join(f"  sent {s.method} {s.host}{s.path}" for s in sent)
            + "\n"
            + "\n".join(f"  kept {c.exchange.method} {c.exchange.host}{c.exchange.path}" for c in calls)
        )
    for n, (one, call) in enumerate(zip(sent, calls, strict=False)):
        kept = call.exchange
        where = f"call {n + 1} ({one.method} {one.host}{one.path[:80]})"
        if (kept.method, kept.host) != (one.method, one.host) or not _same(
            one.path.encode(), kept.path.encode(), wanted
        ):
            broken.append(f"{where}: kept as {kept.method} {kept.host}{kept.path[:80]}")
        if kept.status != one.status:
            broken.append(f"{where}: answered {one.status}, kept as {kept.status}")
        if not _same(one.request, _stored(call, "request"), wanted):
            broken.append(f"{where}: the request body kept differs from what was sent ({len(one.request)} bytes sent)")
        if not _same(one.response, _stored(call, "response"), wanted):
            broken.append(f"{where}: the answer kept differs from what was answered ({len(one.response)} bytes)")
    return broken


@parametrize(cases(Property.RECORD, Family.ACCOUNTS, ["every_call_kept_once_in_order_verbatim"]))
def test_every_request_the_driver_makes_is_kept_once_in_order_and_byte_for_byte(
    case: Case, harness: Harness, record_property: Record
) -> None:
    driver = require(case.provider, Family.ACCOUNTS)
    with harness.world(driver, seed(case.provider)) as world:
        mark = harness.api.recorder.mark()
        with harness.session(driver, world) as session:
            _script(session, driver, record_property)
            secrets = session.secrets
        broken = _compare(harness.api.recorder.since(mark), world.calls(), secrets)
    assert not broken, "\n".join(broken)


@parametrize(cases(Property.RECORD, Family.DOCUMENTS, ["binary_upload_and_download"]))
def test_a_binary_upload_and_download_are_kept_as_the_bytes_that_crossed(
    case: Case, harness: Harness, record_property: Record
) -> None:
    driver = require(case.provider, Family.DOCUMENTS)
    if absent(driver, "documents.binary", record_property):
        return
    with harness.world(driver, seed(case.provider)) as world, harness.session(driver, world) as session:
        assert isinstance(session, Documents)
        mark = harness.api.recorder.mark()
        made = session.upload("conformance.bin", BINARY, "application/octet-stream")
        assert session.download(made) == BINARY, "the download is not the bytes uploaded"
        sent = harness.api.recorder.since(mark)
        calls = world.calls()[-len(sent) :]
        broken = _compare(sent, calls, session.secrets)
    assert not broken, "\n".join(broken)
