"""Property 3 — what people do, everywhere.

Every kind of happening and every control-API act that applies to a family either works on every provider of that
family — observable through the vendor's API with that person as the actor, and recorded in the world as a change by
a person (actor PERSON; an outside edit or an administrator's change, actor SCENARIO, as docs/serve.md says) at the
world's clock — or is refused at the call, 409, naming the provider. Likewise a person's account removed,
deactivated or reactivated, and a named permission withheld and granted again.

Each case first moves the world's clock three days on, so "at the world's clock" is not the seed's start.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from pydantic import TypeAdapter

from minutehand.adapters.control.wire import ChangePerson, Inbound
from minutehand.domain.people import Press
from minutehand.domain.provider import PersonChange
from minutehand.domain.scenario import AccessRole, Happening, TicketState
from minutehand.domain.world import Actor, EntityKind, EntityRef, InteractionSnapshot, TicketSnapshot, WorldEvent
from minutehand.testing.client import Refused, Unsupported
from minutehand.testing.world import OpenWorld
from tests.conformance.contract import (
    CommentSeen,
    Documents,
    Driver,
    Family,
    Messaging,
    Progress,
    Property,
    Tickets,
)
from tests.conformance.harness import Case, Harness, absent, cases, parametrize, require, seed
from tests.conformance.neutral import grants, merged
from tests.conformance.receiver import receiver

LATER = timedelta(days=3)
SECRET = "conformance-signing-secret"

type Record = Callable[[str, object], None]


@dataclass(frozen=True)
class Did:
    """What an act wrote, or None when it was refused at the call naming the provider."""

    event: WorldEvent | None


def act(world: OpenWorld, provider: str, doing: Callable[[], WorldEvent]) -> Did:
    """The act's first event; a 409 refusal naming the provider is an answer the property allows, any other
    refusal fails."""
    try:
        return Did(doing())
    except Refused as refused:
        assert refused.status == 409, f"refused {refused.status}, not 409: {refused.error}"
        assert provider in refused.error, f"refused without naming {provider}: {refused.error}"
        return Did(None)


def by_person_now(world: OpenWorld, event: WorldEvent, actor: Actor = Actor.PERSON) -> None:
    now = world.now()
    assert (event.actor, event.sim_time) == (actor, now), (
        f"recorded as {event.actor.value} at {event.sim_time}; the rule is {actor.value} at the world's clock {now}"
    )


@contextmanager
def opened(harness: Harness, driver: Driver, more: dict[str, object] | None = None) -> Iterator[OpenWorld]:
    with receiver() as pushed:
        inbound = [Inbound(provider=driver.provider, url=pushed.url, secret=SECRET)]
        with harness.world(driver, merged(seed(driver.provider), more or {}), inbound=inbound) as world:
            world.advance(LATER)
            yield world


# ---------------------------------------------------------------------------------------------- tickets

SEEDED = "Book the venue"


def _ticket_happening(action: dict[str, object]) -> Callable[[str], dict[str, object]]:
    return lambda provider: {"kind": "ticket", "person": "sofia", "ticket": SEEDED, "after": "PT0S", "action": action}


def _ref(world: OpenWorld, provider: str) -> EntityRef:
    found = [
        e.entity
        for e in world.events(provider=provider, kind=EntityKind.TICKET)
        if isinstance(e.after, TicketSnapshot) and e.after.title == SEEDED
    ]
    return found[0]


@dataclass(frozen=True)
class TicketAct:
    name: str
    capabilities: tuple[str, ...]
    prepare: Callable[[OpenWorld, str], None]
    do: Callable[[OpenWorld, str], WorldEvent]
    seen: Callable[[Tickets, str], None]
    actor: Actor = Actor.PERSON
    changed_by: str | None = "sofia@example.com"


def _happen(action: dict[str, object]) -> Callable[[OpenWorld, str], WorldEvent]:
    def do(world: OpenWorld, provider: str) -> WorldEvent:
        happening = _ticket_happening(action)(provider)
        return world.happen(_happening(happening))

    return do


HAPPENING: TypeAdapter[Happening] = TypeAdapter(Happening)


def _happening(body: dict[str, object]) -> Happening:
    return HAPPENING.validate_python(body)


def _nothing(world: OpenWorld, provider: str) -> None:
    return None


def _done_first(world: OpenWorld, provider: str) -> None:
    world.happen(_happening(_ticket_happening({"kind": "moves", "to": "done"})(provider)))


def _progress(progress: Progress) -> Callable[[Tickets, str], None]:
    def seen(session: Tickets, ticket: str) -> None:
        found = session.read_ticket(ticket).progress
        assert found is progress, f"the API shows the ticket {found.value}, the person moved it {progress.value}"

    return seen


def _assignee(email: str | None) -> Callable[[Tickets, str], None]:
    def seen(session: Tickets, ticket: str) -> None:
        found = session.read_ticket(ticket).assignee_email
        assert found == email, f"the API shows the ticket assigned to {found}, the person gave it to {email}"

    return seen


def _commented(session: Tickets, ticket: str) -> None:
    found = session.read_ticket(ticket).comments
    assert CommentSeen("sofia@example.com", "Sofia's comment") in found, f"the API shows the comments {found}"


def _gone(session: Tickets, ticket: str) -> None:
    left = session.ticket_ids()
    assert SEEDED not in left, f"the API still lists {SEEDED!r} after its deletion"


TICKET_ACTS = {
    a.name: a
    for a in [
        TicketAct(
            "happening_moves_done", (), _nothing, _happen({"kind": "moves", "to": "done"}), _progress(Progress.DONE)
        ),
        TicketAct(
            "happening_moves_cancelled",
            ("tickets.cancelled",),
            _nothing,
            _happen({"kind": "moves", "to": "cancelled"}),
            _progress(Progress.CANCELLED),
        ),
        TicketAct(
            "happening_reopens", (), _done_first, _happen({"kind": "moves", "to": "open"}), _progress(Progress.OPEN)
        ),
        TicketAct(
            "happening_reassigns",
            (),
            _nothing,
            _happen({"kind": "reassigns", "to": "mila"}),
            _assignee("mila@example.com"),
        ),
        TicketAct("happening_unassigns", (), _nothing, _happen({"kind": "reassigns", "to": None}), _assignee(None)),
        TicketAct(
            "happening_comments", (), _nothing, _happen({"kind": "comments", "text": "Sofia's comment"}), _commented
        ),
        TicketAct("happening_deletes", (), _nothing, _happen({"kind": "deletes"}), _gone, changed_by=None),
        TicketAct(
            "act_move_ticket",
            (),
            _nothing,
            lambda w, p: w.move_ticket(_ref(w, p), TicketState.DONE),
            _progress(Progress.DONE),
        ),
        TicketAct(
            "act_edit_ticket_state",
            (),
            _nothing,
            lambda w, p: w.edit_ticket(_ref(w, p), state=TicketState.DONE),
            _progress(Progress.DONE),
            actor=Actor.SCENARIO,
            changed_by=None,
        ),
        TicketAct(
            "act_edit_ticket_assignee",
            (),
            _nothing,
            lambda w, p: w.edit_ticket(_ref(w, p), assignee="mila"),
            _assignee("mila@example.com"),
            actor=Actor.SCENARIO,
            changed_by=None,
        ),
        TicketAct("act_delete_ticket", (), _nothing, lambda w, p: w.delete_ticket(_ref(w, p)), _gone, changed_by=None),
    ]
}


@parametrize(cases(Property.PEOPLE_ACT, Family.TICKETS, list(TICKET_ACTS)))
def test_what_a_person_does_to_a_ticket_is_seen_through_the_api_as_theirs_or_refused_naming_the_provider(
    case: Case, harness: Harness, record_property: Record
) -> None:
    """A person's act on a seeded ticket shows through the tracker's API, made by them where the API says who
    changed it, and is recorded at the world's clock as theirs; or the act is refused naming the provider."""
    driver = require(case.provider, Family.TICKETS)
    did = TICKET_ACTS[case.case]
    if any(absent(driver, c, record_property) for c in did.capabilities):
        return
    with opened(harness, driver) as world, harness.session(driver, world) as session:
        assert isinstance(session, Tickets)
        ticket = session.ticket_ids()[SEEDED]
        did.prepare(world, case.provider)
        acted = act(world, case.provider, lambda: did.do(world, case.provider))
        if acted.event is None:
            return
        by_person_now(world, acted.event, did.actor)
        did.seen(session, ticket)
        if did.changed_by is not None and not absent(driver, "tickets.changed_by", record_property):
            found = session.read_ticket(ticket).changed_by_email
            assert found == did.changed_by, f"the API says the change was made by {found}, not {did.changed_by}"


# ---------------------------------------------------------------------------------------------- documents

DOC = "People doc"


def _doc_seed(provider: str) -> dict[str, object]:
    return {
        "documents": [
            {
                "provider": provider,
                "title": DOC,
                "text": "Start of the doc.",
                "shared_with": [{"person": "sofia", "role": "writer"}],
            }
        ]
    }


@dataclass(frozen=True)
class DocumentAct:
    name: str
    capabilities: tuple[str, ...]
    action: dict[str, object]
    seen: Callable[[Documents, str, list[WorldEvent], str], None]


def _edited(session: Documents, document: str, events: list[WorldEvent], provider: str) -> None:
    text = session.read_text(document)
    assert "Sofia's addition." in text, f"the API's text lacks the person's edit: {text!r}"


def _renamed(session: Documents, document: str, events: list[WorldEvent], provider: str) -> None:
    titles = {d.id: d.title for d in session.documents()}
    assert titles.get(document) == "Renamed by Sofia", f"the API shows the title {titles.get(document)!r}"


def _moved(session: Documents, document: str, events: list[WorldEvent], provider: str) -> None:
    folders = {d.id: d.folder for d in session.documents()}
    assert folders.get(document) == "Moved/Here", f"the API shows the folder {folders.get(document)!r}"


def _shared(session: Documents, document: str, events: list[WorldEvent], provider: str) -> None:
    shared = {d.id: dict(d.shared) for d in session.documents()}
    mine = {k: v.value for k, v in shared.get(document, {}).items()}
    assert shared.get(document, {}).get("mila@example.com") is AccessRole.COMMENTER, f"shared mila commenter: {mine}"
    held = [(g.to, g.role.value) for g in grants(events, provider)]
    assert ("mila@example.com", "commenter") in held, f"the neutral grants are {held}"


def _trashed(session: Documents, document: str, events: list[WorldEvent], provider: str) -> None:
    assert document not in [d.id for d in session.documents()], "the API still lists the trashed document"


def _doc_commented(session: Documents, document: str, events: list[WorldEvent], provider: str) -> None:
    found = session.comments(document)
    assert any(c.text == "Sofia's note" and c.by("sofia@example.com", "Sofia Romano") for c in found), (
        f"the API shows the comments {found}"
    )


def _field_set(session: Documents, document: str, events: list[WorldEvent], provider: str) -> None:
    raise AssertionError("a field set on a plain document was accepted; no API shows a field it has not got")


DOCUMENT_ACTS = {
    a.name: a
    for a in [
        DocumentAct("happening_edited", (), {"kind": "edited", "append": "Sofia's addition."}, _edited),
        DocumentAct("happening_renamed", (), {"kind": "renamed", "to": "Renamed by Sofia"}, _renamed),
        DocumentAct("happening_moved", ("documents.folders",), {"kind": "moved", "folder": "Moved/Here"}, _moved),
        DocumentAct(
            "happening_shared",
            ("documents.sharing",),
            {"kind": "shared", "access": {"person": "mila", "role": "commenter"}},
            _shared,
        ),
        DocumentAct("happening_trashed", (), {"kind": "trashed"}, _trashed),
        DocumentAct(
            "happening_commented",
            ("documents.comments",),
            {"kind": "commented", "text": "Sofia's note"},
            _doc_commented,
        ),
        DocumentAct("happening_field_set", (), {"kind": "field_set", "field": "Status", "value": "Done"}, _field_set),
    ]
}


@parametrize(cases(Property.PEOPLE_ACT, Family.DOCUMENTS, list(DOCUMENT_ACTS)))
def test_what_a_person_does_to_a_document_is_seen_through_the_api_as_theirs_or_refused_naming_the_provider(
    case: Case, harness: Harness, record_property: Record
) -> None:
    """A person's change to a seeded document shows through the vendor's API, by them where the API says who last
    changed it, and is recorded at the world's clock as theirs; or it is refused naming the provider."""
    driver = require(case.provider, Family.DOCUMENTS)
    did = DOCUMENT_ACTS[case.case]
    if any(absent(driver, c, record_property) for c in did.capabilities):
        return
    with opened(harness, driver, _doc_seed(case.provider)) as world, harness.session(driver, world) as session:
        assert isinstance(session, Documents)
        document = next(d.id for d in session.documents() if d.title == DOC)
        happening = {"kind": "document", "person": "sofia", "document": DOC, "after": "PT1M", "action": did.action}
        acted = act(world, case.provider, lambda: world.happen(_happening(happening)))
        if acted.event is None:
            return
        by_person_now(world, acted.event)
        did.seen(session, document, world.events(), case.provider)
        if case.case in ("happening_edited", "happening_renamed") and not absent(
            driver, "documents.last_editor", record_property
        ):
            editor = {d.id: d.last_editor_email for d in session.documents()}.get(document)
            assert editor == "sofia@example.com", f"the API says {editor} changed it last, not sofia"


# ---------------------------------------------------------------------------------------------- messaging


def _messaging_seed(provider: str) -> dict[str, object]:
    return {
        "channels": [
            {
                "provider": provider,
                "name": "talk",
                "members": ["owen", "sofia", "mila"],
                "history": [
                    {"by": "owen", "text": "Seeded root", "ago": "PT2H", "key": "root"},
                    {"by": "sofia", "text": "Sofia's own post", "ago": "PT1H", "key": "mine"},
                ],
            },
            {"provider": provider, "name": "side", "members": ["owen"]},
            {"provider": provider, "name": "quiet", "members": ["owen", "sofia"], "agent_member": False},
        ]
    }


@dataclass(frozen=True)
class MessagingAct:
    name: str
    capabilities: tuple[str, ...]
    do: Callable[[OpenWorld, str, Messaging], WorldEvent]
    seen: Callable[[Messaging, OpenWorld], None]


def _messaging(body: dict[str, object]) -> Callable[[OpenWorld, str, Messaging], WorldEvent]:
    def do(world: OpenWorld, provider: str, session: Messaging) -> WorldEvent:
        return world.happen(_happening({"provider": provider, "person": "sofia", **body}))

    return do


def _in(channel: str, text: str, *, thread_of: str | None = None) -> Callable[[Messaging, OpenWorld], None]:
    def seen(session: Messaging, world: OpenWorld) -> None:
        history = session.history(session.channel(channel))
        found = [m for m in history if m.text == text]
        assert len(found) == 1, f"#{channel} holds {len(found)} messages {text!r}: {[m.text for m in history]}"
        assert found[0].author_email == "sofia@example.com", f"{text!r} is shown as {found[0].author_email}'s"
        if thread_of is not None:
            root = next(m for m in history if m.text == thread_of)
            assert found[0].thread_of == root.id, f"{text!r} is not in the thread of {thread_of!r}"

    return seen


def _direct_holds(text: str) -> Callable[[Messaging, OpenWorld], None]:
    def seen(session: Messaging, world: OpenWorld) -> None:
        sofia = next(p.id for p in session.people() if p.email == "sofia@example.com")
        history = session.history(session.direct(sofia))
        found = [m for m in history if m.text == text]
        assert len(found) == 1, f"the DM holds {[m.text for m in history]}"
        assert found[0].author_email == "sofia@example.com", f"{text!r} is shown as {found[0].author_email}'s"

    return seen


def _without(channel: str, text: str) -> Callable[[Messaging, OpenWorld], None]:
    def seen(session: Messaging, world: OpenWorld) -> None:
        texts = [m.text for m in session.history(session.channel(channel))]
        assert text not in texts, f"#{channel} still holds {text!r}"

    return seen


def _reacted(session: Messaging, world: OpenWorld) -> None:
    mine = next(m for m in session.history(session.channel("talk")) if m.text == "Sofia's own post")
    assert "eyes" in mine.reactions, f"the API shows the reactions {mine.reactions}"


def _member(channel: str) -> Callable[[Messaging, OpenWorld], None]:
    def seen(session: Messaging, world: OpenWorld) -> None:
        found = next(c for c in session.channels() if c.name == channel)
        assert "sofia@example.com" in found.member_emails, f"#{channel}'s members are {sorted(found.member_emails)}"

    return seen


def _agent_reads(channel: str) -> Callable[[Messaging, OpenWorld], None]:
    def seen(session: Messaging, world: OpenWorld) -> None:
        session.history(session.channel(channel))

    return seen


def _no_vendor_trace(session: Messaging, world: OpenWorld) -> None:
    return None


def _said(world: OpenWorld, provider: str, session: Messaging) -> WorldEvent:
    return world.say("sofia", "Said by Sofia", provider=provider)


def _replied(world: OpenWorld, provider: str, session: Messaging) -> WorldEvent:
    made = session.post(session.channel("talk"), "The agent asks")
    ref = EntityRef(provider=provider, kind=EntityKind.MESSAGE, external_id=made)
    return world.reply("sofia", "Sofia answers", to=ref)


def _pressed(world: OpenWorld, provider: str, session: Messaging) -> WorldEvent:
    made = session.post_button(session.channel("talk"), "Approve this?", "approve_it", "Approve")
    ref = EntityRef(provider=provider, kind=EntityKind.MESSAGE, external_id=made)
    return world.press("sofia", ref, Press(action_id="approve_it", label="Approve"))


def _press_recorded(session: Messaging, world: OpenWorld) -> None:
    pressed = [e.after for e in world.events() if isinstance(e.after, InteractionSnapshot)]
    assert [(p.person, p.action_id) for p in pressed] == [("sofia", "approve_it")], pressed


MESSAGING_ACTS = {
    a.name: a
    for a in [
        MessagingAct("happening_posts_in_a_channel", (), _messaging({"kind": "posts", "channel": "talk", "text": "Posted by Sofia"}), _in("talk", "Posted by Sofia")),
        MessagingAct("happening_posts_directly", ("messaging.direct",), _messaging({"kind": "posts", "text": "Direct from Sofia"}), _direct_holds("Direct from Sofia")),
        MessagingAct(
            "happening_posts_in_a_thread",
            (),
            _messaging({"kind": "posts", "channel": "talk", "text": "Sofia in the thread", "in_thread_of": "root"}),
            _in("talk", "Sofia in the thread", thread_of="Seeded root"),
        ),
        MessagingAct("happening_edits", (), _messaging({"kind": "edits", "post": "mine", "text": "Sofia's edited post"}), _in("talk", "Sofia's edited post")),
        MessagingAct("happening_deletes", (), _messaging({"kind": "deletes", "post": "mine"}), _without("talk", "Sofia's own post")),
        MessagingAct("happening_reacts", ("messaging.react",), _messaging({"kind": "reacts", "post": "mine", "reaction": "eyes"}), _reacted),
        MessagingAct("happening_joins", (), _messaging({"kind": "joins", "channel": "side"}), _member("side")),
        MessagingAct("happening_adds_agent", (), _messaging({"kind": "adds_agent", "channel": "quiet"}), _agent_reads("quiet")),
        MessagingAct("happening_opens_agent", (), _messaging({"kind": "opens_agent"}), _no_vendor_trace),
        MessagingAct("happening_commands", (), _messaging({"kind": "commands", "command": "/conform", "text": "go"}), _no_vendor_trace),
        MessagingAct("act_say", ("messaging.direct",), _said, _direct_holds("Said by Sofia")),
        MessagingAct("act_reply", (), _replied, _in("talk", "Sofia answers")),
        MessagingAct("act_press", ("messaging.button",), _pressed, _press_recorded),
    ]
}  # fmt: skip


@parametrize(cases(Property.PEOPLE_ACT, Family.MESSAGING, list(MESSAGING_ACTS)))
def test_what_a_person_does_in_a_conversation_is_seen_through_the_api_as_theirs_or_refused_naming_the_provider(
    case: Case, harness: Harness, record_property: Record
) -> None:
    """A person's post, reply, edit, delete, reaction, join, command or press shows through the vendor's API as
    theirs and is recorded at the world's clock as theirs; or it is refused naming the provider."""
    driver = require(case.provider, Family.MESSAGING)
    did = MESSAGING_ACTS[case.case]
    if any(absent(driver, c, record_property) for c in did.capabilities):
        return
    with opened(harness, driver, _messaging_seed(case.provider)) as world, harness.session(driver, world) as session:
        assert isinstance(session, Messaging)
        acted = act(world, case.provider, lambda: did.do(world, case.provider, session))
        if acted.event is None:
            return
        by_person_now(world, acted.event)
        did.seen(session, world)


# ---------------------------------------------------------------------------------------------- people and permissions

CHANGES = ["removed", "deactivated", "reactivated"]


@parametrize(cases(Property.PEOPLE_ACT, Family.ACCOUNTS, [f"person_{c}" for c in CHANGES]))
def test_a_persons_account_changed_while_open_shows_through_the_api_or_is_refused_as_unsupported(
    case: Case, harness: Harness, record_property: Record
) -> None:
    """An account removed is gone from the vendor's listing, one deactivated is listed inactive, one reactivated is
    active again; each recorded at the world's clock; or the change is refused as unsupported naming the provider."""
    driver = require(case.provider, Family.ACCOUNTS)
    change = PersonChange(case.case.removeprefix("person_"))
    if absent(driver, "accounts.people", record_property):
        return
    with opened(harness, driver) as world, harness.session(driver, world) as session:
        try:
            if change is PersonChange.REACTIVATED:
                world.deactivate_person(case.provider, "sofia")
            asked = ChangePerson(provider=case.provider, person="sofia", change=change)
            changed = world.client.change_person(world.world_id, asked).event
        except Unsupported as refused:
            assert case.provider in refused.error, (
                f"refused as unsupported without naming the provider: {refused.error}"
            )
            return
        by_person_now(world, changed, Actor.SCENARIO)
        listed = [p for p in session.people() if p.email == "sofia@example.com"]
        if change is PersonChange.REMOVED:
            assert not listed, f"the API still lists the removed account: {listed}"
        else:
            assert len(listed) == 1, f"the API lists {len(listed)} accounts for sofia after she was {change.value}"
            assert listed[0].active is (change is PersonChange.REACTIVATED), listed[0]


@parametrize(cases(Property.PEOPLE_ACT, Family.ACCOUNTS, ["permission_withheld_and_granted"]))
def test_a_permission_withheld_and_granted_changes_what_the_api_allows_or_is_refused_as_unsupported(
    case: Case, harness: Harness, record_property: Record
) -> None:
    """A provider that names permissions answers by them as soon as they change; any other refuses a permission
    change as unsupported, naming itself."""
    driver = require(case.provider, Family.ACCOUNTS)
    permission = driver.permission()
    with opened(harness, driver) as world:
        if permission is None:
            try:
                world.withhold(case.provider, "sofia", "any.permission")
            except Unsupported as refused:
                assert case.provider in refused.error, refused.error
                return
            raise AssertionError(f"{case.provider} took a permission change its driver knows no permission for")
        with harness.session(driver, world, person=permission.person) as session:
            assert permission.allowed(session), "allowed before anything was withheld"
            withheld = world.withhold(
                case.provider, permission.person, permission.permission, project=permission.project
            )
            by_person_now(world, withheld, Actor.SCENARIO)
            assert not permission.allowed(session), "still allowed once withheld"
            world.grant(case.provider, permission.person, permission.permission, project=permission.project)
            assert permission.allowed(session), "not allowed again once granted"
