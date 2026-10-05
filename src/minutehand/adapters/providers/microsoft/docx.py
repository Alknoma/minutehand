"""Word documents as real bytes: a seeded document is a `.docx` a Word reader opens, and the text of an uploaded
one is read back for search. Only paragraphs (`w:p`) and their runs' text (`w:t`) are written or read; styles,
tables and images are not."""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree
from xml.sax.saxutils import escape

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_STAMP = (2026, 1, 1, 0, 0, 0)
"""Every member of the archive carries this date, so the same text is the same bytes in every run."""

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def build(text: str) -> bytes:
    """A Word document with one paragraph per line of `text`."""
    paragraphs = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in text.split("\n")
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{paragraphs}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _RELS),
            ("word/document.xml", document),
        ):
            archive.writestr(zipfile.ZipInfo(name, date_time=_STAMP), body)
    return buffer.getvalue()


def text_of(content: bytes) -> str | None:
    """The paragraphs of a Word document, one per line; None when the bytes are not one."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return None
    lines = ["".join(t.text or "" for t in p.iter(f"{{{_W}}}t")) for p in root.iter(f"{{{_W}}}p")]
    return "\n".join(lines)
