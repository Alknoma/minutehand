"""Excel workbooks as real bytes: a seeded spreadsheet is an `.xlsx` an Excel reader opens, its cells read back as
text. One worksheet, every cell an inline string (`t="inlineStr"`); formulas, styles and number formats are not
written or read."""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree
from xml.sax.saxutils import escape

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_STAMP = (2026, 1, 1, 0, 0, 0)
"""Every member of the archive carries this date, so the same cells are the same bytes in every run."""

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<workbook xmlns="{_S}" xmlns:r="{_R}"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def _column(n: int) -> str:
    """A column's letters: 0 is A, 25 is Z, 26 is AA."""
    letters = ""
    n += 1
    while n:
        n, rest = divmod(n - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def build(rows: list[list[str]]) -> bytes:
    """A workbook of one worksheet holding `rows`, each cell its text."""
    cells = "".join(
        f'<row r="{r + 1}">'
        + "".join(
            f'<c r="{_column(c)}{r + 1}" t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>'
            for c, v in enumerate(row)
        )
        + "</row>"
        for r, row in enumerate(rows)
    )
    sheet = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_S}"><sheetData>{cells}</sheetData></worksheet>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _RELS),
            ("xl/workbook.xml", _WORKBOOK),
            ("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS),
            ("xl/worksheets/sheet1.xml", sheet),
        ):
            archive.writestr(zipfile.ZipInfo(name, date_time=_STAMP), body)
    return buffer.getvalue()


def text_of(content: bytes) -> str | None:
    """The first worksheet's cells, a row a line and its cells tab-separated; None when the bytes are not a workbook
    this module wrote the way it reads (inline strings)."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return None
    rows = [
        "\t".join("".join(t.text or "" for t in c.iter(f"{{{_S}}}t")) for c in row.iter(f"{{{_S}}}c"))
        for row in root.iter(f"{{{_S}}}row")
    ]
    return "\n".join(rows)
