"""A range ending at a table's last cell's end belongs to that cell.

The last cell's end index is also the table's end index — a table carries no end marker — so the range end
alone cannot say whether it means the cell or the body after the table. Google reads it from the start: a range
that starts in the cell ends at the cell's end.
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


def _only_table(document: Any) -> Any:
    tables = [element for element in document["body"]["content"] if "table" in element]
    assert len(tables) == 1
    return tables[0]


async def test_a_paragraph_style_range_ending_at_the_last_cells_end_styles_that_cell(
    docs: httpx.AsyncClient,
) -> None:
    created = await docs.post("/v1/documents", headers=AUTH, json={"title": "Totals"})
    assert created.status_code == 200, created.text
    document_id = created.json()["documentId"]

    answer = await _batch(docs, document_id, [{"insertTable": {"rows": 1, "columns": 2, "location": {"index": 1}}}])
    assert answer.status_code == 200, answer.text
    last_cell = _only_table(await _document(docs, document_id))["table"]["tableRows"][0]["tableCells"][1]
    answer = await _batch(
        docs, document_id, [{"insertText": {"location": {"index": last_cell["startIndex"] + 1}, "text": "Total"}}]
    )
    assert answer.status_code == 200, answer.text

    table = _only_table(await _document(docs, document_id))
    last_cell = table["table"]["tableRows"][0]["tableCells"][1]
    assert last_cell["endIndex"] == table["endIndex"]
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

    styled = _only_table(await _document(docs, document_id))["table"]["tableRows"][0]["tableCells"][1]["content"][0]
    assert styled["paragraph"]["paragraphStyle"]["alignment"] == "CENTER"
