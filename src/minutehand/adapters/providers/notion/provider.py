"""The Notion provider: the public API, the seeded workspaces, and what people do in them without the agent.

Notion pushes nothing to the agent in this simulation (webhooks are not built), so this is a
`Provider`. A person's act is a provider method (`person_edits`, `person_sets_property`,
`person_comments`, `person_archives`): it lands at the clock's time as that person's change,
recorded as actor PERSON, and the agent finds it on its next read. Nothing in the run loop
or the standing mode calls these yet; the shared port that will is not on integration-main.
"""

from __future__ import annotations

from pydantic import JsonValue

from minutehand.adapters.providers.notion import wire
from minutehand.adapters.providers.notion.app import build_app
from minutehand.adapters.providers.notion.edits import Editor
from minutehand.adapters.providers.notion.manifest import MANIFEST
from minutehand.adapters.providers.notion.seed import SeedValue, person_id, seed, value_request
from minutehand.adapters.providers.notion.state import NotionWorld
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class NotionProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    # ------------------------------------------------------------------ people acting

    def person_edits(self, page: str, text: str, *, by: str, world: Store, clock: Clock) -> None:
        """The person writes `text` at the end of a page or row, one paragraph a line."""
        editor, user = _as_person(page, by, world, clock)
        paragraphs: list[JsonValue] = [
            {"type": "paragraph", "paragraph": {"rich_text": wire.text_run(line)}} for line in text.split("\n")
        ]
        editor.append(editor.page(page).id, paragraphs, after=None, by=user)

    def person_sets_property(
        self, row: str, property: str, value: SeedValue, *, by: str, world: Store, clock: Clock
    ) -> None:
        """The person sets one property of a database row: a select's or status's option by name, a date as ISO
        text, people by email, a relation by row id, as a seed writes them."""
        editor, user = _as_person(row, by, world, clock)
        found = editor.page(row)
        if found.parent.id is None:
            raise ValueError(f"{row} is not a row of a database")
        database = editor.database(found.parent.id)
        if property not in database.schema_:
            raise ValueError(f"database {database.id} has no property {property}")
        emails = {u.email: u.id for u in NotionWorld(world).users(found.workspace) if u.email}
        raw = value_request(database.schema_[property], value, {}, emails, by_id=True)
        editor.update_page(row, {"properties": {property: raw}}, by=user)

    def person_comments(self, page: str, text: str, *, by: str, world: Store, clock: Clock) -> None:
        editor, user = _as_person(page, by, world, clock)
        editor.comment(page, None, wire.text_run(text), by=user)

    def person_archives(self, page: str, *, by: str, world: Store, clock: Clock) -> None:
        """The person moves a page or row to the trash."""
        editor, user = _as_person(page, by, world, clock)
        editor.archive(page, by=user)


def _as_person(object_id: str, email: str, world: Store, clock: Clock) -> tuple[Editor, str]:
    notion = NotionWorld(world)
    page = notion.page(object_id)
    if page is None:
        raise ValueError(f"no page {object_id} in this world")
    user = person_id(page.workspace, email)
    found = notion.user(user)
    if found is None or found.workspace != page.workspace:
        raise ValueError(f"{email} is not a member of the page's workspace")
    return Editor(notion, page.workspace, clock, actor=Actor.PERSON), user


def build() -> NotionProvider:
    return NotionProvider()
