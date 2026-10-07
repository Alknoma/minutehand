"""Property 2 — family equivalence.

The same abstract lifecycle, driven through each provider of a family by its vendor's own API, leaves the same
NEUTRAL record: the sequence of changes (who, what operation, to which thing, with which neutral state), the things
named by the order they first appear. What is the vendor's own (ids, wire shapes, vendor-only records) is not
compared; a difference in neutral meaning is a failure.

Two checks per lifecycle. Against the RULE: the sequence the neutral model states (a ticket created open and
unassigned, assigned, commented, done, reopened, deleted; a message posted, replied to in its thread, edited,
deleted; a document created, written, renamed, moved, shared, trashed). Between PROVIDERS: each provider's whole
neutral sequence, every neutral change it recorded and not only those the rule names, equals the first provider's of
its family (alphabetically) that can drive the lifecycle, so two providers that disagree on what an unnamed step
means (a reaction, a link, the folder a move made, what trashing is) are told apart. A provider whose driver declares
a capability the lifecycle needs absent does not drive it, and the lifecycle is reported not applicable to it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from minutehand.domain.scenario import AccessRole
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    EntityKind,
    GrantSnapshot,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.testing.world import OpenWorld
from tests.conformance.contract import Documents, Driver, Family, Messaging, Progress, Property, Session, Tickets
from tests.conformance.harness import (
    Case,
    Harness,
    NoDriver,
    absent,
    cases,
    driver_of,
    in_family,
    parametrize,
    require,
    seed,
)

type Step = tuple[str, str, str, str, tuple[tuple[str, str], ...]]
"""(actor, operation, kind, thing, neutral fields): one neutral change."""


def _fields(after: object, names: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """The neutral fields of a snapshot, with every vendor id it holds replaced by the thing's name."""
    if isinstance(after, TicketSnapshot):
        return (("title", after.title), ("body", after.body.strip()), ("state", after.state.value),
                ("assignee", str(after.assignee_email)), ("project", str(after.project)))  # fmt: skip
    if isinstance(after, MessageSnapshot):
        return (
            ("text", after.text),
            ("thread_of", names.get(after.thread_of or "", str(after.thread_of))),
            ("recipients", ",".join(sorted(after.recipient_emails))),
        )
    if isinstance(after, DocumentSnapshot):
        return (("title", after.title), ("text", (after.text or "").strip()), ("parent", str(after.parent)),
                ("owner", str(after.owner)), ("space", str(after.space)), ("mime", str(after.mime_type)))  # fmt: skip
    if isinstance(after, GrantSnapshot):
        return (("document", after.document), ("to", after.to), ("role", after.role.value))
    return ()


def neutral_sequence(events: Sequence[WorldEvent], provider: str, since: int) -> list[Step]:
    """The world's changes after `since`, neutral: vendor-only records (`RecordSnapshot` and kinds outside the
    neutral ones) dropped, things named T1, T2... by first appearance, and an update that changes no neutral field of
    its thing dropped (it means nothing neutral)."""
    names: dict[str, str] = {}
    last: dict[str, tuple[tuple[str, str], ...]] = {}
    found: list[Step] = []
    for event in events:
        if event.seq <= since or event.entity.provider != provider:
            continue
        if event.operation in (Operation.READ, Operation.SEARCH) or isinstance(event.after, RecordSnapshot):
            continue
        if event.entity.kind in (EntityKind.RECORD, EntityKind.CHANNEL):
            continue
        thing = names.setdefault(event.entity.external_id, f"T{len(names) + 1}")
        fields = _fields(event.after, names)
        if event.operation is Operation.UPDATE and last.get(thing) == fields:
            continue
        last[thing] = fields
        found.append((event.actor.value, event.operation.value, event.entity.kind.value, thing, fields))
    return found


def _show(steps: Sequence[Step]) -> str:
    return "\n".join(f"  {a} {o} {k} {t} {dict(f)}" for a, o, k, t, f in steps)


# ---------------------------------------------------------------------------------------------- the lifecycles


@dataclass(frozen=True)
class Lifecycle:
    name: str
    family: Family
    drive: Callable[[Session, Driver, Callable[[str, object], None]], None]
    rule: Callable[[list[Step]], list[str]]
    """What the neutral model says the sequence must hold; each broken expectation as a sentence."""
    requires: tuple[str, ...] = ()
    """Capabilities every step of the lifecycle needs: a provider declaring one absent cannot drive it, so the
    lifecycle does not apply to it (reported) and it is no one's measure."""


def drives(provider: str, lifecycle: Lifecycle) -> bool:
    """Whether the provider's driver can drive the whole lifecycle; one with no driver is taken to, so its cases
    fail by name."""
    try:
        driver = driver_of(provider)
    except NoDriver:
        return True
    return not any(c in driver.absent for c in lifecycle.requires)


def drivers_of(lifecycle: Lifecycle) -> list[str]:
    """The providers of the lifecycle's family that drive it, alphabetically: the first is the family's measure."""
    return [p for p in in_family(lifecycle.family) if drives(p, lifecycle)]


def _sofia(session: Session) -> str:
    return session.person_id("sofia@example.com")


def _tickets(session: Session, driver: Driver, record: Callable[[str, object], None]) -> None:
    assert isinstance(session, Tickets)
    first = session.create_ticket("Launch", "Lifecycle A", "lifecycle body")
    session.assign(first, _sofia(session))
    session.comment(first, "lifecycle comment")
    if not absent(driver, "tickets.in_progress", record):
        session.set_progress(first, Progress.IN_PROGRESS)
    session.set_progress(first, Progress.DONE)
    session.set_progress(first, Progress.OPEN)
    second = session.create_ticket("Launch", "Lifecycle B", "")
    if not absent(driver, "tickets.link", record):
        session.link(first, second)
    if not absent(driver, "tickets.labels", record):
        session.relabel(first, ["lifecycle"])
    session.delete_ticket(first)
    left = session.ticket_ids()
    assert "Lifecycle A" not in left and "Lifecycle B" in left, f"the API still lists {sorted(left)}"


def _ticket_states(steps: list[Step]) -> list[tuple[str, str, str]]:
    return [
        (o, dict(f)["state"], dict(f)["assignee"])
        for _, o, k, t, f in steps
        if k == "ticket" and t == "T1" and o != "delete"
    ]


def _ticket_rule(steps: list[Step]) -> list[str]:
    broken: list[str] = []
    wanted = [
        ("create", "open", "None"),
        ("update", "open", "sofia@example.com"),
        ("update", "done", "sofia@example.com"),
        ("update", "open", "sofia@example.com"),
    ]
    held = _ticket_states(steps)
    if held != wanted:
        broken.append(f"ticket A's neutral states were {held}, the rule is {wanted}")
    if not any(o == "delete" and k == "ticket" and t == "T1" for _, o, k, t, _ in steps):
        broken.append("ticket A's deletion is not recorded as a delete")
    if [k for _, o, k, _, _ in steps if o == "create" and k == "comment"] != ["comment"]:
        broken.append("the comment is not recorded as one created comment")
    if not any(o == "create" and k == "ticket" and dict(f).get("title") == "Lifecycle B" for _, o, k, _, f in steps):
        broken.append("ticket B's creation is not recorded")
    if any(a != Actor.AGENT.value for a, *_ in steps):
        broken.append("a change the agent made is recorded as someone else's")
    return broken


def _messages(where: str) -> Callable[[Session, Driver, Callable[[str, object], None]], None]:
    def drive(session: Session, driver: Driver, record: Callable[[str, object], None]) -> None:
        assert isinstance(session, Messaging)
        channel = session.channel("launch") if where == "channel" else session.direct(_sofia(session))
        first = session.post(channel, "lifecycle hello")
        session.reply(channel, first, "lifecycle reply")
        session.edit(channel, first, "lifecycle hello, edited")
        if not absent(driver, "messaging.react", record):
            session.react(channel, first, "thumbsup")
        session.delete_message(channel, first)
        texts = [m.text for m in session.history(channel)]
        assert "lifecycle hello, edited" not in texts and "lifecycle hello" not in texts, texts

    return drive


def _message_rule(steps: list[Step]) -> list[str]:
    broken: list[str] = []
    shape = [(o, k, t, dict(f).get("text"), dict(f).get("thread_of")) for _, o, k, t, f in steps if k == "message"]
    wanted_head = [
        ("create", "message", "T1", "lifecycle hello", "None"),
        ("create", "message", "T2", "lifecycle reply", "T1"),
        ("update", "message", "T1", "lifecycle hello, edited", "None"),
    ]
    if shape[:3] != wanted_head:
        broken.append(f"the messages' neutral sequence begins {shape[:3]}, the rule is {wanted_head}")
    if not any(o == "delete" and t == "T1" for o, _, t, _, _ in shape):
        broken.append("the deleted message is not recorded as a delete")
    if any(a != Actor.AGENT.value for a, *_ in steps):
        broken.append("a change the agent made is recorded as someone else's")
    return broken


def _documents(session: Session, driver: Driver, record: Callable[[str, object], None]) -> None:
    assert isinstance(session, Documents)
    made = session.create_document("Lifecycle doc", None)
    session.write(made, "lifecycle text")
    assert "lifecycle text" in session.read_text(made)
    session.rename(made, "Lifecycle doc renamed")
    if not absent(driver, "documents.folders", record):
        session.move(made, "Lifecycle folder")
    if not absent(driver, "documents.sharing", record):
        session.share(made, "sofia@example.com", AccessRole.WRITER)
    session.trash(made)
    assert made not in [d.id for d in session.documents()], "a trashed document is still listed"


def _document_rule(steps: list[Step]) -> list[str]:
    broken: list[str] = []
    doc = [(o, dict(f)) for _, o, k, t, f in steps if k == "document" and t == "T1"]
    if not doc or doc[0][0] != "create" or doc[0][1].get("title") != "Lifecycle doc":
        broken.append(f"the document's first neutral change is {doc[:1]}, the rule is its creation")
    if not any(f.get("text") == "lifecycle text" for _, f in doc):
        broken.append("no neutral change holds the text written")
    if not any(f.get("title") == "Lifecycle doc renamed" for _, f in doc):
        broken.append("no neutral change holds the new title")
    if not any(f.get("parent") == "Lifecycle folder" for _, f in doc):
        broken.append("no neutral change puts the document in the folder it was moved to")
    wanted_grant = {"document": "Lifecycle doc renamed", "to": "sofia@example.com", "role": "writer"}
    if any(k == "grant" for _, _, k, _, _ in steps) and not any(
        k == "grant" and dict(f) == wanted_grant for _, _, k, _, f in steps
    ):
        broken.append(f"no neutral grant {wanted_grant}")
    if not doc or doc[-1][0] != "delete":
        broken.append(f"trashing it is recorded as {doc[-1:]}, the rule is a delete: it is gone from where it was")
    if any(a != Actor.AGENT.value for a, *_ in steps):
        broken.append("a change the agent made is recorded as someone else's")
    return broken


LIFECYCLES = {
    lc.name: lc
    for lc in [
        Lifecycle("tickets", Family.TICKETS, _tickets, _ticket_rule),
        Lifecycle(
            "messages_in_a_channel",
            Family.MESSAGING,
            _messages("channel"),
            _message_rule,
            ("messaging.channels", "messaging.edit"),
        ),
        Lifecycle(
            "messages_in_a_direct_conversation",
            Family.MESSAGING,
            _messages("direct"),
            _message_rule,
            ("messaging.direct", "messaging.edit"),
        ),
        Lifecycle("documents", Family.DOCUMENTS, _documents, _document_rule),
    ]
}


def run(
    harness: Harness, case_provider: str, lifecycle: Lifecycle, record: Callable[[str, object], None]
) -> list[Step]:
    driver = require(case_provider, lifecycle.family)
    with harness.world(driver, seed(case_provider)) as world:
        since = world.view.head
        with harness.session(driver, world) as session:
            lifecycle.drive(session, driver, record)
        steps = neutral_sequence(world.events(), case_provider, since)
        _same_clock(world, steps)
        return steps


def _same_clock(world: OpenWorld, steps: list[Step]) -> None:
    now = world.now()
    off = [e for e in world.events() if e.seq > world.view.head and e.sim_time != now]
    assert not off, f"changes recorded off the world's clock {now}: {[(e.seq, e.sim_time) for e in off][:5]}"


@parametrize([c for lc in LIFECYCLES.values() for c in cases(Property.FAMILY, lc.family, [lc.name])])
def test_a_lifecycle_leaves_the_neutral_record_the_rule_states(
    case: Case, harness: Harness, record_property: Callable[[str, object], None]
) -> None:
    """Driven through the vendor's API, the lifecycle's neutral record holds what the neutral model says it means,
    every change the agent's, at the world's clock."""
    lifecycle = LIFECYCLES[case.case]
    driver = require(case.provider, lifecycle.family)
    if any(absent(driver, c, record_property) for c in lifecycle.requires):
        return
    steps = run(harness, case.provider, lifecycle, record_property)
    broken = lifecycle.rule(steps)
    assert not broken, "\n".join(broken) + "\nthe neutral record:\n" + _show(steps)


PAIRS = [
    Case(other, Property.FAMILY, f"same_as_{first}_{lc.name}")
    for lc in LIFECYCLES.values()
    for first, *others in [drivers_of(lc)]
    for other in others
]


@parametrize(PAIRS)
def test_every_provider_of_a_family_leaves_the_same_neutral_sequence_as_the_first(
    case: Case, harness: Harness, record_property: Callable[[str, object], None]
) -> None:
    """The whole neutral sequence one provider leaves equals the first provider's of its family for the same
    lifecycle: what an unnamed step means (a reaction, a link, a folder made, a trash) is the same everywhere."""
    name = next(n for n in LIFECYCLES if case.case.endswith(f"_{n}"))
    lifecycle = LIFECYCLES[name]
    first = drivers_of(lifecycle)[0]
    try:
        theirs = run(harness, first, lifecycle, record_property)
    except Exception as failed:
        raise AssertionError(
            f"{first} cannot complete the lifecycle, so {case.provider} cannot be compared with it: {failed}"
        ) from None
    mine = run(harness, case.provider, lifecycle, record_property)
    differ = next(
        (n for n, (a, b) in enumerate(zip(mine, theirs, strict=False)) if a != b), min(len(mine), len(theirs))
    )
    assert mine == theirs, (
        f"the neutral sequences part at step {differ + 1}: {case.provider} {mine[differ : differ + 1]}, "
        f"{first} {theirs[differ : differ + 1]}\n{case.provider}:\n{_show(mine)}\n{first}:\n{_show(theirs)}"
    )
