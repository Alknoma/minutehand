"""A table ends one index past its last cell, as Google's index offsets put it.

https://developers.google.com/workspace/docs/api/how-tos/tables: a 2x2 table of empty cells starting at S has its
last row end at S + 11 and the table end at S + 12. So a range ending at the last cell's end stays in that cell,
and the paragraph after the table starts at the table's end.
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.providers.google_workspace.drive_world import AUTH


async def _document(docs: httpx.AsyncClient, document_id: str) -> Any:
    answer = await docs.get(f"/v1/documents/{document_id}", headers=AUTH)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def _batch(docs: httpx.AsyncClient, document_id: str, requests: list[dict[str, Any]]) -> httpx.Response:
    return await docs.post(f"/v1/documents/{document_id}:batchUpdate", headers=AUTH, json={"requests": requests})


async def _with_table(docs: httpx.AsyncClient) -> str:
    created = await docs.post("/v1/documents", headers=AUTH, json={"title": "Totals"})
    assert created.status_code == 200, created.text
    document_id = created.json()["documentId"]
    answer = await _batch(docs, document_id, [{"insertTable": {"rows": 2, "columns": 2, "location": {"index": 1}}}])
    assert answer.status_code == 200, answer.text
    return document_id


def _only_table(document: Any) -> Any:
    tables = [element for element in document["body"]["content"] if "table" in element]
    assert len(tables) == 1
    return tables[0]


async def test_an_empty_two_by_two_table_takes_the_index_offsets_google_documents(docs: httpx.AsyncClient) -> None:
    table = _only_table(await _document(docs, await _with_table(docs)))
    start = table["startIndex"]
    rows = table["table"]["tableRows"]
    first = rows[0]["tableCells"][0]

    assert (rows[0]["startIndex"], rows[0]["endIndex"]) == (start + 1, start + 6)
    assert (first["startIndex"], first["endIndex"]) == (start + 2, start + 4)
    assert (first["content"][0]["startIndex"], first["content"][0]["endIndex"]) == (start + 3, start + 4)
    assert (rows[1]["endIndex"], table["endIndex"]) == (start + 11, start + 12)


async def test_text_inserted_at_a_tables_end_starts_the_paragraph_after_it(docs: httpx.AsyncClient) -> None:
    document_id = await _with_table(docs)
    end = _only_table(await _document(docs, document_id))["endIndex"]

    answer = await _batch(docs, document_id, [{"insertText": {"location": {"index": end}, "text": "After"}}])
    assert answer.status_code == 200, answer.text

    after = next(e for e in (await _document(docs, document_id))["body"]["content"] if e.get("startIndex") == end)
    assert after["paragraph"]["elements"][0]["textRun"]["content"] == "After\n"


async def test_a_paragraph_style_range_ending_at_the_last_cells_end_styles_that_cell(
    docs: httpx.AsyncClient,
) -> None:
    document_id = await _with_table(docs)
    last_cell = _only_table(await _document(docs, document_id))["table"]["tableRows"][1]["tableCells"][1]
    answer = await _batch(
        docs, document_id, [{"insertText": {"location": {"index": last_cell["startIndex"] + 1}, "text": "Total"}}]
    )
    assert answer.status_code == 200, answer.text

    table = _only_table(await _document(docs, document_id))
    last_cell = table["table"]["tableRows"][1]["tableCells"][1]
    assert last_cell["endIndex"] == table["endIndex"] - 1
    paragraph = last_cell["content"][0]

    answer = await _batch(
        docs,
        document_id,
        [
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": paragraph["startIndex"], "endIndex": last_cell["endIndex"]},
                    "paragraphStyle": {"alignment": "CENTER"},
                    "fields": "alignment",
                }
            }
        ],
    )
    assert answer.status_code == 200, answer.text

    styled = _only_table(await _document(docs, document_id))["table"]["tableRows"][1]["tableCells"][1]["content"][0]
    assert styled["paragraph"]["paragraphStyle"]["alignment"] == "CENTER"
