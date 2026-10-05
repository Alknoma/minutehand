"""The know-how a finding hands over: each way a proactive agent fails, and the design that stops it.

The pages in `docs/patterns/<key>.md` say the same at length. `Pattern.reference`
names that page.
"""

from __future__ import annotations

from minutehand.domain.checks import Pattern


def _page(key: str) -> str:
    return f"docs/patterns/{key}.md"


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        key="expiry_on_every_wait",
        title="An expiry on every wait",
        failure="Waits on something forever, or notices the silence long after it should have.",
        design="Every wait carries an expected-by date and the agent wakes on it.",
        reference=_page("expiry_on_every_wait"),
    ),
    Pattern(
        key="check_world_before_model",
        title="Look at the world before asking the model",
        failure="Spends a wake, and the model calls in it, to learn that nothing changed.",
        design="Spend a wake's model calls only on what changed since the last look; the page lists why a wake can "
        "change nothing and what each cause needs.",
        reference=_page("check_world_before_model"),
    ),
    Pattern(
        key="absence_aware",
        title="Know who is away",
        failure="Chases someone who is away.",
        design="Know who is away and until when; extend the wait or go to their delegate.",
        reference=_page("absence_aware"),
    ),
    Pattern(
        key="budgeted_follow_up",
        title="Follow-ups budgeted against the deadline",
        failure="Follows up too often, or too late, or keeps acting after the date has passed.",
        design="Space reminders across the time left before the deadline.",
        reference=_page("budgeted_follow_up"),
    ),
    Pattern(
        key="bounded_asking",
        title="Bounded asking",
        failure="Asks for input indefinitely.",
        design="After a fixed number of attempts, stop asking and deliver the best available version.",
        reference=_page("bounded_asking"),
    ),
    Pattern(
        key="one_open_ask_per_person",
        title="One open ask per person",
        failure="Sends the same question, or files the same piece of work, twice.",
        design="Track what is already open with each person before asking.",
        reference=_page("one_open_ask_per_person"),
    ),
    Pattern(
        key="no_double_tick",
        title="One instance per period",
        failure="Does the weekly task twice.",
        design="A recurring task has one instance per period.",
        reference=_page("no_double_tick"),
    ),
    Pattern(
        key="honest_closure",
        title="Honest closure",
        failure="Reports done when it is not.",
        design="Closing is decided from the state of the world, not from the agent's last message.",
        reference=_page("honest_closure"),
    ),
    Pattern(
        key="confirm_names",
        title="Confirm names",
        failure="Acts on a name it guessed.",
        design=(
            "A name that matters is carried exactly as given, and an assumption is asked about before it is acted on."
        ),
        reference=_page("confirm_names"),
    ),
)

_BY_KEY = {p.key: p for p in PATTERNS}


def pattern(key: str) -> Pattern:
    """The pattern with this key; a key that names none is an error, never a quiet None."""
    if key not in _BY_KEY:
        raise KeyError(f"no pattern {key!r}; known: {', '.join(_BY_KEY)}")
    return _BY_KEY[key]
