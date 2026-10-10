"""A route's shape learned from its first answer (`domain.shapes.shape_of`): an object's fields are fixed, and a list's
items take the fields all of them have between them, none required, since a few items cannot say which fields a later
one may leave out. A push listing an ask-back with a note, then also an approval without one, was refused before."""

from __future__ import annotations

from minutehand.domain.shapes import problems, shape_of

FIRST = {"id": "req_42", "status": "needs_info", "responses": [{"kind": "ask_back", "note": "Add the quote."}]}
LATER = {
    "id": "req_42",
    "status": "approved",
    "responses": [{"kind": "ask_back", "note": "Add the quote."}, {"kind": "approve"}],
}


def test_a_list_item_without_a_field_the_first_answers_item_had_is_of_the_shape() -> None:
    assert problems(LATER, shape_of(FIRST), None) == []


def test_an_object_still_has_exactly_its_fields_and_a_list_item_none_it_never_had() -> None:
    shape = shape_of(FIRST)

    assert problems({k: v for k, v in LATER.items() if k != "status"}, shape, None) == ["$.status: is missing"]
    odd = {**LATER, "responses": [{"kind": "approve", "extra": 1}]}
    assert problems(odd, shape, None) == ["$.responses[0].extra: is not a field this shape has"]


def test_a_lists_items_take_every_field_any_of_them_has() -> None:
    both = shape_of({"rows": [{"a": 1}, {"b": "x"}]})

    assert problems({"rows": [{"a": 2, "b": "y"}]}, both, None) == []
