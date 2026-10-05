"""An inbox declaration is refused at load, naming what is wrong; so is a person who says nothing of deciding, and an
agent file written for a later version. JSONPath and the placeholder syntax are checked where they are declared."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from minutehand.adapters.agent.inboxes import HttpInboxReach
from minutehand.application.inboxes import refuse_undecided
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.inboxes import HttpInbox
from minutehand.domain.jsonpath import JsonPathError, parse, query
from minutehand.domain.scenario import Scripted, ScriptedDecision, Silent
from minutehand.domain.templates import fill
from tests.inboxes.support import deciding, scenario


def declared(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "approvals",
        "as_person": {"headers": {"Authorization": "Bearer {person.credential}"}},
        "pending": {
            "request": {"kind": "template", "url": "http://127.0.0.1:1/approvals?approver={person.email}"},
            "items": "$.items[*]",
            "id": "$.id",
            "summary": "$.summary",
        },
        "decisions": [
            {
                "name": "reject",
                "request": {
                    "kind": "template",
                    "method": "POST",
                    "url": "http://127.0.0.1:1/approvals/{item.id}",
                    "body": {"reason": "{input.reason}", "at": "{clock.now}"},
                },
                "inputs": [{"name": "reason", "description": "Why"}],
            }
        ],
    }
    base.update(changes)
    return base


def test_a_complete_declaration_loads() -> None:
    assert HttpInbox.model_validate(declared()).decision("reject") is not None


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (
            {"as_person": {"headers": {"Authorization": "Bearer {person.token}"}}},
            "the as_person header 'Authorization' names {person.token}",
        ),
        (
            {"pending": {**declared()["pending"], "id": "id"}},
            "a JSONPath query starts at the root, $",
        ),
        (
            {"pending": {**declared()["pending"], "items": "$.items[?@.open]"}},
            "filter selectors ([?...]) are not supported here",
        ),
        (
            {"pending": {**declared()["pending"], "items": "$.items[0:2]"}},
            "slices ([start:end]) are not supported here",
        ),
        (
            {"pending": {**declared()["pending"], "paging": {"next": "$.next"}}},
            "a list paged with no `param` places `{page.cursor}` in its request",
        ),
        (
            {"pending": {**declared()["pending"], "request": {"kind": "template", "url": "http://h/{item.id}"}}},
            "the list's request's url names {item.id}",
        ),
        (
            {"decisions": [{**declared()["decisions"][0], "inputs": []}]},
            "decision 'reject''s body names {input.reason}",
        ),
        (
            {"decisions": [declared()["decisions"][0], declared()["decisions"][0]]},
            "inbox 'approvals' declares decision reject twice",
        ),
        (
            {"decisions": [{**declared()["decisions"][0], "request": {"kind": "smoke", "url": "x"}}]},
            "does not match any of the expected tags: 'template', 'operation'",
        ),
        (
            {"pending": {**declared()["pending"], "request": {"kind": "template", "url": "u", "body": {"a": 1}}}},
            "the list's request is a GET and has no body",
        ),
    ],
)
def test_a_wrong_declaration_is_refused_naming_what_is_wrong(change: dict[str, Any], said: str) -> None:
    with pytest.raises(ValidationError, match=_escaped(said)):
        HttpInbox.model_validate(declared(**change))


def _escaped(text: str) -> str:
    import re

    return re.escape(text)


def test_two_inboxes_of_one_name_are_refused() -> None:
    with pytest.raises(ValidationError, match="inbox declared twice: approvals"):
        AgentUnderTest.model_validate(
            {"name": "a", "wakes": [{"kind": "command", "argv": ["x"]}], "inboxes": [declared(), declared()]}
        )


def test_an_agent_file_written_for_a_later_version_is_refused_saying_so() -> None:
    with pytest.raises(
        ValidationError, match="written for version 2 of the agent file, and this Minutehand reads up to"
    ):
        AgentUnderTest.model_validate({"version": 2, "name": "a", "wakes": [{"kind": "command", "argv": ["x"]}]})
    assert AgentUnderTest.model_validate({"version": 1, "name": "a", "wakes": [{"kind": "command", "argv": ["x"]}]})


def test_a_person_who_can_receive_items_and_says_nothing_of_deciding_is_refused_naming_them() -> None:
    reach = HttpInboxReach(HttpInbox.model_validate(declared()), {"nadia": "t"})
    with pytest.raises(RunRefused, match="nadia can receive items in inbox approvals and their script says nothing"):
        refuse_undecided(scenario(Scripted(replies=[])), [reach])
    refuse_undecided(scenario(Silent()), [reach])
    refuse_undecided(scenario(Scripted(replies=[], decisions=[])), [reach])


def test_a_person_without_a_credential_cannot_receive_items_and_is_not_refused() -> None:
    reach = HttpInboxReach(HttpInbox.model_validate(declared()), {})
    refuse_undecided(scenario(Scripted(replies=[])), [reach])


@pytest.mark.parametrize(
    ("decision", "said"),
    [
        (ScriptedDecision(decision="approve"), "nadia decides 'approve', which no inbox the agent declares offers"),
        (ScriptedDecision(decision="reject"), "nadia makes decision 'reject' without reason, which it requires"),
        (
            ScriptedDecision(decision="reject", inputs={"reason": "x", "mood": "y"}),
            "nadia gives decision 'reject' mood; it takes reason",
        ),
        (ScriptedDecision(decision="reject", inbox="elsewhere"), "nadia decides in inbox elsewhere"),
    ],
)
def test_a_scripted_decision_the_inbox_cannot_take_is_refused(decision: ScriptedDecision, said: str) -> None:
    reach = HttpInboxReach(HttpInbox.model_validate(declared()), {"nadia": "t"})
    with pytest.raises(RunRefused, match=_escaped(said)):
        refuse_undecided(scenario(deciding(decision)), [reach])


def test_jsonpath_reads_the_rfc_9535_selectors_it_supports() -> None:
    document = {"items": [{"id": 1, "a b": "x"}, {"id": 2, "tags": ["p", "q"]}], "next": None}
    assert query(document, "$.items[*].id") == [1, 2]
    assert query(document, "$.items[-1].tags[0]") == ["p"]
    assert query(document, "$['items'][0]['a b']") == ["x"]
    assert query(document, "$..id") == [1, 2]
    assert query(document, "$.items[0,1].id") == [1, 2]
    assert query(document, "$.missing") == []
    assert query([3, 4], "$[*]") == [3, 4]
    with pytest.raises(JsonPathError, match="expected a member name"):
        parse("$.")


def test_a_template_fills_its_names_inside_strings_and_leaves_structure() -> None:
    filled = fill(
        {"to": "{person.email}", "n": 1, "said": ["{input.reason}", "{other.thing}"]},
        {
            "person.email": 'a"b@example.com',
            "input.reason": "{person.email}",
        },
    )
    assert filled == {"to": 'a"b@example.com', "n": 1, "said": ["{person.email}", "{other.thing}"]}
