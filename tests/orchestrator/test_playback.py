"""What a fork's playback can and cannot send the agent again, read from the parent's record over the real store: a
provider whose requests to the agent the door does not yet record, a decision made in the agent's own product, and a
person's answer on a captured channel each name themselves; a provider whose sends are recorded does not."""

from __future__ import annotations

from pathlib import Path

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.playback import sends_not_recorded
from minutehand.application.run_clock import RunClock
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    InboxAct,
    InboxCall,
    MessageSnapshot,
    Operation,
)
from tests.support.scenarios import T0


def _message(store: SqliteStore, provider: str, text: str) -> int:
    return store.apply(
        Change(
            entity=EntityRef(provider=provider, kind=EntityKind.MESSAGE, external_id=text),
            operation=Operation.CREATE,
            actor=Actor.PERSON,
            body="{}",
            after=MessageSnapshot(text=text, channel="c", recipient_emails=[]),
        )
    ).seq


def test_a_provider_whose_sends_the_door_does_not_record_is_named_once_it_acted(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "w.db", "r", RunClock(T0))
    first = _message(store, "slack", "hi")
    second = _message(store, "microsoft", "hello")
    senders = frozenset({"microsoft", "github"})
    assert sends_not_recorded(store, first, senders=senders, channels=frozenset()) == []
    assert sends_not_recorded(store, second, senders=senders, channels=frozenset()) == [
        "microsoft's requests to the agent (pushed events, webhooks, deliveries)"
    ]


def test_a_decision_in_the_agents_product_is_named_and_a_listing_is_not(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "w.db", "r", RunClock(T0))
    for act in (InboxAct.LIST, InboxAct.DECIDE):
        head = store.head()
        store.attach(
            Exchange(
                method="POST",
                host="127.0.0.1:8720",
                path="/approvals",
                status=200,
                inbox_call=InboxCall(inbox="approvals", person="nadia", act=act),
            ),
            first_seq=head + 1,
            last_seq=head,
        )
    assert sends_not_recorded(store, store.head(), senders=frozenset(), channels=frozenset()) == [
        "decisions and uses made as a person in the agent's own product (approvals)"
    ]


def test_an_answer_on_a_captured_channel_is_named(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "w.db", "r", RunClock(T0))
    seq = _message(store, "mail", "Yes, 40k.")
    assert sends_not_recorded(store, seq, senders=frozenset(), channels=frozenset({"mail"})) == [
        "people's answers delivered on the captured channel 'mail'"
    ]
    assert sends_not_recorded(store, seq - 1, senders=frozenset(), channels=frozenset({"mail"})) == []
