"""The Notion provider: the public API, the seeded workspaces, what people do in them without the agent, and
the webhooks that tell an integration of it.

It is `ChangesDocuments`: a `DocumentHappening` lands on the page or database row its seeded document was written as
(`seed.document_page`), as that person, recorded as actor PERSON: `Edited` appends paragraphs, `Renamed` sets the
title, `Trashed` archives, `Commented` writes a comment, `FieldSet` sets a row's property from its text. A page
cannot be moved or shared by a person here, and the manifest says so, so a scenario asking for either is refused
at load. It is `NotifiesChanges`: a seeded webhook subscription (`NotionSeed.webhooks`) is told of every change
it is owed, signed as Notion signs it (`webhooks.py`).
"""

from __future__ import annotations

from pydantic import JsonValue

from minutehand.adapters.answering import guarded
from minutehand.adapters.providers.notion import webhooks, wire
from minutehand.adapters.providers.notion.app import build_app
from minutehand.adapters.providers.notion.edits import Editor
from minutehand.adapters.providers.notion.manifest import MANIFEST
from minutehand.adapters.providers.notion.seed import (
    NotionSeed,
    SeedValue,
    document_page,
    plan,
    seed,
    user_id,
    value_request,
)
from minutehand.adapters.providers.notion.state import NotionWorld, is_row
from minutehand.domain.errors import Rendered
from minutehand.domain.provider import Manifest, PersonChange, fault_fragment
from minutehand.domain.scenario import (
    Commented,
    DocumentHappening,
    Edited,
    FieldSet,
    Moved,
    Person,
    Renamed,
    Scenario,
    Shared,
    Trashed,
)
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class NotionProvider:
    manifest: Manifest = MANIFEST
    seed_model = NotionSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return guarded(build_app(world, clock), self, provider=self.manifest.key)

    def error(self, status: int, code: str, message: str) -> Rendered:
        """Notion's error object, coded so `notion-client` raises `APIResponseError` with `message` as its text;
        `code`, Minutehand's own, is not one of Notion's codes, and the answer's `x-minutehand-answer` header says
        what it was instead."""
        del code
        return wire.error_answer(status, message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    # ------------------------------------------------------------------ ChangesDocuments

    def change(self, happening: DocumentHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        """The person does the happening's action to the page or row its document was written as. A page the agent
        has deleted for good is not there, and nothing is written."""
        page = document_page(scenario, scenario.happening_document(happening).title)
        found = NotionWorld(world).page(page)
        if found is None:
            return
        person = next(p for p in scenario.people if p.key == happening.person)
        editor, user = _as_person(page, person, world, clock)
        action = happening.action
        if isinstance(action, Edited):
            paragraphs: list[JsonValue] = [
                {"type": "paragraph", "paragraph": {"rich_text": wire.text_run(line)}}
                for line in action.append.split("\n")
            ]
            editor.append(found.id, paragraphs, after=None, by=user)
        elif isinstance(action, Renamed):
            editor.update_page(
                page, {"properties": {_title_property(editor, found): {"title": wire.text_run(action.to)}}}, by=user
            )
        elif isinstance(action, Trashed):
            editor.archive(page, by=user)
        elif isinstance(action, Commented):
            editor.comment(page, None, wire.text_run(action.text), by=user)
        elif isinstance(action, FieldSet):
            self._sets(editor, found, action, scenario, user)
        else:
            assert isinstance(action, Moved | Shared)
            raise ValueError(f"a person cannot {action.kind} a Notion page here; the scenario is refused at load")

    @staticmethod
    def _sets(editor: Editor, row: wire.StoredPage, action: FieldSet, scenario: Scenario, user: str) -> None:
        """One property of a database row, from the happening's text: a number, `true`/`false`, comma-separated
        option names, person keys or row ids for a property holding several, anything else as written."""
        if not is_row(row):
            raise ValueError(f"{row.id} is a page, not a row of a database, so it has no {action.field!r} to set")
        assert row.parent.id is not None
        database = editor.database(row.parent.id)
        if action.field not in database.schema_:
            raise ValueError(f"database {database.id} has no property {action.field!r}")
        schema = database.schema_[action.field]
        kind = wire.schema_type(schema)
        value: SeedValue = action.value
        if kind is wire.PropertyType.NUMBER:
            value = float(action.value) if "." in action.value else int(action.value)
        elif kind is wire.PropertyType.CHECKBOX:
            value = action.value.strip().lower() == "true"
        elif kind in (wire.PropertyType.MULTI_SELECT, wire.PropertyType.PEOPLE, wire.PropertyType.RELATION):
            value = [v.strip() for v in action.value.split(",") if v.strip()]
        users = editor.world.users(row.workspace)
        members = {u.id for u in users}
        named = {u.email: u.id for u in users if u.email}
        named |= {p.key: found for p in scenario.people if (found := user_id(row.workspace, p)) in members}
        raw = value_request(schema, value, {}, named, by_id=True)
        editor.update_page(row.id, {"properties": {action.field: raw}}, by=user)

    # ------------------------------------------------------------------ ChangesPeople

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """A workspace owner removes the person from every workspace they are a member of: `/v1/users` lists them no
        more and `/v1/users/{id}` answers `object_not_found`; pages and comments they wrote still name them."""
        if change is not PersonChange.REMOVED:
            raise ValueError(f"notion has no way to show a person {change.value}")
        notion = NotionWorld(world)
        members = [
            u
            for w in notion.workspaces()
            for u in notion.users(w.id)
            if u.type is wire.UserType.PERSON and u.id == user_id(w.id, person)
        ]
        if not members:
            raise ValueError(f"{person.key} is not a member of any Notion workspace")
        for member in members:
            notion.write_user(member.model_copy(update={"removed": True}), operation=Operation.UPDATE)
        del clock

    # ------------------------------------------------------------------ DeclaresFaults

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`NotionSeed.faults`, on a world already open, armed after those already armed. An integration is named
        by its key, as in the seed, and must be one this world holds."""
        found = fault_fragment(NotionSeed, faults, frozenset({"faults"})).faults
        notion = NotionWorld(world)
        bots = {i.key: i.id for w in notion.workspaces() for i in notion.integrations(w.id)}
        schedule = notion.declared()
        notion.write_schedule(
            schedule.model_copy(update={"faults": [*schedule.faults, *plan(found, bots)]}), declared=True
        )
        del clock

    # ------------------------------------------------------------------ NotifiesChanges

    def watched(self, world: Store, clock: Clock) -> bool:
        return webhooks.watching(NotionWorld(world))

    async def notify(self, world: Store, clock: Clock) -> None:
        await webhooks.deliver(NotionWorld(world), clock)


def _title_property(editor: Editor, page: wire.StoredPage) -> str:
    if not is_row(page):
        return "title"
    assert page.parent.id is not None
    schema = editor.database(page.parent.id).schema_
    return next(name for name, prop in schema.items() if wire.schema_type(prop) is wire.PropertyType.TITLE)


def _as_person(object_id: str, person: Person, world: Store, clock: Clock) -> tuple[Editor, str]:
    notion = NotionWorld(world)
    page = notion.page(object_id)
    if page is None:
        raise ValueError(f"no page {object_id} in this world")
    user = user_id(page.workspace, person)
    found = notion.user(user)
    if found is None or found.workspace != page.workspace:
        raise ValueError(f"{person.key} is not a member of the page's workspace")
    return Editor(notion, page.workspace, clock, actor=Actor.PERSON), user


def build() -> NotionProvider:
    return NotionProvider()
