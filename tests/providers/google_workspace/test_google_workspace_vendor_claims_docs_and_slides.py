"""Facts about real Google Docs v1 and Slides v1 that an older stand-in kept, each held here against this fake.

Each docstring names the class: DOCUMENTED (the cited page says so) or OBSERVED (asserted from a sighting of the
real service, no page says so). `src/minutehand/adapters/providers/google_workspace/CLAIMS.md` is the index."""

from __future__ import annotations

import httpx

from tests.providers.google_workspace.drive_world import AUTH, Answer, answer
from tests.providers.google_workspace.test_docs_batch_update import batch, content, get, new_doc, spans, text_of

JsonObject = dict[str, object]
SHIP = "\U0001f6a2"
"""One Python character, two UTF-16 code units."""


def bold_runs(document: Answer) -> list[str]:
    runs: list[str] = []
    for element in content(document):
        if "paragraph" not in element:
            continue
        paragraph = element["paragraph"]
        assert isinstance(paragraph, dict)
        for child in paragraph["elements"]:
            run = child.get("textRun") if isinstance(child, dict) else None
            if isinstance(run, dict) and run.get("textStyle", {}).get("bold"):
                runs.append(str(run["content"]))
    return runs


async def test_a_style_range_after_an_emoji_is_counted_in_utf16_and_lands_on_its_word(docs: httpx.AsyncClient) -> None:
    """DOCUMENTED: Docs indexes count UTF-16 code units, so an emoji takes two and every range after it is counted
    that way. https://developers.google.com/workspace/docs/api/concepts/structure"""
    document_id = await new_doc(docs)
    prefix = f"Sailing {SHIP} "
    assert len(prefix.encode("utf-16-le")) // 2 == 11

    await batch(
        docs,
        document_id,
        [
            {"insertText": {"location": {"index": 1}, "text": f"{prefix}today\n"}},
            {
                "updateTextStyle": {
                    "range": {"startIndex": 12, "endIndex": 17},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            },
        ],
    )

    assert bold_runs(await get(docs, document_id)) == ["today"]


async def test_inserting_at_index_zero_is_refused_400_invalid_argument(docs: httpx.AsyncClient) -> None:
    """DOCUMENTED: text goes inside the bounds of an existing paragraph, and index 0 is the body's section break.
    https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#inserttextrequest"""
    document_id = await new_doc(docs)

    refused = await batch(docs, document_id, [{"insertText": {"location": {"index": 0}, "text": "x"}}], status=400)

    error = refused["error"]
    assert isinstance(error, dict) and error["status"] == "INVALID_ARGUMENT"
    assert "insertText" in str(error["message"])


async def test_inserting_at_the_bodys_end_index_is_refused_400(docs: httpx.AsyncClient) -> None:
    """DOCUMENTED: the end index sits after the body's final newline, outside every paragraph, so it is not a place
    text can go; the refusal names the segment's end index.
    https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#inserttextrequest"""
    document_id = await new_doc(docs)
    end = content(await get(docs, document_id))[-1]["endIndex"]
    assert end == 2

    refused = await batch(docs, document_id, [{"insertText": {"location": {"index": end}, "text": "x"}}], status=400)

    assert "end index" in str(refused["error"])


async def test_appending_just_before_the_final_newline_lands_at_the_end(docs: httpx.AsyncClient) -> None:
    """DOCUMENTED: the last index inside the body's last paragraph is one short of its end; text written there ends
    the document. https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#inserttextrequest"""
    document_id = await new_doc(docs)
    await batch(docs, document_id, [{"insertText": {"location": {"index": 1}, "text": "Moored.\n"}}])
    end = content(await get(docs, document_id))[-1]["endIndex"]
    assert isinstance(end, int)

    await batch(docs, document_id, [{"insertText": {"location": {"index": end - 1}, "text": "Cast off."}}])

    texts = [text_of(e) for e in content(await get(docs, document_id)) if "paragraph" in e]
    assert texts == ["Moored.\n", "Cast off.\n"]


async def test_documents_create_makes_a_blank_document_not_one_holding_its_title(docs: httpx.AsyncClient) -> None:
    """DOCUMENTED: `documents.create` makes a blank document with the given title.
    https://developers.google.com/workspace/docs/api/how-tos/documents"""
    made = answer(await docs.post("/v1/documents", json={"title": "Pilotage Log"}, headers=AUTH))

    assert made["title"] == "Pilotage Log"
    assert spans(made) == [(None, 1, "section"), (1, 2, "\n")]


async def new_deck(presentations: httpx.AsyncClient, title: str = "Port Review") -> Answer:
    return answer(await presentations.post("/v1/presentations", json={"title": title}, headers=AUTH))


async def deck_batch(presentations: httpx.AsyncClient, deck_id: str, requests: list[JsonObject]) -> Answer:
    return answer(
        await presentations.post(f"/v1/presentations/{deck_id}:batchUpdate", json={"requests": requests}, headers=AUTH)
    )


async def deck_get(presentations: httpx.AsyncClient, deck_id: str) -> Answer:
    return answer(await presentations.get(f"/v1/presentations/{deck_id}", headers=AUTH))


def slides_of(deck: Answer) -> list[JsonObject]:
    found = deck["slides"]
    assert isinstance(found, list)
    return found


def elements_of(page: JsonObject) -> list[JsonObject]:
    found = page["pageElements"]
    assert isinstance(found, list)
    return found


def shape_text(element: JsonObject) -> str:
    shape = element["shape"]
    assert isinstance(shape, dict)
    text = shape.get("text", {"textElements": []})
    return "".join(str(t["textRun"]["content"]) for t in text["textElements"] if "textRun" in t)


def placeholder(slide: JsonObject, kind: str) -> JsonObject:
    [found] = [e for e in elements_of(slide) if e.get("shape", {}).get("placeholder", {}).get("type") == kind]  # type: ignore[union-attr]
    return found


async def slide_with_title_and_body(presentations: httpx.AsyncClient) -> tuple[str, JsonObject]:
    deck_id = str((await new_deck(presentations))["presentationId"])
    replied = await deck_batch(
        presentations,
        deck_id,
        [
            {
                "createSlide": {
                    "objectId": "berth_slide",
                    "insertionIndex": 1,
                    "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
                }
            }
        ],
    )
    assert replied["replies"] == [{"createSlide": {"objectId": "berth_slide"}}]
    return deck_id, slides_of(await deck_get(presentations, deck_id))[1]


async def test_a_slide_made_with_a_layout_lands_at_its_index_with_that_layouts_placeholders(
    presentations: httpx.AsyncClient,
) -> None:
    """DOCUMENTED: `createSlide` takes the caller's object id, an insertion index and a predefined layout, and the
    slide carries the layout's placeholders.
    https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#createsliderequest"""
    deck_id, slide = await slide_with_title_and_body(presentations)

    assert slide["objectId"] == "berth_slide"
    assert len(slides_of(await deck_get(presentations, deck_id))) == 2
    assert {"TITLE", "BODY"} <= {
        e["shape"]["placeholder"]["type"]  # type: ignore[index]
        for e in elements_of(slide)
        if "placeholder" in e.get("shape", {})  # type: ignore[operator]
    }


async def test_text_inserted_into_a_slides_placeholders_reads_back_in_each(presentations: httpx.AsyncClient) -> None:
    """DOCUMENTED: `insertText` on a shape's object id writes into that shape.
    https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#inserttextrequest"""
    deck_id, slide = await slide_with_title_and_body(presentations)
    title, body = placeholder(slide, "TITLE")["objectId"], placeholder(slide, "BODY")["objectId"]

    await deck_batch(
        presentations,
        deck_id,
        [
            {"insertText": {"objectId": title, "insertionIndex": 0, "text": "Throughput"}},
            {"insertText": {"objectId": body, "insertionIndex": 0, "text": "Containers up a fifth"}},
        ],
    )

    after = slides_of(await deck_get(presentations, deck_id))[1]
    assert shape_text(placeholder(after, "TITLE")) == "Throughput\n"
    assert shape_text(placeholder(after, "BODY")) == "Containers up a fifth\n"


async def test_speaker_notes_are_written_through_the_notes_pages_speaker_notes_object_id(
    presentations: httpx.AsyncClient,
) -> None:
    """DOCUMENTED: a slide's notes page names, in `speakerNotesObjectId`, the shape holding its speaker notes, and
    text inserted at that id is the notes.
    https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations.pages#notesproperties"""
    deck_id, slide = await slide_with_title_and_body(presentations)
    notes_page = slide["slideProperties"]["notesPage"]  # type: ignore[index]
    notes_id = notes_page["notesProperties"]["speakerNotesObjectId"]  # type: ignore[index]

    await deck_batch(
        presentations,
        deck_id,
        [{"insertText": {"objectId": notes_id, "insertionIndex": 0, "text": "Mention dredging"}}],
    )

    page = slides_of(await deck_get(presentations, deck_id))[1]["slideProperties"]["notesPage"]  # type: ignore[index]
    [notes] = [e for e in page["pageElements"] if e["objectId"] == notes_id]  # type: ignore[index]
    assert shape_text(notes) == "Mention dredging\n"


BOX: JsonObject = {
    "pageObjectId": "berth_slide",
    "size": {"width": {"magnitude": 4000000, "unit": "EMU"}, "height": {"magnitude": 1500000, "unit": "EMU"}},
    "transform": {"scaleX": 1, "scaleY": 1, "translateX": 900000, "translateY": 2600000, "unit": "EMU"},
}


async def test_a_shape_keeps_its_own_text_and_fill_apart_from_the_body(presentations: httpx.AsyncClient) -> None:
    """DOCUMENTED: `createShape` adds a shape of the given type to the page, `insertText` writes into it and
    `updateShapeProperties` fills it; none of it reaches the body placeholder.
    https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#createshaperequest"""
    deck_id, slide = await slide_with_title_and_body(presentations)
    body = placeholder(slide, "BODY")["objectId"]

    await deck_batch(
        presentations,
        deck_id,
        [
            {"insertText": {"objectId": body, "insertionIndex": 0, "text": "Quarter in brief"}},
            {"createShape": {"objectId": "tonnage_box", "shapeType": "RECTANGLE", "elementProperties": BOX}},
            {"insertText": {"objectId": "tonnage_box", "insertionIndex": 0, "text": "Tonnage chart"}},
            {
                "updateShapeProperties": {
                    "objectId": "tonnage_box",
                    "fields": "shapeBackgroundFill",
                    "shapeProperties": {
                        "shapeBackgroundFill": {"solidFill": {"color": {"rgbColor": {"red": 0.9, "green": 0.9}}}}
                    },
                }
            },
        ],
    )

    after = slides_of(await deck_get(presentations, deck_id))[1]
    [box] = [e for e in elements_of(after) if e["objectId"] == "tonnage_box"]
    assert box["shape"]["shapeType"] == "RECTANGLE"  # type: ignore[index]
    assert shape_text(box) == "Tonnage chart\n"
    assert "solidFill" in box["shape"]["shapeProperties"]["shapeBackgroundFill"]  # type: ignore[index]
    assert shape_text(placeholder(after, "BODY")) == "Quarter in brief\n"


async def test_a_tables_cells_hold_their_own_text_apart_from_the_body(presentations: httpx.AsyncClient) -> None:
    """DOCUMENTED: `createTable` makes a table of the given rows and columns, and `insertText` with a `cellLocation`
    writes into that cell. https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#createtablerequest"""
    deck_id, slide = await slide_with_title_and_body(presentations)
    body = placeholder(slide, "BODY")["objectId"]
    cells = [(0, 0, "Berth"), (0, 1, "Calls"), (1, 0, "North"), (1, 1, "31"), (2, 0, "South"), (2, 1, "18")]

    await deck_batch(
        presentations,
        deck_id,
        [
            {"insertText": {"objectId": body, "insertionIndex": 0, "text": "Calls by berth"}},
            {"createTable": {"objectId": "calls_table", "elementProperties": BOX, "rows": 3, "columns": 2}},
            *[
                {
                    "insertText": {
                        "objectId": "calls_table",
                        "cellLocation": {"rowIndex": r, "columnIndex": c},
                        "insertionIndex": 0,
                        "text": text,
                    }
                }
                for r, c, text in cells
            ],
        ],
    )

    after = slides_of(await deck_get(presentations, deck_id))[1]
    [element] = [e for e in elements_of(after) if e["objectId"] == "calls_table"]
    table = element["table"]
    assert isinstance(table, dict) and (table["rows"], table["columns"]) == (3, 2)
    for r, c, text in cells:
        cell = table["tableRows"][r]["tableCells"][c]
        assert "".join(t["textRun"]["content"] for t in cell["text"]["textElements"] if "textRun" in t) == f"{text}\n"
    assert shape_text(placeholder(after, "BODY")) == "Calls by berth\n"
