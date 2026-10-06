"""A provider seed fragment grows what the world's seed holds: a list item naming the identity of one already held
(as its model's `Keyed.IDENTITY` declares it) is merged into that item, never added as a second of the same thing."""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from minutehand.domain.provider import Keyed, merged_seed
from minutehand.domain.scenario import Model


class Page(Model, Keyed):
    IDENTITY: ClassVar[tuple[str, ...]] = ("key",)
    key: str
    title: str = ""
    tags: list[str] = []


class Space(Model, Keyed):
    IDENTITY: ClassVar[tuple[str, ...]] = ("owner", "name")
    owner: str
    name: str
    pages: list[Page] = []


class Plain(Model):
    name: str


class Whole(Model):
    spaces: list[Space] = []
    plains: list[Plain] = []


HELD = json.dumps({"spaces": [{"owner": "o", "name": "a", "pages": [{"key": "p1", "title": "t", "tags": ["x"]}]}]})


def _merged(fragment: object) -> Whole:
    return Whole.model_validate_json(merged_seed(Whole, HELD, json.dumps(fragment)))


def test_an_item_naming_a_held_identity_grows_the_held_item() -> None:
    found = _merged({"spaces": [{"owner": "o", "name": "a", "pages": [{"key": "p1", "tags": ["y"]}, {"key": "p2"}]}]})
    assert len(found.spaces) == 1
    assert [p.key for p in found.spaces[0].pages] == ["p1", "p2"]
    assert found.spaces[0].pages[0].tags == ["x", "y"]


def test_an_item_naming_another_identity_is_added_beside() -> None:
    found = _merged({"spaces": [{"owner": "o", "name": "b"}]})
    assert [(s.owner, s.name) for s in found.spaces] == [("o", "a"), ("o", "b")]


def test_a_list_of_an_unkeyed_model_only_grows() -> None:
    held = json.dumps({"plains": [{"name": "n"}]})
    found = Whole.model_validate_json(merged_seed(Whole, held, json.dumps({"plains": [{"name": "n"}]})))
    assert len(found.plains) == 2


def test_a_held_item_given_a_contradicting_value_is_refused_naming_it() -> None:
    with pytest.raises(ValueError, match=r"spaces\[owner='o', name='a'\]\.pages\[key='p1'\]\.title"):
        _merged({"spaces": [{"owner": "o", "name": "a", "pages": [{"key": "p1", "title": "other"}]}]})
