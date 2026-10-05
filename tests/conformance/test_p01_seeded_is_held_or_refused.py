"""Property 1 — seeded is held or refused.

For every field of the shared seed models, seeded with a distinctive value, either the value is observable through
the vendor's own API (and, where the neutral snapshot has a field for it, in the world's neutral view), or creating
the world is refused with a message naming the field and the provider. Never accepted and absent.

People have no neutral snapshot, and labels, comments, channel settings and files have no neutral field: for those
the vendor's API is the only witness. `SeededTicket` has no parent field, so a ticket's parent is not seedable at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from minutehand.domain.scenario import AccessRole, DocumentKind, TicketState
from minutehand.domain.world import WorldEvent
from tests.conformance.contract import (
    ChannelSeen,
    CommentSeen,
    Documents,
    Driver,
    Family,
    Messaging,
    PersonSeen,
    Progress,
    Property,
    Session,
    Tickets,
)
from tests.conformance.harness import Case, Harness, absent, cases, parametrize, require, seed
from tests.conformance.neutral import document_titled, grants, merged, messages, opened_or_refused, ticket_titled

START = datetime(2026, 9, 1, 9, tzinfo=UTC)

type Check = Callable[[Session, Sequence[WorldEvent], str], None]


@dataclass(frozen=True)
class Fact:
    name: str
    family: Family
    names: tuple[str, ...]
    """What a refusal must name besides the provider: the field."""
    capabilities: tuple[str, ...]
    """A driver declaring any of these absent says the vendor has no such thing."""
    seed: Callable[[str], dict[str, object]]
    check: Check


def _person(people: list[PersonSeen], name: str) -> PersonSeen:
    found = [p for p in people if p.name == name]
    assert len(found) == 1, f"the vendor lists {len(found)} accounts named {name!r}: {[p.name for p in people]}"
    return found[0]


def _people(*more: dict[str, object]) -> Callable[[str], dict[str, object]]:
    return lambda provider: {"people": [dict(p) | {"reply": {"kind": "silent"}} for p in more]}


def _listed(name: str, holds: Callable[[PersonSeen], bool], what: str) -> Check:
    def check(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
        found = _person(session.people(), name)
        shown = f"email={found.email} active={found.active} bot={found.bot} guest={found.guest} title={found.title}"
        assert holds(found), f"{name!r} was seeded {what}; the vendor lists {shown}"

    return check


PEOPLE_FACTS = [
    Fact(
        "person_name_and_email",
        Family.ACCOUNTS,
        ("email",),
        ("accounts.people",),
        _people({"key": "zelda", "name": "Zelda Quintero-Vance", "email": "zelda.qv+seed@example.org"}),
        _listed("Zelda Quintero-Vance", lambda p: p.email == "zelda.qv+seed@example.org", "with her email"),
    ),
    Fact(
        "person_title",
        Family.ACCOUNTS,
        ("title",),
        ("accounts.people", "accounts.title"),
        _people({"key": "tara", "name": "Tara Title", "email": "tara@example.com", "title": "Chief Ferret Officer"}),
        _listed("Tara Title", lambda p: p.title == "Chief Ferret Officer", "with the title Chief Ferret Officer"),
    ),
    Fact(
        "person_without_email",
        Family.ACCOUNTS,
        ("email",),
        ("accounts.people", "accounts.no_email"),
        _people({"key": "nemo", "name": "Nemo Noemail"}),
        _listed("Nemo Noemail", lambda p: p.email is None, "with no email"),
    ),
    Fact(
        "person_bot",
        Family.ACCOUNTS,
        ("bot",),
        ("accounts.people", "accounts.bot"),
        _people({"key": "robo", "name": "Robo Helper", "email": "robo@example.com", "account": "bot"}),
        _listed("Robo Helper", lambda p: p.bot, "as a bot"),
    ),
    Fact(
        "person_deactivated",
        Family.ACCOUNTS,
        ("deactivated",),
        ("accounts.people", "accounts.deactivated"),
        _people({"key": "gone", "name": "Dee Activated", "email": "dee@example.com", "account": "deactivated"}),
        _listed("Dee Activated", lambda p: not p.active, "deactivated"),
    ),
    Fact(
        "person_guest",
        Family.ACCOUNTS,
        ("guest",),
        ("accounts.people", "accounts.guest"),
        _people({"key": "gus", "name": "Gus Guest", "email": "gus@example.com", "account": "guest"}),
        _listed("Gus Guest", lambda p: p.guest, "as a guest"),
    ),
]


# ---------------------------------------------------------------------------------------------- tickets

TITLE = "Fact: the seeded ticket"


def _ticket(**fields: object) -> Callable[[str], dict[str, object]]:
    return lambda provider: {"tickets": [{"provider": provider, "project": "Launch", "title": TITLE} | fields]}


def _read(session: Session) -> tuple[Tickets, str]:
    assert isinstance(session, Tickets)
    ids = session.ticket_ids()
    assert TITLE in ids, f"the tracker's API does not reach the seeded ticket {TITLE!r}: {sorted(ids)}"
    return session, ids[TITLE]


def _title_body_project(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    tickets, ticket = _read(session)
    seen = tickets.read_ticket(ticket)
    assert (seen.title, seen.body.strip(), seen.project) == (TITLE, BODY, "Launch"), (
        f"the API shows title {seen.title!r}, body {seen.body!r}, project {seen.project!r}"
    )
    neutral = ticket_titled(events, provider, TITLE)
    assert (neutral.body.strip(), neutral.project) == (BODY, "Launch"), (
        f"the neutral view holds body {neutral.body!r}, project {neutral.project!r}; seeded in project 'Launch'"
    )


BODY = "First line, ünïcode.\nSecond line with <angle brackets> & ampersand."


def _assignee(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    tickets, ticket = _read(session)
    assert tickets.read_ticket(ticket).assignee_email == "mila@example.com"
    assert ticket_titled(events, provider, TITLE).assignee_email == "mila@example.com"


def _state(progress: Progress, state: TicketState) -> Check:
    def check(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
        tickets, ticket = _read(session)
        assert tickets.read_ticket(ticket).progress is progress
        assert ticket_titled(events, provider, TITLE).state is state

    return check


def _labels(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    tickets, ticket = _read(session)
    seen = tickets.read_ticket(ticket).labels
    assert {"fact-label-a", "fact-label-b"} <= seen, f"seeded labels fact-label-a, fact-label-b; the API shows {seen}"


def _comments(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    tickets, ticket = _read(session)
    seen = tickets.read_ticket(ticket).comments
    assert CommentSeen("mila@example.com", "Fact comment by Mila") in seen, f"the API shows the comments {seen}"


def _present(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    _read(session)


TICKET_FACTS = [
    Fact("ticket_title_body_project", Family.TICKETS, ("body",), (), _ticket(body=BODY), _title_body_project),
    Fact("ticket_assignee", Family.TICKETS, ("assignee",), (), _ticket(assignee="mila"), _assignee),
    Fact("ticket_done", Family.TICKETS, ("state",), (), _ticket(state="done"), _state(Progress.DONE, TicketState.DONE)),
    Fact(
        "ticket_cancelled",
        Family.TICKETS,
        ("state",),
        ("tickets.cancelled",),
        _ticket(state="cancelled"),
        _state(Progress.CANCELLED, TicketState.CANCELLED),
    ),
    Fact(
        "ticket_labels",
        Family.TICKETS,
        ("labels",),
        ("tickets.labels",),
        _ticket(labels=["fact-label-a", "fact-label-b"]),
        _labels,
    ),
    Fact(
        "ticket_comments",
        Family.TICKETS,
        ("comments",),
        (),
        _ticket(comments=[{"by": "mila", "text": "Fact comment by Mila"}]),
        _comments,
    ),
    Fact("ticket_key", Family.TICKETS, ("key",), (), _ticket(key="factkey"), _present),
]


# ---------------------------------------------------------------------------------------------- documents

DOC = "Fact document"
READER = [{"person": "owen", "role": "reader"}]


def _document(**fields: object) -> Callable[[str], dict[str, object]]:
    return lambda provider: {"documents": [{"provider": provider, "title": DOC} | fields]}


def _seen_document(session: Session) -> tuple[Documents, str]:
    assert isinstance(session, Documents)
    found = [d for d in session.documents() if d.title == DOC]
    assert len(found) == 1, f"the API reaches {len(found)} documents titled {DOC!r}"
    return session, found[0].id


def _text(*parts: str) -> Check:
    def check(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
        documents, document = _seen_document(session)
        text = documents.read_text(document)
        missing = [p for p in parts if p not in text]
        assert not missing, f"the API's text of {DOC!r} lacks {missing}: {text[:300]!r}"
        neutral = document_titled(events, provider, DOC).text or ""
        missing = [p for p in parts if p not in neutral]
        assert not missing, f"the world's neutral text of {DOC!r} lacks {missing}: {neutral[:300]!r}"

    return check


def _kind(kind: DocumentKind, *parts: str, mime: str | None = None) -> Check:
    def check(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
        documents, document = _seen_document(session)
        seen = next(d for d in documents.documents() if d.id == document)
        assert seen.kind is kind, f"{DOC!r} was seeded a {kind.value}; the API shows a {seen.kind} ({seen.mime_type})"
        if mime is not None:
            assert seen.mime_type == mime, f"seeded {mime}, the API shows {seen.mime_type}"
        _text(*parts)(session, events, provider)

    return check


def _folder(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    documents, document = _seen_document(session)
    seen = next(d for d in documents.documents() if d.id == document)
    assert seen.folder == "Fact/Nested", f"seeded in Fact/Nested, the API shows the folder {seen.folder!r}"
    parent = document_titled(events, provider, DOC).parent
    assert parent == "Nested", f"seeded in Fact/Nested, the neutral view's parent is {parent!r}"


def _owner(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    documents, document = _seen_document(session)
    seen = next(d for d in documents.documents() if d.id == document)
    assert seen.owner_email == "sofia@example.com", f"seeded owned by sofia, the API shows owner {seen.owner_email}"
    owner = document_titled(events, provider, DOC).owner
    assert owner == "sofia@example.com", f"seeded owned by sofia, the neutral view's owner is {owner}"


def _shared(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    documents, document = _seen_document(session)
    seen = next(d for d in documents.documents() if d.id == document)
    shared = {k: v.value for k, v in seen.shared.items()}
    assert dict(seen.shared).get("mila@example.com") is AccessRole.COMMENTER, (
        f"seeded mila commenter; the API: {shared}"
    )
    held = [g for g in grants(events, provider) if g.to == "mila@example.com" and g.document == DOC]
    assert [g.role for g in held] == [AccessRole.COMMENTER], f"the neutral grants for mila: {held}"


def _last_editor(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    documents, document = _seen_document(session)
    seen = next(d for d in documents.documents() if d.id == document)
    assert (seen.last_editor_email, seen.modified) == ("mila@example.com", START - timedelta(days=3)), (
        f"seeded last changed by mila 3 days before the start; the API: {seen.last_editor_email} at {seen.modified}"
    )
    neutral = document_titled(events, provider, DOC)
    assert (neutral.last_edited_by, neutral.last_edited_at) == ("mila@example.com", START - timedelta(days=3))


def _space(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    documents, document = _seen_document(session)
    seen = next(d for d in documents.documents() if d.id == document)
    assert seen.space == "Fact Space", f"seeded in Fact Space, the API shows the space {seen.space!r}"
    space = document_titled(events, provider, DOC).space
    assert space == "Fact Space", f"seeded in Fact Space, the neutral view's space is {space!r}"


def _in_space(provider: str) -> dict[str, object]:
    return {
        "spaces": [{"provider": provider, "name": "Fact Space", "members": [{"person": "owen", "role": "organizer"}]}],
        "documents": [{"provider": provider, "title": DOC, "text": "in a space", "space": "Fact Space"}],
    }


DOCUMENT_FACTS = [
    Fact(
        "document_text",
        Family.DOCUMENTS,
        ("text",),
        (),
        _document(text="Fact paragraph one.\n\nFact paragraph two."),
        _text("Fact paragraph one.", "Fact paragraph two."),
    ),
    Fact(
        "document_spreadsheet",
        Family.DOCUMENTS,
        ("spreadsheet",),
        ("documents.spreadsheet",),
        _document(kind="spreadsheet", rows=[["fact-a", "fact-b"], ["1", "2"]]),
        _kind(DocumentKind.SPREADSHEET, "fact-a", "fact-b"),
    ),
    Fact(
        "document_presentation",
        Family.DOCUMENTS,
        ("presentation",),
        ("documents.presentation",),
        _document(kind="presentation", text="Slide one\n\nSlide two"),
        _kind(DocumentKind.PRESENTATION, "Slide one", "Slide two"),
    ),
    Fact(
        "document_file",
        Family.DOCUMENTS,
        ("file",),
        ("documents.file",),
        _document(kind="file", mime_type="text/csv", text="fact,csv\n1,2"),
        _kind(DocumentKind.FILE, "fact,csv", mime="text/csv"),
    ),
    Fact(
        "document_folder",
        Family.DOCUMENTS,
        ("folder",),
        ("documents.folders",),
        _document(text="filed", folder="Fact/Nested"),
        _folder,
    ),
    Fact(
        "document_owner",
        Family.DOCUMENTS,
        ("owner",),
        ("documents.owner",),
        _document(text="hers", owner="sofia", shared_with=READER),
        _owner,
    ),
    Fact(
        "document_sharing",
        Family.DOCUMENTS,
        ("shared_with",),
        ("documents.sharing",),
        _document(text="shared", shared_with=[{"person": "mila", "role": "commenter"}]),
        _shared,
    ),
    Fact(
        "document_last_editor",
        Family.DOCUMENTS,
        ("modified_by",),
        ("documents.last_editor",),
        _document(text="edited", modified_by="mila", modified_before_start="P3D", shared_with=READER),
        _last_editor,
    ),
    Fact("document_space", Family.DOCUMENTS, ("space",), ("documents.spaces",), _in_space, _space),
]


# ---------------------------------------------------------------------------------------------- messaging


def _channel(**fields: object) -> Callable[[str], dict[str, object]]:
    return lambda provider: {
        "channels": [{"provider": provider, "name": "fact-channel", "members": ["owen", "sofia"]} | fields]
    }


def _setting(what: str, holds: Callable[[ChannelSeen], bool]) -> Check:
    def check(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
        assert isinstance(session, Messaging)
        found = [c for c in session.channels() if c.name == "fact-channel"]
        assert len(found) == 1, f"the API lists {len(found)} channels named fact-channel"
        seen = found[0]
        shown = f"private={seen.private} archived={seen.archived} topic={seen.topic!r} purpose={seen.purpose!r}"
        assert holds(seen), f"fact-channel was seeded {what}; the API shows {shown}, {sorted(seen.member_emails)}"

    return check


def _history(
    author: str, text: str, ago: timedelta, *, thread: str | None = None, files: tuple[str, ...] = ()
) -> Check:
    def check(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
        assert isinstance(session, Messaging)
        history = session.history(session.channel("fact-channel"))
        found = [m for m in history if m.text == text]
        assert len(found) == 1, f"the history holds {len(found)} messages {text!r}: {[m.text for m in history]}"
        seen = found[0]
        assert (seen.author_email, seen.at) == (author, START - ago), (
            f"seeded by {author} at {START - ago}; the API shows {seen.author_email} at {seen.at}"
        )
        if thread is not None:
            parent = next(m for m in history if m.text == thread)
            assert seen.thread_of == parent.id, f"{text!r} is not in the thread of {thread!r}: {seen}"
        assert set(files) <= set(seen.files), f"seeded with {files}, the API shows {seen.files}"
        assert text in [m.text for m in messages(events, provider).values()], "absent from the neutral view"

    return check


def _direct(session: Session, events: Sequence[WorldEvent], provider: str) -> None:
    assert isinstance(session, Messaging)
    sofia = next(p for p in session.people() if p.email == "sofia@example.com")
    history = session.history(session.direct(sofia.id))
    assert "Fact direct message" in [m.text for m in history], [m.text for m in history]


POST = {"by": "sofia", "text": "Fact post", "ago": "PT2H"}

MESSAGING_FACTS = [
    Fact("channel_name", Family.MESSAGING, ("channel",), (), _channel(), _setting("named", lambda c: True)),
    Fact(
        "channel_private",
        Family.MESSAGING,
        ("private",),
        ("messaging.private",),
        _channel(private=True),
        _setting("private", lambda c: c.private),
    ),
    Fact(
        "channel_archived",
        Family.MESSAGING,
        ("archived",),
        ("messaging.archived",),
        _channel(archived=True),
        _setting("archived", lambda c: c.archived),
    ),
    Fact(
        "channel_topic",
        Family.MESSAGING,
        ("topic",),
        ("messaging.topic",),
        _channel(topic="Fact topic"),
        _setting("with a topic", lambda c: c.topic == "Fact topic"),
    ),
    Fact(
        "channel_purpose",
        Family.MESSAGING,
        ("purpose",),
        ("messaging.purpose",),
        _channel(purpose="Fact purpose"),
        _setting("with a purpose", lambda c: c.purpose == "Fact purpose"),
    ),
    Fact(
        "channel_members",
        Family.MESSAGING,
        ("members",),
        (),
        _channel(members=["sofia", "mila"]),
        _setting(
            "with sofia and mila",
            lambda c: (
                {"sofia@example.com", "mila@example.com"} <= c.member_emails
                and "owen@example.com" not in c.member_emails
            ),
        ),
    ),
    Fact(
        "history_post",
        Family.MESSAGING,
        ("history",),
        (),
        _channel(history=[POST]),
        _history("sofia@example.com", "Fact post", timedelta(hours=2)),
    ),
    Fact(
        "history_thread_reply",
        Family.MESSAGING,
        ("replies",),
        (),
        _channel(history=[POST | {"replies": [{"by": "owen", "text": "Fact reply", "ago": "PT1H"}]}]),
        _history("owen@example.com", "Fact reply", timedelta(hours=1), thread="Fact post"),
    ),
    Fact(
        "history_files",
        Family.MESSAGING,
        ("files",),
        ("messaging.files",),
        _channel(history=[POST | {"files": [{"name": "fact.txt", "text": "fact file"}]}]),
        _history("sofia@example.com", "Fact post", timedelta(hours=2), files=("fact.txt",)),
    ),
    Fact(
        "direct_history",
        Family.MESSAGING,
        ("history",),
        ("messaging.direct",),
        lambda provider: {
            "channels": [
                {
                    "provider": provider,
                    "members": ["sofia"],
                    "history": [{"by": "sofia", "text": "Fact direct message", "ago": "PT1H"}],
                }
            ]
        },
        _direct,
    ),
]

FACTS = {f.name: f for f in [*PEOPLE_FACTS, *TICKET_FACTS, *DOCUMENT_FACTS, *MESSAGING_FACTS]}
CASES: list[Case] = [
    *(c for f in FACTS.values() for c in cases(Property.SEEDED, f.family, [f.name])),
    *cases(Property.SEEDED, Family.ACCOUNTS, ["sign_in"]),
]


def _declared(driver: Driver, fact: Fact, record: Callable[[str, object], None]) -> bool:
    return any(absent(driver, c, record) for c in fact.capabilities)


@parametrize([c for c in CASES if c.case != "sign_in"])
def test_a_seeded_fact_is_observable_through_the_vendor_api_or_the_world_is_refused_naming_it(
    case: Case, harness: Harness, record_property: Callable[[str, object], None]
) -> None:
    """A distinctive value for one field of the shared seed is either read back through the vendor's API (and the
    neutral view, where it has a field for it), or the world is refused naming the field and the provider."""
    fact = FACTS[case.case]
    driver = require(case.provider, fact.family)
    if _declared(driver, fact, record_property):
        return
    seeded = merged(seed(case.provider), fact.seed(case.provider))
    with opened_or_refused(harness, driver, seeded, names=fact.names) as world:
        if world is None:
            return
        with harness.session(driver, world) as session:
            fact.check(session, world.events(), case.provider)


@parametrize(cases(Property.SEEDED, Family.ACCOUNTS, ["sign_in"]))
def test_a_seeded_sign_in_signs_in_as_its_person_or_the_world_is_refused_naming_it(
    case: Case, harness: Harness, record_property: Callable[[str, object], None]
) -> None:
    """A `SignIn` naming a person is a credential the vendor's own sign-in takes and answers as that person, or the
    world is refused naming sign-ins and the provider."""
    driver = require(case.provider, Family.ACCOUNTS)
    credential = driver.sign_in_credential(uuid.uuid4().hex)
    signs_in = {"provider": case.provider, "credential": credential, "person": "sofia"}
    seeded = merged(seed(case.provider), {"sign_ins": [signs_in]})
    with opened_or_refused(harness, driver, seeded, names=("sign_in",), claiming=[credential]) as world:
        if world is None:
            return
        with driver.signed_in(harness.api, world.view, credential) as session:
            who = session.whoami().casefold()
            assert "sofia" in who, f"the sign-in seeded for sofia answers as {who!r}"
