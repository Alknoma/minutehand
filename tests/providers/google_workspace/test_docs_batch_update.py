"""Docs v1 `documents.create`, `documents.get` and `documents.batchUpdate`, index by index.

The requests are the ones a production Drive adapter sends: text inserted at index 1, a heading styled over its
range, a bulleted list, a table whose cells are filled at `table_start + 4 + r * (2 * columns + 1) + 2 * c`,
counted back to front, and a link."""

from __future__ import annotations

import httpx

from minutehand.adapters.providers.google_workspace import docs as docs_module
from minutehand.adapters.providers.google_workspace import state
from minutehand.domain.world import Actor, Operation
from tests.providers.google_workspace.drive_world import AUTH, LATER, Answer, Drive, answer, create_doc, issue

JsonObject = dict[str, object]


async def new_doc(docs: httpx.AsyncClient, title: str = "Plan") -> str:
    made = answer(await docs.post("/v1/documents", json={"title": title}, headers=AUTH))
    return str(made["documentId"])


async def batch(docs: httpx.AsyncClient, document_id: str, requests: list[JsonObject], status: int = 200) -> Answer:
    return answer(
        await docs.post(f"/v1/documents/{document_id}:batchUpdate", json={"requests": requests}, headers=AUTH), status
    )


async def get(docs: httpx.AsyncClient, document_id: str, **params: str) -> Answer:
    return answer(await docs.get(f"/v1/documents/{document_id}", params=params, headers=AUTH))


def content(document: Answer) -> list[JsonObject]:
    body = document["body"]
    assert isinstance(body, dict)
    found = body["content"]
    assert isinstance(found, list)
    return found


def text_of(element: JsonObject) -> str:
    paragraph = element["paragraph"]
    assert isinstance(paragraph, dict)
    return "".join(str(e["textRun"]["content"]) for e in paragraph["elements"] if "textRun" in e)


def spans(document: Answer) -> list[tuple[int | None, int, str]]:
    """Each structural element: its start, its end, and what it is."""
    found: list[tuple[int | None, int, str]] = []
    for element in content(document):
        start = element["startIndex"] if "startIndex" in element else None
        assert start is None or isinstance(start, int)
        end = element["endIndex"]
        assert isinstance(end, int)
        kind = "section" if "sectionBreak" in element else "table" if "table" in element else text_of(element)
        found.append((start, end, kind))
    return found


def written(drive: Drive) -> list[tuple[Actor, Operation]]:
    return [
        (e.actor, e.operation)
        for e in drive.store.events()
        if e.entity.kind is state.file_ref("").kind and e.operation is not Operation.READ
    ]


PLAN = "Plan\nIntro paragraph.\nFirst\nSecond\n"


async def test_a_document_written_with_a_heading_a_list_a_table_and_a_link_reads_back_index_by_index(
    drive: Drive, docs: httpx.AsyncClient
) -> None:
    document_id = await new_doc(docs)
    drive.clock.jump(LATER)
    table_at = 1 + len(PLAN)
    cells = {(r, c): table_at + 4 + r * (2 * 2 + 1) + c * 2 for r in range(2) for c in range(2)}
    replied = await batch(
        docs,
        document_id,
        [
            {"insertText": {"location": {"index": 1}, "text": PLAN}},
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": 1, "endIndex": 6},
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            },
            {
                "createParagraphBullets": {
                    "range": {"startIndex": 23, "endIndex": 36},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            },
            {
                "updateTextStyle": {
                    "range": {"startIndex": 6, "endIndex": 11},
                    "textStyle": {"link": {"url": "https://example.com/intro"}},
                    "fields": "link",
                }
            },
            {"insertTable": {"rows": 2, "columns": 2, "location": {"index": table_at}}},
            *[
                {"insertText": {"location": {"index": cells[r, c]}, "text": text}}
                for (r, c), text in sorted({(0, 0): "a", (0, 1): "b", (1, 0): "c", (1, 1): "d"}.items(), reverse=True)
            ],
        ],
    )
    document = await get(docs, document_id)

    assert cells == {(0, 0): 40, (0, 1): 42, (1, 0): 45, (1, 1): 47}
    assert replied["replies"] == [{}] * 9 and replied["documentId"] == document_id
    assert spans(document) == [
        (None, 1, "section"),
        (1, 6, "Plan\n"),
        (6, 23, "Intro paragraph.\n"),
        (23, 29, "First\n"),
        (29, 36, "Second\n"),
        (36, 37, "\n"),
        (37, 52, "table"),
        (52, 53, "\n"),
    ]
    elements = content(document)
    heading = elements[1]["paragraph"]
    assert isinstance(heading, dict)
    assert heading["paragraphStyle"]["namedStyleType"] == "HEADING_1" and heading["paragraphStyle"]["headingId"]
    intro = elements[2]["paragraph"]
    assert isinstance(intro, dict)
    assert [(e["startIndex"], e["endIndex"], e["textRun"]) for e in intro["elements"]] == [
        (6, 11, {"content": "Intro", "textStyle": {"link": {"url": "https://example.com/intro"}}}),
        (11, 23, {"content": " paragraph.\n", "textStyle": {}}),
    ]
    first, second = elements[3]["paragraph"], elements[4]["paragraph"]
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["bullet"] == second["bullet"] and "bullet" not in intro
    list_id = first["bullet"]["listId"]
    lists = document["lists"]
    assert isinstance(lists, dict) and list(lists) == [list_id]
    assert lists[list_id]["listProperties"]["nestingLevels"][0]["glyphSymbol"] == "●"
    table = elements[6]["table"]
    assert isinstance(table, dict) and (table["rows"], table["columns"]) == (2, 2)
    grid = [
        [
            (
                cell["startIndex"],
                cell["endIndex"],
                [(p["startIndex"], p["endIndex"], text_of(p)) for p in cell["content"]],
            )
            for cell in row["tableCells"]
        ]
        for row in table["tableRows"]
    ]
    assert grid == [
        [(39, 42, [(40, 42, "a\n")]), (42, 45, [(43, 45, "b\n")])],
        [(46, 49, [(47, 49, "c\n")]), (49, 52, [(50, 52, "d\n")])],
    ]
    assert [(r["startIndex"], r["endIndex"]) for r in table["tableRows"]] == [(38, 45), (45, 52)]
    assert written(drive)[-1] == (Actor.AGENT, Operation.UPDATE)
    stored = drive.world.file(document_id)
    assert stored is not None and stored.file.modifiedTime == "2026-09-14T11:37:11.250Z"


async def test_the_written_document_exports_with_its_structure(
    drive: Drive, docs: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    document_id = await new_doc(docs)
    await batch(
        docs,
        document_id,
        [
            {"insertText": {"location": {"index": 1}, "text": PLAN}},
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": 1, "endIndex": 2},
                    "paragraphStyle": {"namedStyleType": "HEADING_2"},
                    "fields": "namedStyleType",
                }
            },
            {
                "createParagraphBullets": {
                    "range": {"startIndex": 23, "endIndex": 36},
                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN",
                }
            },
        ],
    )
    markdown = await api.get(
        f"/drive/v3/files/{document_id}/export", params={"mimeType": "text/markdown"}, headers=AUTH
    )
    plain = await api.get(f"/drive/v3/files/{document_id}/export", params={"mimeType": "text/plain"}, headers=AUTH)
    assert markdown.text == "## Plan\nIntro paragraph.\n1. First\n1. Second\n"
    assert plain.content == "﻿Plan\r\nIntro paragraph.\r\n1. First\r\n1. Second\r\n\r\n".encode()


async def test_indexes_count_utf16_code_units(docs: httpx.AsyncClient) -> None:
    document_id = await new_doc(docs)
    await batch(docs, document_id, [{"insertText": {"location": {"index": 1}, "text": "a😀b\n"}}])
    inside_the_pair = await batch(
        docs, document_id, [{"insertText": {"location": {"index": 3}, "text": "x"}}], status=400
    )
    await batch(docs, document_id, [{"insertText": {"location": {"index": 4}, "text": "!"}}])
    document = await get(docs, document_id)
    assert spans(document)[1] == (1, 7, "a😀!b\n")
    error = inside_the_pair["error"]
    assert isinstance(error, dict) and error["status"] == "INVALID_ARGUMENT"
    assert "requests[0].insertText" in str(error["message"]) and "surrogate pair" in str(error["message"])


async def test_deleting_across_a_paragraph_boundary_merges_and_keeps_the_first_paragraphs_style(
    docs: httpx.AsyncClient,
) -> None:
    document_id = await new_doc(docs)
    await batch(
        docs,
        document_id,
        [
            {"insertText": {"location": {"index": 1}, "text": "Title\nBody text\n"}},
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": 1, "endIndex": 7},
                    "paragraphStyle": {"namedStyleType": "TITLE"},
                    "fields": "namedStyleType",
                }
            },
            {"deleteContentRange": {"range": {"startIndex": 6, "endIndex": 12}}},
        ],
    )
    document = await get(docs, document_id)
    assert spans(document)[1:] == [(1, 11, "Titletext\n"), (11, 12, "\n")]
    merged = content(document)[1]["paragraph"]
    assert isinstance(merged, dict) and merged["paragraphStyle"]["namedStyleType"] == "TITLE"


async def test_a_batch_with_one_bad_request_changes_nothing_and_names_the_request(
    drive: Drive, docs: httpx.AsyncClient
) -> None:
    document_id = await new_doc(docs)
    before = await get(docs, document_id)
    head = drive.store.head()
    refused = await batch(
        docs,
        document_id,
        [
            {"insertText": {"location": {"index": 1}, "text": "kept?"}},
            {"insertText": {"location": {"index": 99}, "text": "no"}},
        ],
        status=400,
    )
    after = await get(docs, document_id)
    error = refused["error"]
    assert isinstance(error, dict)
    assert error["message"] == (
        "Invalid requests[1].insertText: Index 99 must be less than the end index of the referenced segment, 7."
    )
    assert spans(after) == spans(before) == [(None, 1, "section"), (1, 2, "\n")]
    assert [e for e in drive.store.events(since=head) if e.operation is not Operation.READ] == []


async def test_the_last_newline_of_a_segment_cannot_be_deleted_is_refused(docs: httpx.AsyncClient) -> None:
    document_id = await new_doc(docs)
    await batch(docs, document_id, [{"insertText": {"location": {"index": 1}, "text": "abc"}}])
    refused = await batch(
        docs, document_id, [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}}], status=400
    )
    assert "newline character at the end of the segment" in str(refused["error"])


async def test_a_stale_required_revision_is_refused_and_the_current_one_writes(docs: httpx.AsyncClient) -> None:
    document_id = await new_doc(docs)
    first = await get(docs, document_id)
    written_once = await batch(docs, document_id, [{"insertText": {"location": {"index": 1}, "text": "one"}}])
    stale = await docs.post(
        f"/v1/documents/{document_id}:batchUpdate",
        json={
            "requests": [{"insertText": {"location": {"index": 1}, "text": "two"}}],
            "writeControl": {"requiredRevisionId": first["revisionId"]},
        },
        headers=AUTH,
    )
    current = written_once["writeControl"]
    assert isinstance(current, dict)
    fresh = await docs.post(
        f"/v1/documents/{document_id}:batchUpdate",
        json={
            "requests": [{"insertText": {"location": {"index": 1}, "text": "two "}}],
            "writeControl": {"requiredRevisionId": current["requiredRevisionId"]},
        },
        headers=AUTH,
    )
    assert stale.status_code == 400 and stale.json()["error"]["status"] == "FAILED_PRECONDITION"
    assert fresh.status_code == 200
    assert spans(await get(docs, document_id))[1] == (1, 9, "two one\n")
    assert first["revisionId"] != current["requiredRevisionId"]


async def test_a_page_break_an_image_a_replacement_and_a_named_range_keep_their_indexes(
    docs: httpx.AsyncClient,
) -> None:
    document_id = await new_doc(docs)
    replied = await batch(
        docs,
        document_id,
        [
            {"insertText": {"location": {"index": 1}, "text": "Owner: TBD\nNotes\n"}},
            {"createNamedRange": {"name": "notes", "range": {"startIndex": 12, "endIndex": 17}}},
            {"insertPageBreak": {"location": {"index": 12}}},
            {"insertInlineImage": {"location": {"index": 1}, "uri": "https://drive.google.com/uc?id=chart"}},
            {"replaceAllText": {"containsText": {"text": "tbd", "matchCase": False}, "replaceText": "Rosa"}},
        ],
    )
    document = await get(docs, document_id)
    replies = replied["replies"]
    assert isinstance(replies, list)
    image_id = replies[3]["insertInlineImage"]["objectId"]
    assert replies[4] == {"replaceAllText": {"occurrencesChanged": 1}}
    assert replies[1]["createNamedRange"]["namedRangeId"]
    assert spans(document)[1:] == [(1, 14, "Owner: Rosa\n"), (14, 16, "\n"), (16, 22, "Notes\n"), (22, 23, "\n")]
    first = content(document)[1]["paragraph"]
    assert isinstance(first, dict) and first["elements"][0]["inlineObjectElement"]["inlineObjectId"] == image_id
    second = content(document)[2]["paragraph"]
    assert isinstance(second, dict) and "pageBreak" in second["elements"][0]
    ranges = document["namedRanges"]
    assert isinstance(ranges, dict)
    assert ranges["notes"]["namedRanges"][0]["ranges"] == [{"startIndex": 16, "endIndex": 21}]
    objects = document["inlineObjects"]
    assert isinstance(objects, dict)
    assert objects[image_id]["inlineObjectProperties"]["embeddedObject"]["imageProperties"]["sourceUri"] == (
        "https://drive.google.com/uc?id=chart"
    )


async def test_tabs_content_puts_the_body_under_the_first_tab(docs: httpx.AsyncClient) -> None:
    document_id = await new_doc(docs)
    tabbed = await get(docs, document_id, includeTabsContent="true")
    tabs = tabbed["tabs"]
    assert isinstance(tabs, list) and "body" not in tabbed
    assert tabs[0]["tabProperties"]["tabId"] == "t.0" and tabs[0]["documentTab"]["body"]["content"][1]["endIndex"] == 2


async def test_a_request_kind_this_fake_does_not_build_is_refused_501(docs: httpx.AsyncClient) -> None:
    document_id = await new_doc(docs)
    refused = await batch(docs, document_id, [{"mergeTableCells": {"tableRange": {}}}], status=501)
    error = refused["error"]
    assert isinstance(error, dict) and error["status"] == "UNIMPLEMENTED"


async def test_a_reader_cannot_write_a_document_is_refused_403(
    drive: Drive, docs: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    made = await create_doc(api, "Shared With Dov", "text")
    await api.post(
        f"/drive/v3/files/{made['id']}/permissions",
        json={"type": "user", "role": "reader", "emailAddress": "dov@example.com"},
        headers=AUTH,
    )
    issue(drive.store, "ya29.dov", "dov@example.com")
    dov = {"Authorization": "Bearer ya29.dov"}
    read = await docs.get(f"/v1/documents/{made['id']}", headers=dov)
    write = await docs.post(
        f"/v1/documents/{made['id']}:batchUpdate",
        json={"requests": [{"insertText": {"location": {"index": 1}, "text": "x"}}]},
        headers=dov,
    )
    assert read.status_code == 200
    assert write.status_code == 403 and write.json()["error"]["status"] == "PERMISSION_DENIED"


def test_a_markdown_document_is_seeded_with_headings_lists_and_a_table() -> None:
    body = docs_module.from_markdown(
        "seeded",
        "# Launch\nSee [the brief](https://example.com/b) and **act**.\n- one\n  - nested\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n",
    )
    rendered = docs_module.render("seeded", "Launch", "rev", body, tabs=False)
    styles = [
        (e.paragraph.paragraphStyle.namedStyleType, e.paragraph.bullet.nestingLevel if e.paragraph.bullet else None)
        for e in rendered.body.content  # type: ignore[union-attr]
        if e.paragraph is not None
    ]
    assert styles[:4] == [
        (docs_module.NamedStyle.HEADING_1, None),
        (docs_module.NamedStyle.NORMAL_TEXT, None),
        (docs_module.NamedStyle.NORMAL_TEXT, None),
        (docs_module.NamedStyle.NORMAL_TEXT, 1),
    ]
    assert any(e.table is not None for e in rendered.body.content)  # type: ignore[union-attr]
    assert docs_module.text_of(body, markdown=True).splitlines()[:2] == [
        "# Launch",
        "See [the brief](https://example.com/b) and **act**.",
    ]
