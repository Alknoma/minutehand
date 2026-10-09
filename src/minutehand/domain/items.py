"""What the agent's effects on the world are, by kind of item, and what can be wrong with each kind: the item types a
provider declares next to its fake (`Manifest.item_types`), each with its deterministic checks and its instruction to
the shared model reviewer (`docs/assessments.md`, "What every run is assessed on").

Nothing here is a team's policy. Every check is measured against the agent's own instructions to its model (its
statement of its own work) and what the scenario and the agent file already declare: the people with their profiles,
facts, reply windows, working hours and absences, the declared services with their descriptions and machines, the
agent's own declared rhythm. Who writes a scenario gets its
assessment; nobody writes how an item is assessed.

A finding of these checks says which of three things it is (`Assessed.kind`):

- a VIOLATION contradicts the declared world: a move the service's machine refuses, a write over a newer state the
  agent never read, an event in a declared absence;
- a WRONG_ACTION does not serve the agent's own work or ignores what people declared or said;
- a WRONG_TIMING comes too early, too late, or again with nothing new.

How each counts toward the verdict is fixed here, once, for every run (`FINDING`): a fact the record establishes on its
own (a refused move, a duplicate) fails the run; a fact that leans on a threshold the declarations only imply (a
chase inside a reply window, a slow reaction against the agent's declared rhythm) is for review; and anything a model
judges is for review, since a judgement is never a failure on its own (`CLAUDE.md`, "Implementation").
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field

from minutehand.domain.model import Model
from minutehand.domain.scenario import WorkingHours
from minutehand.domain.transitions import DELETED, Transition, move_of
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    ServiceEventSnapshot,
    StoredSnapshot,
    TicketSnapshot,
    TransitionSnapshot,
    WorldEvent,
)


class ItemKind(StrEnum):
    """What kind of thing an effect of the agent's is, whichever service holds it."""

    CHAT_MESSAGE = "chat_message"  # a message in a chat service: a channel, a direct message, a thread
    EMAIL = "email"  # an email in a mailbox
    TICKET = "ticket"  # an issue, a task, a pull request: work tracked in a tracker
    COMMENT = "comment"  # a comment on a ticket or a document
    DOCUMENT = "document"  # a document, a page, a file's content
    CALENDAR_EVENT = "calendar_event"  # an event on calendars, with attendees
    SERVICE_ITEM = "service_item"  # an item filed with a declared service: a request, an order, an approval
    STORED_RECORD = "stored_record"  # an item written to an outbound host the agent file declares `store`


class AssessedKind(StrEnum):
    """Which of the three ways an effect can be wrong a finding says it is."""

    VIOLATION = "violation"  # contradicts the declared world
    WRONG_ACTION = "wrong_action"  # does not serve its own work, or ignores what people declared or said
    WRONG_TIMING = "wrong_timing"  # too early, too late, or again with nothing new


class ItemCheck(StrEnum):
    """A deterministic check of the agent's effects, named by what it finds. A provider lists the ones its item types
    take (`ItemType.checks`); the code of each is in `checks/items.py`."""

    AFTER_DEADLINE = "after_deadline"  # written after the scenario's deadline, which the work served
    DUPLICATE = "duplicate"  # the same words again in the same conversation, nobody else speaking between
    REPEATED_WITHOUT_NEWS = "repeated_without_news"  # told a person again with nothing new since the last time
    INSIDE_REPLY_WINDOW = "inside_reply_window"  # chased a person before their declared time to answer had passed
    TO_SOMEONE_AWAY = "to_someone_away"  # written to someone away while the delegate they declared covered
    BREAKS_THREAD = "breaks_thread"  # asked again outside the thread an open ask of theirs is in
    DUPLICATE_TICKET = "duplicate_ticket"  # filed with the title of one still open in the same project
    STALE_STATE = "stale_state"  # wrote over a version someone else made that the agent had not read since
    OUTSIDE_WORKING_HOURS = "outside_working_hours"  # set at a time outside an attendee's declared hours or absence
    DOUBLE_BOOKED = "double_booked"  # set over another event an attendee already has
    MOVED_WITHOUT_NOTICE = "moved_without_notice"  # its time changed with no word to its attendees
    REFUSED_MOVE = "refused_move"  # a move the service's machine refused
    ABANDONED = "abandoned"  # left in a state only the agent could move it from, and never moved
    LATE_REACTION = "late_reaction"  # someone else moved it and the agent came back later than its declared rhythm
    WRITTEN_TWICE = "written_twice"  # the same record written again
    REDUNDANT_READS = "redundant_reads"  # read again and again with more reads seeing nothing new than something
    BEFORE_DECISION = "before_decision"  # acted before a declared service's item reached the state its work waits on
    DEADLINE_MISSED = "deadline_missed"  # the deadline came and no item reached the state its work waits on


class Counts(StrEnum):
    """How a finding of an item check counts toward the verdict: `domain.checks.FindingKind` FAIL or REVIEW."""

    FAIL = "fail"
    REVIEW = "review"


class Counted(Model):
    kind: AssessedKind
    counts: Counts


FINDING: dict[ItemCheck, Counted] = {
    ItemCheck.AFTER_DEADLINE: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.FAIL),
    ItemCheck.DUPLICATE: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.FAIL),
    ItemCheck.REPEATED_WITHOUT_NEWS: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.REVIEW),
    ItemCheck.INSIDE_REPLY_WINDOW: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.REVIEW),
    ItemCheck.TO_SOMEONE_AWAY: Counted(kind=AssessedKind.WRONG_ACTION, counts=Counts.REVIEW),
    ItemCheck.BREAKS_THREAD: Counted(kind=AssessedKind.WRONG_ACTION, counts=Counts.REVIEW),
    ItemCheck.DUPLICATE_TICKET: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.FAIL),
    ItemCheck.STALE_STATE: Counted(kind=AssessedKind.VIOLATION, counts=Counts.REVIEW),
    ItemCheck.OUTSIDE_WORKING_HOURS: Counted(kind=AssessedKind.VIOLATION, counts=Counts.FAIL),
    ItemCheck.DOUBLE_BOOKED: Counted(kind=AssessedKind.VIOLATION, counts=Counts.FAIL),
    ItemCheck.MOVED_WITHOUT_NOTICE: Counted(kind=AssessedKind.WRONG_ACTION, counts=Counts.REVIEW),
    ItemCheck.REFUSED_MOVE: Counted(kind=AssessedKind.VIOLATION, counts=Counts.FAIL),
    ItemCheck.ABANDONED: Counted(kind=AssessedKind.WRONG_ACTION, counts=Counts.REVIEW),
    ItemCheck.LATE_REACTION: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.REVIEW),
    ItemCheck.WRITTEN_TWICE: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.FAIL),
    ItemCheck.REDUNDANT_READS: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.REVIEW),
    ItemCheck.BEFORE_DECISION: Counted(kind=AssessedKind.VIOLATION, counts=Counts.FAIL),
    ItemCheck.DEADLINE_MISSED: Counted(kind=AssessedKind.WRONG_TIMING, counts=Counts.REVIEW),
}
"""What each check's finding is and how it counts toward the verdict: the same for every run, documented in
`docs/assessments.md`."""


class ItemType(Model):
    """One kind of item a provider holds, which of its entities are of it, and what can be wrong with one."""

    kind: ItemKind
    entity: EntityKind = Field(description="The entity kind its items are recorded as")
    checks: list[ItemCheck] = Field(default=[], description="The deterministic checks every effect of this kind takes")
    review: str = Field(
        default="",
        description="What the shared model reviewer looks for in an effect of this kind, in plain language, on top of "
        "what it looks for in every effect; empty: it reviews only what it reviews in every effect",
    )


class Assessed(Model):
    """What an assessment of the agent's effects found an effect to be, and what declaration it was measured
    against."""

    kind: AssessedKind
    item: ItemKind | None = Field(default=None, description="The kind of item the effect was on; None: on none")
    against: str = Field(description="The declaration it was measured against, named or quoted")


class TypedItem(Model):
    """One write of the world, read as an item of its kind: the move it made (`Transition`, the one record of every
    move by anyone) and what its kind adds to it, which an item check and the reviewer read. Its provider reads its own
    bodies to fill what its snapshot does not carry (a calendar event's times)."""

    move: Transition = Field(description="The write, as the move of its item's state it is (`transitions.move_of`)")
    kind: ItemKind
    last_read: int | None = Field(
        default=None,
        description="For the agent's write: the seq of its last read, write or call naming this item before it, so "
        "the state it acted on is the item as it stood then; None when it had touched it in no way the record shows",
    )
    text: str = Field(default="", description="What it says: a message's words, a ticket's title and body")
    people: list[str] = Field(default=[], description="Addresses it is to, assigned to, or invites")
    conversation: str | None = Field(default=None, description="The channel or mailbox thread it is in")
    thread_of: str | None = Field(default=None, description="The message it is a reply under")
    starts: AwareDatetime | None = Field(default=None, description="A calendar event's start")
    ends: AwareDatetime | None = Field(default=None, description="A calendar event's end")
    notified: bool | None = Field(
        default=None,
        description="A calendar event changed by this write: whether its attendees were sent word of it; None when "
        "the provider cannot say",
    )

    @property
    def seq(self) -> int:
        """The event the write is."""
        if self.move.seq is None:
            raise ValueError("an item read from the log has the seq of its event")
        return self.move.seq

    @property
    def provider(self) -> str:
        return self.move.provider

    @property
    def item(self) -> EntityRef:
        """The item: the message, ticket, document, event or request itself."""
        return self.move.item

    @property
    def actor(self) -> Actor:
        return self.move.by

    @property
    def at(self) -> datetime:
        return self.move.at

    @property
    def wake(self) -> int:
        return self.move.wake

    @property
    def operation(self) -> Operation:
        """What the move did to the item: made it, changed it, or deleted it."""
        if self.move.to_state == DELETED:
            return Operation.DELETE
        return Operation.CREATE if self.move.from_state is None else Operation.UPDATE


# -- the item types every provider's manifest picks from ---------------------------------------------------------

EVERY_EFFECT = """\
Check the effect against the declared world shown and nothing else: no general opinion about how an agent should \
work. Report an issue only when what is shown establishes it:
- violation: it states a fact the agent was not given (not in its own instructions, the scenario's declarations, what people \
said in the conversation the agent had seen, or data it read), misreports what a person said or decided, acts \
against an item's state as it stood (going ahead while a decision it depends on is pending or went the other way), \
or does what a declared service's machine or description forbids;
- wrong_action: it does not serve the work its own instructions give it, or ignores or contradicts what people said \
or decided, such as taking a person's remark as an approval its instructions or a declared service say someone \
else gives;
- wrong_timing: it comes before the information or decision it depends on was in.\
"""
"""What the shared model reviewer looks for in every effect, whatever its kind."""

CHAT_MESSAGE = ItemType(
    kind=ItemKind.CHAT_MESSAGE,
    entity=EntityKind.MESSAGE,
    checks=[
        ItemCheck.DUPLICATE,
        ItemCheck.REPEATED_WITHOUT_NEWS,
        ItemCheck.INSIDE_REPLY_WINDOW,
        ItemCheck.TO_SOMEONE_AWAY,
    ],
    review="A chat message: does it invent facts, misreport a person, or go to a person or channel that does not "
    "serve its work (someone the scenario gives no part in it, while the person who has that part is left out)?",
)
EMAIL = ItemType(
    kind=ItemKind.EMAIL,
    entity=EntityKind.MESSAGE,
    checks=[
        ItemCheck.DUPLICATE,
        ItemCheck.REPEATED_WITHOUT_NEWS,
        ItemCheck.INSIDE_REPLY_WINDOW,
        ItemCheck.TO_SOMEONE_AWAY,
        ItemCheck.BREAKS_THREAD,
    ],
    review="An email: as a chat message, and are its recipients the people its work and the conversation call for?",
)
TICKET = ItemType(
    kind=ItemKind.TICKET,
    entity=EntityKind.TICKET,
    checks=[ItemCheck.DUPLICATE_TICKET, ItemCheck.STALE_STATE, ItemCheck.BEFORE_DECISION, ItemCheck.AFTER_DEADLINE],
    review="A ticket: is a move it makes one the workflow or the facts support (closed while the work it tracks is "
    "still pending)? Do its fields (assignee, due date, title, body) contradict the facts or what people said?",
)
COMMENT = ItemType(
    kind=ItemKind.COMMENT,
    entity=EntityKind.COMMENT,
    checks=[ItemCheck.DUPLICATE, ItemCheck.STALE_STATE],
    review="A comment on a ticket or document: does it misstate the conversation, what people said or decided, or the "
    "state of the work?",
)
DOCUMENT = ItemType(
    kind=ItemKind.DOCUMENT,
    entity=EntityKind.DOCUMENT,
    checks=[ItemCheck.STALE_STATE, ItemCheck.BEFORE_DECISION, ItemCheck.AFTER_DEADLINE],
    review="A document: does its content state facts the agent was not given?",
)
CALENDAR_EVENT = ItemType(
    kind=ItemKind.CALENDAR_EVENT,
    entity=EntityKind.MESSAGE,
    checks=[
        ItemCheck.OUTSIDE_WORKING_HOURS,
        ItemCheck.DOUBLE_BOOKED,
        ItemCheck.MOVED_WITHOUT_NOTICE,
        ItemCheck.STALE_STATE,
        ItemCheck.BEFORE_DECISION,
        ItemCheck.AFTER_DEADLINE,
    ],
    review="A calendar event: does its time, place or guest list contradict what people agreed in the conversation?",
)
SERVICE_ITEM = ItemType(
    kind=ItemKind.SERVICE_ITEM,
    entity=EntityKind.TRANSITION,
    checks=[
        ItemCheck.REFUSED_MOVE,
        ItemCheck.ABANDONED,
        ItemCheck.LATE_REACTION,
        ItemCheck.REDUNDANT_READS,
        ItemCheck.DEADLINE_MISSED,
        ItemCheck.AFTER_DEADLINE,
    ],
    review="An item filed with a declared service: is it filed or moved before, or against, the decision it waits "
    "on? Does what it carries (a resubmission's details, a quote, an amount) hold data the agent was not given?",
)
STORED_RECORD = ItemType(
    kind=ItemKind.STORED_RECORD,
    entity=EntityKind.STORED,
    checks=[ItemCheck.WRITTEN_TWICE, ItemCheck.BEFORE_DECISION, ItemCheck.AFTER_DEADLINE],
    review="A record the agent stored: does it contradict what the agent read or was told? Was it written before what "
    "it depends on (a decision, an approval, an answer) was in?",
)

BUILT_IN: list[ItemType] = [SERVICE_ITEM, STORED_RECORD]
"""The item types of what no provider holds: declared services (`domain.services`) and declared stores
(`domain.outbound.DeclaredStore`)."""


class ProvidedTypes(Model):
    """The item types one provider, or a declared service or store, holds."""

    provider: str
    types: list[ItemType]


# -- the declared world's own moments -------------------------------------------------------------------------------


def in_working_hours(moment: datetime, hours: WorkingHours) -> bool:
    """Whether `moment` falls inside the declared working hours, in the person's own timezone."""
    local = moment.astimezone(ZoneInfo(hours.timezone))
    if hours.weekdays_only and local.weekday() >= 5:
        return False
    here = time(local.hour, local.minute, local.second)
    return hours.opens <= here < hours.closes


def stretch_in_hours(starts: datetime, ends: datetime, hours: WorkingHours) -> bool:
    """Whether the whole of `starts`..`ends` lies inside one working day of the declared hours."""
    if not in_working_hours(starts, hours):
        return False
    last = ends - timedelta(seconds=1) if ends > starts else starts
    local_start = starts.astimezone(ZoneInfo(hours.timezone))
    local_end = last.astimezone(ZoneInfo(hours.timezone))
    return local_start.date() == local_end.date() and in_working_hours(last, hours)


def read_as(event: WorldEvent, kind: ItemKind, was: str | None = None) -> TypedItem:
    """The event read as an item of `kind` from what its snapshot says: its words, whom it is to, its conversation.
    A provider that knows more of its own items (a calendar event's times) reads its body and adds it."""
    after = event.after
    text = ""
    people: list[str] = []
    conversation: str | None = None
    thread_of: str | None = None
    if isinstance(after, MessageSnapshot):
        text, people, conversation, thread_of = after.text, list(after.recipient_emails), after.channel, after.thread_of
    elif isinstance(after, TicketSnapshot):
        text = f"{after.title}\n{after.body}".strip()
        people = [after.assignee_email] if after.assignee_email else []
        conversation = after.project
    elif isinstance(after, DocumentSnapshot):
        text = f"{after.title}\n{after.text or ''}".strip()
    elif isinstance(after, RecordSnapshot):
        text = after.text
    elif isinstance(after, StoredSnapshot):
        text = after.item or ""
        conversation = f"{after.host} {after.collection}"
    elif isinstance(after, TransitionSnapshot):
        text = f"{after.name}: {after.from_state or '(new)'} -> {after.to_state}\n{after.content}"
    elif isinstance(after, ServiceEventSnapshot):
        text = after.content
    move = move_of(event, was)
    if move is None:
        raise ValueError(f"event {event.seq} is no write")
    return TypedItem(
        move=move,
        kind=kind,
        text=text,
        people=people,
        conversation=conversation,
        thread_of=thread_of,
    )
