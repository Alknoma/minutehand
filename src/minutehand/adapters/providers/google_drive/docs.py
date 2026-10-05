"""Docs v1: a document's structure as the store keeps it, the `batchUpdate` requests that change it, and the
`Document` it is served as. Docs' own JSON is parsed and built here, as Drive's is in `wire.py`.

**Indexes** count UTF-16 code units from 1, where the body starts after the section break at 0. Every
paragraph ends in a newline that holds its paragraph style and its bullet. A table takes one index for
itself, one for each row and one for each cell, then the cell's paragraphs; nothing marks the end of a cell,
a row or a table. So a table inserted at index `i` starts at `i + 1` (a newline is inserted before it), and
the first paragraph of the cell at row `r`, column `c` starts at `i + 4 + r * (2 * columns + 1) + 2 * c`
while the cells are empty. A page break and an inline image take one index each.

**Editing** works on a flat form of the body: one `Unit` per code point, newline, page break, image or table,
with a table's cells flat in turn. Every edit is an insertion or a deletion at a body index, which is also
how named ranges are kept in step. A batch is applied to a copy and kept only when every request in it
succeeded, as Docs does; a refusal names the request by its position, as Docs' messages do.

**Not built**, answered with Docs' 501 `UNIMPLEMENTED` in the batch rather than a made-up success: headers,
footers and footnotes (any `segmentId` other than the body), tabs other than the first, suggestions, and
the request kinds no caller of this fake sends (`mergeTableCells`, `updateDocumentStyle`, `insertSectionBreak`,
and the rest of Docs' list). `targetRevisionId` is accepted for any revision the document has had and the
batch is applied to the latest: there are no collaborators' concurrent edits to merge.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from enum import StrEnum
from typing import Annotated, Literal, NamedTuple

from pydantic import Field, ValidationError

from minutehand.domain.errors import Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

JSON = "application/json; charset=UTF-8"
"""What Google's APIs answer JSON as, refusals included."""

# --------------------------------------------------------------------------- styles, as Docs spells them


class RgbColor(Model):
    red: float | None = None
    green: float | None = None
    blue: float | None = None


class Color(Model):
    rgbColor: RgbColor | None = None


class OptionalColor(Model):
    color: Color | None = None


class Dimension(Model):
    magnitude: float | None = None
    unit: Literal["PT", "UNIT_UNSPECIFIED"] | None = None


class WeightedFontFamily(Model):
    fontFamily: str | None = None
    weight: int | None = None


class BookmarkLink(Model):
    id: str
    tabId: str | None = None


class HeadingLink(Model):
    id: str
    tabId: str | None = None


class Link(Model):
    url: str | None = None
    bookmarkId: str | None = None
    headingId: str | None = None
    tabId: str | None = None
    bookmark: BookmarkLink | None = None
    heading: HeadingLink | None = None


class TextStyle(Model):
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikethrough: bool | None = None
    smallCaps: bool | None = None
    backgroundColor: OptionalColor | None = None
    foregroundColor: OptionalColor | None = None
    fontSize: Dimension | None = None
    weightedFontFamily: WeightedFontFamily | None = None
    baselineOffset: Literal["NONE", "SUPERSCRIPT", "SUBSCRIPT", "BASELINE_OFFSET_UNSPECIFIED"] | None = None
    link: Link | None = None


class NamedStyle(StrEnum):
    NORMAL_TEXT = "NORMAL_TEXT"
    TITLE = "TITLE"
    SUBTITLE = "SUBTITLE"
    HEADING_1 = "HEADING_1"
    HEADING_2 = "HEADING_2"
    HEADING_3 = "HEADING_3"
    HEADING_4 = "HEADING_4"
    HEADING_5 = "HEADING_5"
    HEADING_6 = "HEADING_6"


HEADINGS = (
    NamedStyle.HEADING_1,
    NamedStyle.HEADING_2,
    NamedStyle.HEADING_3,
    NamedStyle.HEADING_4,
    NamedStyle.HEADING_5,
    NamedStyle.HEADING_6,
)


class ParagraphBorder(Model):
    color: OptionalColor | None = None
    width: Dimension | None = None
    padding: Dimension | None = None
    dashStyle: Literal["SOLID", "DOT", "DASH", "DASH_STYLE_UNSPECIFIED"] | None = None


class Shading(Model):
    backgroundColor: OptionalColor | None = None


class ParagraphStyle(Model):
    headingId: str | None = None
    namedStyleType: NamedStyle | None = None
    alignment: Literal["START", "CENTER", "END", "JUSTIFIED", "ALIGNMENT_UNSPECIFIED"] | None = None
    lineSpacing: float | None = None
    direction: Literal["LEFT_TO_RIGHT", "RIGHT_TO_LEFT", "CONTENT_DIRECTION_UNSPECIFIED"] | None = None
    spacingMode: Literal["NEVER_COLLAPSE", "COLLAPSE_LISTS", "SPACING_MODE_UNSPECIFIED"] | None = None
    spaceAbove: Dimension | None = None
    spaceBelow: Dimension | None = None
    borderBetween: ParagraphBorder | None = None
    borderTop: ParagraphBorder | None = None
    borderBottom: ParagraphBorder | None = None
    borderLeft: ParagraphBorder | None = None
    borderRight: ParagraphBorder | None = None
    indentFirstLine: Dimension | None = None
    indentStart: Dimension | None = None
    indentEnd: Dimension | None = None
    keepLinesTogether: bool | None = None
    keepWithNext: bool | None = None
    avoidWidowAndOrphan: bool | None = None
    shading: Shading | None = None
    pageBreakBefore: bool | None = None


class Bullet(Model):
    listId: str
    nestingLevel: int | None = Field(default=None, description="Absent at level 0, as Docs serves it")
    textStyle: TextStyle = TextStyle()


class NestingLevel(Model):
    bulletAlignment: Literal["START"] = "START"
    glyphFormat: str
    glyphSymbol: str | None = None
    glyphType: str | None = None
    indentFirstLine: Dimension
    indentStart: Dimension
    startNumber: int | None = None
    textStyle: TextStyle = TextStyle()


class ListProperties(Model):
    nestingLevels: list[NestingLevel]


class DocList(Model):
    listProperties: ListProperties


# --------------------------------------------------------------------------- what the store keeps


class TextPiece(Model):
    kind: Literal["text"] = "text"
    text: str
    style: TextStyle = TextStyle()


class PageBreakPiece(Model):
    kind: Literal["page_break"] = "page_break"
    style: TextStyle = TextStyle()


class ImagePiece(Model):
    kind: Literal["image"] = "image"
    object_id: str
    style: TextStyle = TextStyle()


Piece = Annotated[TextPiece | PageBreakPiece | ImagePiece, Field(discriminator="kind")]


class Para(Model):
    kind: Literal["paragraph"] = "paragraph"
    pieces: list[Piece] = []
    style: ParagraphStyle = ParagraphStyle()
    bullet: Bullet | None = None
    newline: TextStyle = Field(default=TextStyle(), description="The style of the newline that ends it")


class Cell(Model):
    content: list[Para]


class Row(Model):
    cells: list[Cell]


class TableBlock(Model):
    kind: Literal["table"] = "table"
    rows: list[Row]


Block = Annotated[Para | TableBlock, Field(discriminator="kind")]


class Size(Model):
    height: Dimension | None = None
    width: Dimension | None = None


class InlineImage(Model):
    uri: str
    size: Size | None = None


class Range(Model):
    startIndex: int | None = None
    endIndex: int | None = None
    segmentId: str | None = None
    tabId: str | None = None


class KeptRange(Model):
    id: str
    name: str
    start: int
    end: int


class List(Model):
    preset: str
    properties: DocList


class DocBody(Model):
    """A Google Doc as the store keeps it, between the file's metadata and Drive's other content kinds."""

    kind: Literal["doc"] = "doc"
    blocks: list[Block]
    lists: dict[str, List] = {}
    images: dict[str, InlineImage] = {}
    named_ranges: list[KeptRange] = []
    minted: int = Field(default=0, description="How many ids this document has minted: lists, images, ranges")
    revisions: list[str] = Field(default=[], description="Every revision id it has had, oldest first")


def empty() -> DocBody:
    return DocBody(blocks=[Para()])


# --------------------------------------------------------------------------- the flat form


class UnitKind(StrEnum):
    CHAR = "char"
    NEWLINE = "newline"
    PAGE_BREAK = "page_break"
    IMAGE = "image"
    TABLE = "table"


Segment = list["Unit"]
FlatTable = list[list[Segment]]


class Unit(NamedTuple):
    """One index-bearing thing in the body, in edit order. Never stored: the store keeps `DocBody`."""

    kind: UnitKind
    style: TextStyle
    char: str = ""
    paragraph: ParagraphStyle | None = None
    bullet: Bullet | None = None
    image: str | None = None
    table: FlatTable | None = None


def width(unit: Unit) -> int:
    if unit.kind is UnitKind.CHAR:
        return utf16(unit.char)
    if unit.kind is UnitKind.TABLE:
        assert unit.table is not None
        return 1 + sum(1 + sum(1 + span(cell) for cell in row) for row in unit.table)
    return 1


def span(segment: Segment) -> int:
    return sum(width(u) for u in segment)


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _flat_para(para: Para) -> Segment:
    units: Segment = []
    for piece in para.pieces:
        if isinstance(piece, TextPiece):
            units += [Unit(UnitKind.CHAR, piece.style, char=c) for c in piece.text]
        elif isinstance(piece, PageBreakPiece):
            units.append(Unit(UnitKind.PAGE_BREAK, piece.style))
        else:
            units.append(Unit(UnitKind.IMAGE, piece.style, image=piece.object_id))
    units.append(Unit(UnitKind.NEWLINE, para.newline, paragraph=para.style, bullet=para.bullet))
    return units


def flatten(blocks: list[Block]) -> Segment:
    units: Segment = []
    for block in blocks:
        if isinstance(block, Para):
            units += _flat_para(block)
        else:
            table = [[[u for p in cell.content for u in _flat_para(p)] for cell in row.cells] for row in block.rows]
            units.append(Unit(UnitKind.TABLE, TextStyle(), table=table))
    return units


def _paras(segment: Segment) -> list[Para]:
    return [b for b in rebuild(segment) if isinstance(b, Para)]


def rebuild(segment: Segment) -> list[Block]:
    blocks: list[Block] = []
    pieces: list[Piece] = []
    run: list[str] = []
    run_style: TextStyle | None = None

    def close_run() -> None:
        nonlocal run, run_style
        if run and run_style is not None:
            pieces.append(TextPiece(text="".join(run), style=run_style))
        run, run_style = [], None

    for unit in segment:
        if unit.kind is UnitKind.CHAR:
            if run_style is not None and unit.style != run_style:
                close_run()
            run.append(unit.char)
            run_style = unit.style
            continue
        close_run()
        if unit.kind is UnitKind.PAGE_BREAK:
            pieces.append(PageBreakPiece(style=unit.style))
        elif unit.kind is UnitKind.IMAGE:
            assert unit.image is not None
            pieces.append(ImagePiece(object_id=unit.image, style=unit.style))
        elif unit.kind is UnitKind.NEWLINE:
            blocks.append(
                Para(pieces=pieces, style=unit.paragraph or ParagraphStyle(), bullet=unit.bullet, newline=unit.style)
            )
            pieces = []
        else:
            assert unit.table is not None
            rows = [Row(cells=[Cell(content=_paras(cell)) for cell in row]) for row in unit.table]
            blocks.append(TableBlock(rows=rows))
    close_run()
    return blocks


# --------------------------------------------------------------------------- requests


class Location(Model):
    index: int
    segmentId: str | None = None
    tabId: str | None = None


class EndOfSegmentLocation(Model):
    segmentId: str | None = None
    tabId: str | None = None


class InsertText(Model):
    text: str = ""
    location: Location | None = None
    endOfSegmentLocation: EndOfSegmentLocation | None = None


class DeleteContentRange(Model):
    range: Range


class UpdateParagraphStyle(Model):
    range: Range
    paragraphStyle: ParagraphStyle
    fields: str


class UpdateTextStyle(Model):
    range: Range
    textStyle: TextStyle
    fields: str


class CreateParagraphBullets(Model):
    range: Range
    bulletPreset: str


class DeleteParagraphBullets(Model):
    range: Range


class InsertTable(Model):
    rows: int
    columns: int
    location: Location | None = None
    endOfSegmentLocation: EndOfSegmentLocation | None = None


class InsertPageBreak(Model):
    location: Location | None = None
    endOfSegmentLocation: EndOfSegmentLocation | None = None


class InsertInlineImage(Model):
    uri: str
    objectSize: Size | None = None
    location: Location | None = None
    endOfSegmentLocation: EndOfSegmentLocation | None = None


class SubstringMatchCriteria(Model):
    text: str
    matchCase: bool = False
    searchByRegex: bool | None = None


class TabsCriteria(Model):
    tabIds: list[str] = []


class ReplaceAllText(Model):
    containsText: SubstringMatchCriteria
    replaceText: str = ""
    tabsCriteria: TabsCriteria | None = None


class CreateNamedRange(Model):
    name: str
    range: Range


class DeleteNamedRange(Model):
    namedRangeId: str | None = None
    name: str | None = None
    tabsCriteria: TabsCriteria | None = None


class ReplaceNamedRangeContent(Model):
    text: str = ""
    namedRangeId: str | None = None
    namedRangeName: str | None = None
    tabsCriteria: TabsCriteria | None = None


class TableCellLocation(Model):
    tableStartLocation: Location
    rowIndex: int = 0
    columnIndex: int = 0


class InsertTableRow(Model):
    tableCellLocation: TableCellLocation
    insertBelow: bool = False


class InsertTableColumn(Model):
    tableCellLocation: TableCellLocation
    insertRight: bool = False


class DeleteTableRow(Model):
    tableCellLocation: TableCellLocation


class DeleteTableColumn(Model):
    tableCellLocation: TableCellLocation


class Request(Model):
    """One entry of `requests`: exactly one of these is set. Docs' kinds this fake does not build are named
    in `NOT_BUILT` and refused with 501."""

    insertText: InsertText | None = None
    deleteContentRange: DeleteContentRange | None = None
    updateParagraphStyle: UpdateParagraphStyle | None = None
    updateTextStyle: UpdateTextStyle | None = None
    createParagraphBullets: CreateParagraphBullets | None = None
    deleteParagraphBullets: DeleteParagraphBullets | None = None
    insertTable: InsertTable | None = None
    insertPageBreak: InsertPageBreak | None = None
    insertInlineImage: InsertInlineImage | None = None
    replaceAllText: ReplaceAllText | None = None
    createNamedRange: CreateNamedRange | None = None
    deleteNamedRange: DeleteNamedRange | None = None
    replaceNamedRangeContent: ReplaceNamedRangeContent | None = None
    insertTableRow: InsertTableRow | None = None
    insertTableColumn: InsertTableColumn | None = None
    deleteTableRow: DeleteTableRow | None = None
    deleteTableColumn: DeleteTableColumn | None = None


NOT_BUILT = frozenset(
    {
        "updateDocumentStyle",
        "updateSectionStyle",
        "insertSectionBreak",
        "mergeTableCells",
        "unmergeTableCells",
        "updateTableCellStyle",
        "updateTableColumnProperties",
        "updateTableRowStyle",
        "pinTableHeaderRows",
        "createHeader",
        "createFooter",
        "deleteHeader",
        "deleteFooter",
        "createFootnote",
        "deletePositionedObject",
        "replaceImage",
        "addDocumentTab",
        "deleteTab",
        "updateDocumentTabProperties",
        "insertPerson",
        "insertDate",
    }
)
"""Request kinds real Docs takes and this fake refuses with 501."""


class WriteControl(Model):
    requiredRevisionId: str | None = None
    targetRevisionId: str | None = None


class BatchUpdate(Model):
    requests: list[Request] = []
    writeControl: WriteControl | None = None


class CreateDocument(Model):
    """`documents.create`: only the title is read; Docs ignores a body sent here."""

    title: str = ""


class InsertInlineImageReply(Model):
    objectId: str


class ReplaceAllTextReply(Model):
    occurrencesChanged: int = 0


class CreateNamedRangeReply(Model):
    namedRangeId: str


class Reply(Model):
    insertInlineImage: InsertInlineImageReply | None = None
    replaceAllText: ReplaceAllTextReply | None = None
    createNamedRange: CreateNamedRangeReply | None = None


class WriteControlAnswer(Model):
    requiredRevisionId: str


class BatchUpdateAnswer(Model):
    documentId: str
    replies: list[Reply]
    writeControl: WriteControlAnswer


class StatusError(Model):
    code: int
    message: str
    status: str


class StatusErrorAnswer(Model):
    """The error envelope Docs and Slides answer in: `code`, `message` and the status word, no `errors` list."""

    error: StatusError


class Refused(ServiceRefusal):
    """A request Docs or Slides refuses: the HTTP status, the status word (the refusal's `code`), and its message."""

    def __init__(self, status: int, word: str, message: str) -> None:
        super().__init__(code=word, message=message, status=status)

    def render(self) -> Rendered:
        answer = StatusErrorAnswer(error=StatusError(code=self.status, message=self.message, status=self.code))
        return Rendered(status=self.status, content_type=JSON, body=answer.model_dump_json().encode())


def _invalid(message: str) -> Refused:
    return Refused(400, "INVALID_ARGUMENT", message)


# --------------------------------------------------------------------------- editing


class Spot(NamedTuple):
    """Where a body index falls: the segment it is in, the unit position in that segment, and that segment's
    first index."""

    segment: Segment
    position: int
    base: int
    in_table: bool


PRESETS: dict[str, tuple[str, ...]] = {
    "BULLET_DISC_CIRCLE_SQUARE": ("●", "○", "■"),
    "BULLET_DIAMONDX_ARROW3D_SQUARE": ("❖", "➢", "■"),
    "BULLET_CHECKBOX": ("☐",),
    "BULLET_ARROW_DIAMOND_DISC": ("➔", "◆", "●"),
    "BULLET_STAR_CIRCLE_SQUARE": ("★", "○", "■"),
    "BULLET_ARROW3D_CIRCLE_SQUARE": ("➢", "○", "■"),
    "BULLET_LEFTTRIANGLE_DIAMOND_DISC": ("◄", "◆", "●"),
    "BULLET_DIAMOND_CIRCLE_SQUARE": ("◆", "○", "■"),
    "NUMBERED_DECIMAL_ALPHA_ROMAN": ("DECIMAL", "ALPHA", "ROMAN"),
    "NUMBERED_DECIMAL_ALPHA_ROMAN_PARENS": ("DECIMAL", "ALPHA", "ROMAN"),
    "NUMBERED_DECIMAL_NESTED": ("DECIMAL",),
    "NUMBERED_UPPERALPHA_ALPHA_ROMAN": ("UPPER_ALPHA", "ALPHA", "ROMAN"),
    "NUMBERED_UPPERROMAN_UPPERALPHA_DECIMAL": ("UPPER_ROMAN", "UPPER_ALPHA", "DECIMAL"),
    "NUMBERED_ZERODECIMAL_ALPHA_ROMAN": ("ZERO_DECIMAL", "ALPHA", "ROMAN"),
}
"""Docs' bullet presets: the glyph of each nesting level, cycling over nine levels."""


def _list_for(preset: str) -> DocList:
    glyphs = PRESETS[preset]
    numbered = preset.startswith("NUMBERED")
    levels: list[NestingLevel] = []
    for level in range(9):
        glyph = glyphs[level % len(glyphs)]
        indent = Dimension(magnitude=36.0 * (level + 1), unit="PT")
        first = Dimension(magnitude=36.0 * level + 18.0, unit="PT")
        if numbered:
            closing = ")" if preset.endswith("PARENS") else "."
            levels.append(
                NestingLevel(
                    glyphFormat=f"%{level}{closing}",
                    glyphType=glyph,
                    indentFirstLine=first,
                    indentStart=indent,
                    startNumber=1,
                )
            )
        else:
            levels.append(
                NestingLevel(glyphFormat=f"%{level}", glyphSymbol=glyph, indentFirstLine=first, indentStart=indent)
            )
    return DocList(listProperties=ListProperties(nestingLevels=levels))


def _field_names(fields: str, model: type[Model], where: str) -> list[str] | None:
    """A request's `fields`: None for `*`, else the top-level names, each one the style has."""
    names = [f.strip() for f in fields.split(",") if f.strip()]
    if not names:
        raise _invalid(f"{where}: At least one field must be listed in 'fields'. (Use '*' to indicate all fields.)")
    if names == ["*"]:
        return None
    for name in names:
        if name.split(".", 1)[0] not in model.model_fields:
            raise _invalid(f"{where}: Invalid field: {name}")
    return [name.split(".", 1)[0] for name in names]


def _merged[S: (TextStyle, ParagraphStyle)](current: S, asked: S, names: list[str] | None) -> S:
    """`current` with the listed fields taken from `asked`; a listed field `asked` leaves unset is cleared."""
    chosen = list(type(current).model_fields) if names is None else names
    update = {name: getattr(asked, name) for name in chosen if name != "headingId"}
    return current.model_copy(update=update)


class Editor:
    """One `batchUpdate`, applied to a flat copy of the document."""

    def __init__(self, document_id: str, doc: DocBody) -> None:
        self._document_id = document_id
        self.body = flatten(doc.blocks)
        self.lists = dict(doc.lists)
        self.images = dict(doc.images)
        self.ranges = [r.model_copy() for r in doc.named_ranges]
        self.minted = doc.minted
        self.revisions = list(doc.revisions)
        self._where = ""

    def result(self) -> DocBody:
        return DocBody(
            blocks=rebuild(self.body),
            lists=self.lists,
            images=self.images,
            named_ranges=self.ranges,
            minted=self.minted,
            revisions=self.revisions,
        )

    def _mint(self, prefix: str) -> str:
        self.minted += 1
        digest = hashlib.sha256(f"{self._document_id}\x1f{prefix}\x1f{self.minted}".encode()).hexdigest()
        return f"{prefix}{digest[:12]}"

    # ---------------------------------------------------------------- finding an index

    def _segment_end(self) -> int:
        return 1 + span(self.body)

    def _find(self, index: int, *, for_range_end: bool = False) -> Spot:
        if index < 1:
            raise _invalid(f"{self._where}: Index {index} must be greater than or equal to 1.")
        return self._find_in(self.body, 1, index, for_range_end=for_range_end, in_table=False)

    def _find_in(self, segment: Segment, base: int, index: int, *, for_range_end: bool, in_table: bool) -> Spot:
        at = base
        for position, unit in enumerate(segment):
            if at == index:
                return Spot(segment, position, base, in_table)
            size = width(unit)
            if at < index < at + size:
                if unit.kind is UnitKind.CHAR:
                    raise _invalid(f"{self._where}: The index {index} falls inside a surrogate pair.")
                assert unit.table is not None
                inner = at + 1
                for row in unit.table:
                    if index == inner:
                        raise _invalid(f"{self._where}: Index {index} is the start of a table row.")
                    inner += 1
                    for cell in row:
                        if index == inner:
                            raise _invalid(f"{self._where}: Index {index} is the start of a table cell.")
                        inner += 1
                        cell_span = span(cell)
                        if index < inner + cell_span or (for_range_end and index == inner + cell_span):
                            return self._find_in(cell, inner, index, for_range_end=for_range_end, in_table=True)
                        inner += cell_span
            at += size
        if at == index and for_range_end:
            return Spot(segment, len(segment), base, in_table)
        end = base + span(segment)
        raise _invalid(
            f"{self._where}: Index {index} must be less than the end index of the referenced segment, {end}."
        )

    def _check_segment(self, segment_id: str | None, tab_id: str | None) -> None:
        if segment_id:
            raise NotImplementedError(f"{self._where}: this simulation has no headers, footers or footnotes")
        if tab_id and tab_id != FIRST_TAB:
            raise _invalid(f"{self._where}: The tab with ID {tab_id} was not found.")

    def _insertion(self, location: Location | None, end_of_segment: EndOfSegmentLocation | None) -> int:
        if (location is None) == (end_of_segment is None):
            raise _invalid(f"{self._where}: Exactly one of location or endOfSegmentLocation must be set.")
        if location is not None:
            self._check_segment(location.segmentId, location.tabId)
            return location.index
        assert end_of_segment is not None
        self._check_segment(end_of_segment.segmentId, end_of_segment.tabId)
        return self._segment_end() - 1

    def _paragraph_spot(self, index: int) -> Spot:
        """An index text can go at: inside an existing paragraph, before the segment's last newline's end."""
        spot = self._find(index)
        if spot.position >= len(spot.segment):
            raise _invalid(
                f"{self._where}: Index {index} must be less than the end index of the referenced segment, "
                f"{spot.base + span(spot.segment)}."
            )
        if spot.segment[spot.position].kind is UnitKind.TABLE:
            raise _invalid(
                f"{self._where}: The insertion index must be inside the bounds of an existing paragraph. You can "
                "still create new paragraphs by inserting newlines."
            )
        return spot

    def _range(self, asked: Range) -> tuple[Spot, Spot, int, int]:
        self._check_segment(asked.segmentId, asked.tabId)
        if asked.startIndex is None or asked.endIndex is None:
            raise _invalid(f"{self._where}: The range must have a start index and an end index.")
        start, end = asked.startIndex, asked.endIndex
        if start >= end:
            raise _invalid(f"{self._where}: The range should not be empty.")
        first = self._find(start)
        last = self._find(end, for_range_end=True)
        if first.segment is not last.segment:
            raise _invalid(f"{self._where}: The range cannot span the boundary of a table cell.")
        return first, last, start, end

    # ---------------------------------------------------------------- the two edits every request is made of

    def _insert(self, index: int, spot: Spot, units: Segment) -> None:
        spot.segment[spot.position : spot.position] = units
        size = span(units)
        for i, kept in enumerate(self.ranges):
            start = kept.start + size if kept.start >= index else kept.start
            end = kept.end + size if kept.end > index else kept.end
            self.ranges[i] = kept.model_copy(update={"start": start, "end": end})

    def _delete(self, start: int, end: int, first: Spot, last: Spot) -> None:
        del first.segment[first.position : last.position]
        size = end - start

        def moved(at: int) -> int:
            return at if at <= start else start if at <= end else at - size

        kept: list[KeptRange] = []
        for r in self.ranges:
            survivor = r.model_copy(update={"start": moved(r.start), "end": moved(r.end)})
            if survivor.end > survivor.start:
                kept.append(survivor)
        self.ranges = kept

    # ---------------------------------------------------------------- helpers over the flat form

    @staticmethod
    def _closing(segment: Segment, position: int) -> Unit:
        """The newline that ends the paragraph holding `position`."""
        for unit in segment[position:]:
            if unit.kind is UnitKind.NEWLINE:
                return unit
        raise AssertionError("every segment ends in a newline")

    @staticmethod
    def _inherited(segment: Segment, position: int) -> TextStyle:
        """The style inserted text takes: the character before it in its paragraph, else the one after; a link
        is not carried past its own end."""
        after = segment[position].style if position < len(segment) else TextStyle()
        if position > 0 and segment[position - 1].kind in (UnitKind.CHAR, UnitKind.PAGE_BREAK, UnitKind.IMAGE):
            before = segment[position - 1].style
            if before.link is not None and before.link != after.link:
                return before.model_copy(update={"link": None})
            return before
        return after

    def _text_units(self, text: str, style: TextStyle, closing: Unit) -> Segment:
        units: Segment = []
        for char in text:
            if char == "\n":
                units.append(Unit(UnitKind.NEWLINE, style, paragraph=closing.paragraph, bullet=closing.bullet))
            else:
                units.append(Unit(UnitKind.CHAR, style, char=char))
        return units

    def _paragraph_ends(self, segment: Segment, first: int, last: int) -> list[int]:
        """Positions of the newlines closing every paragraph that overlaps units [first, last) of `segment`."""
        ends: list[int] = []
        for position in range(first, len(segment)):
            if segment[position].kind is UnitKind.NEWLINE:
                ends.append(position)
                if position >= last - 1:
                    break
        return ends

    def _each_paragraph(self, first: Spot, last: Spot, change: Callable[[Unit], Unit]) -> None:
        """Apply `change` to the closing newline of every paragraph the range touches, inside tables too."""
        segment = first.segment
        for position in range(first.position, max(last.position, first.position + 1)):
            unit = segment[position] if position < len(segment) else None
            if unit is not None and unit.kind is UnitKind.TABLE:
                assert unit.table is not None
                for row in unit.table:
                    for cell in row:
                        for at, inner in enumerate(cell):
                            if inner.kind is UnitKind.NEWLINE:
                                cell[at] = change(inner)
        for position in self._paragraph_ends(segment, first.position, max(last.position, first.position + 1)):
            segment[position] = change(segment[position])

    # ---------------------------------------------------------------- requests

    def apply(self, request: Request, position: int) -> Reply:
        set_fields = [name for name in type(request).model_fields if getattr(request, name) is not None]
        if len(set_fields) != 1:
            raise _invalid(f"Invalid requests[{position}]: exactly one kind of request must be set.")
        kind = set_fields[0]
        self._where = f"Invalid requests[{position}].{kind}"
        handler: Callable[[Model], Reply] = getattr(self, f"_{kind}")
        return handler(getattr(request, kind))

    def _insertText(self, asked: InsertText) -> Reply:
        index = self._insertion(asked.location, asked.endOfSegmentLocation)
        spot = self._paragraph_spot(index)
        if not asked.text:
            return Reply()
        closing = self._closing(spot.segment, spot.position)
        units = self._text_units(asked.text, self._inherited(spot.segment, spot.position), closing)
        self._insert(index, spot, units)
        return Reply()

    def _deleteContentRange(self, asked: DeleteContentRange) -> Reply:
        first, last, start, end = self._range(asked.range)
        segment = first.segment
        if last.position >= len(segment):
            raise _invalid(f"{self._where}: The range cannot include the newline character at the end of the segment.")
        removed = segment[first.position : last.position]
        merging = any(u.kind is UnitKind.NEWLINE for u in removed)
        keep_style = self._closing(segment, first.position) if merging else None
        self._delete(start, end, first, last)
        if keep_style is not None:
            for position in range(first.position, len(segment)):
                unit = segment[position]
                if unit.kind is UnitKind.NEWLINE:
                    segment[position] = unit._replace(paragraph=keep_style.paragraph, bullet=keep_style.bullet)
                    break
        return Reply()

    def _updateParagraphStyle(self, asked: UpdateParagraphStyle) -> Reply:
        first, last, _, _ = self._range(asked.range)
        names = _field_names(asked.fields, ParagraphStyle, self._where)

        def restyled(unit: Unit) -> Unit:
            style = _merged(unit.paragraph or ParagraphStyle(), asked.paragraphStyle, names)
            if style.namedStyleType in (*HEADINGS, NamedStyle.TITLE, NamedStyle.SUBTITLE):
                if style.headingId is None:
                    style = style.model_copy(update={"headingId": self._mint("h.")})
            else:
                style = style.model_copy(update={"headingId": None})
            return unit._replace(paragraph=style)

        self._each_paragraph(first, last, restyled)
        return Reply()

    def _updateTextStyle(self, asked: UpdateTextStyle) -> Reply:
        first, last, _, _ = self._range(asked.range)
        names = _field_names(asked.fields, TextStyle, self._where)

        def restyled(unit: Unit) -> Unit:
            return unit._replace(style=_merged(unit.style, asked.textStyle, names))

        segment = first.segment
        for position in range(first.position, last.position):
            unit = segment[position]
            if unit.kind is UnitKind.TABLE:
                assert unit.table is not None
                for row in unit.table:
                    for cell in row:
                        cell[:] = [restyled(u) for u in cell]
            else:
                segment[position] = restyled(unit)
        return Reply()

    def _createParagraphBullets(self, asked: CreateParagraphBullets) -> Reply:
        if asked.bulletPreset not in PRESETS:
            raise _invalid(f"{self._where}: Invalid value at 'bullet_preset': {asked.bulletPreset}")
        first, last, start, _ = self._range(asked.range)
        segment = first.segment
        ends = self._paragraph_ends(segment, first.position, last.position)
        previous = self._previous_paragraph_bullet(segment, first.position)
        list_id = (
            previous.listId if previous is not None and self._preset(previous.listId) == asked.bulletPreset else None
        )
        if list_id is None:
            list_id = self._mint("kix.")
            self.lists[list_id] = List(preset=asked.bulletPreset, properties=_list_for(asked.bulletPreset))
        for end in reversed(ends):
            begin = end
            while begin > 0 and segment[begin - 1].kind not in (UnitKind.NEWLINE, UnitKind.TABLE):
                begin -= 1
            tabs = 0
            while segment[begin + tabs].kind is UnitKind.CHAR and segment[begin + tabs].char == "\t":
                tabs += 1
            bullet = Bullet(listId=list_id, nestingLevel=min(tabs, 8) or None)
            segment[end] = segment[end]._replace(bullet=bullet)
            if tabs:
                index = first.base + span(segment[:begin])
                self._delete(
                    index,
                    index + tabs,
                    Spot(segment, begin, first.base, first.in_table),
                    Spot(segment, begin + tabs, first.base, first.in_table),
                )
        del start
        return Reply()

    def _preset(self, list_id: str) -> str | None:
        return self.lists[list_id].preset if list_id in self.lists else None

    @staticmethod
    def _previous_paragraph_bullet(segment: Segment, position: int) -> Bullet | None:
        begin = position
        while begin > 0 and segment[begin - 1].kind is not UnitKind.NEWLINE:
            if segment[begin - 1].kind is UnitKind.TABLE:
                return None
            begin -= 1
        return segment[begin - 1].bullet if begin > 0 else None

    def _deleteParagraphBullets(self, asked: DeleteParagraphBullets) -> Reply:
        first, last, _, _ = self._range(asked.range)
        self._each_paragraph(first, last, lambda unit: unit._replace(bullet=None))
        return Reply()

    def _insertTable(self, asked: InsertTable) -> Reply:
        if not 1 <= asked.rows <= 20 or not 1 <= asked.columns <= 20:
            raise _invalid(f"{self._where}: A table must have between 1 and 20 rows and columns.")
        index = self._insertion(asked.location, asked.endOfSegmentLocation)
        spot = self._paragraph_spot(index)
        if spot.in_table:
            raise NotImplementedError(f"{self._where}: this simulation does not nest a table in a table")
        closing = self._closing(spot.segment, spot.position)
        style = self._inherited(spot.segment, spot.position)
        cell_newline = Unit(
            UnitKind.NEWLINE, TextStyle(), paragraph=ParagraphStyle(namedStyleType=NamedStyle.NORMAL_TEXT)
        )
        table: FlatTable = [[[cell_newline] for _ in range(asked.columns)] for _ in range(asked.rows)]
        newline = Unit(UnitKind.NEWLINE, style, paragraph=closing.paragraph, bullet=closing.bullet)
        self._insert(index, spot, [newline, Unit(UnitKind.TABLE, TextStyle(), table=table)])
        return Reply()

    def _insertPageBreak(self, asked: InsertPageBreak) -> Reply:
        index = self._insertion(asked.location, asked.endOfSegmentLocation)
        spot = self._paragraph_spot(index)
        if spot.in_table:
            raise _invalid(f"{self._where}: Page breaks cannot be inserted inside a table.")
        closing = self._closing(spot.segment, spot.position)
        style = self._inherited(spot.segment, spot.position)
        newline = Unit(UnitKind.NEWLINE, style, paragraph=closing.paragraph, bullet=closing.bullet)
        self._insert(index, spot, [Unit(UnitKind.PAGE_BREAK, style), newline])
        return Reply()

    def _insertInlineImage(self, asked: InsertInlineImage) -> Reply:
        if not asked.uri.startswith(("https://", "http://")) or len(asked.uri) > 2000:
            raise _invalid(f"{self._where}: The URL must be public and shorter than 2 kB.")
        index = self._insertion(asked.location, asked.endOfSegmentLocation)
        spot = self._paragraph_spot(index)
        object_id = self._mint("kix.")
        self.images[object_id] = InlineImage(uri=asked.uri, size=asked.objectSize)
        self._insert(index, spot, [Unit(UnitKind.IMAGE, self._inherited(spot.segment, spot.position), image=object_id)])
        return Reply(insertInlineImage=InsertInlineImageReply(objectId=object_id))

    def _replaceAllText(self, asked: ReplaceAllText) -> Reply:
        needle = asked.containsText.text
        if not needle:
            raise _invalid(f"{self._where}: The search text must not be empty.")
        if asked.containsText.searchByRegex:
            raise NotImplementedError(f"{self._where}: this simulation does not search by regular expression")
        changed = 0
        for segment, base in list(self._segments()):
            changed += self._replace_in(segment, base, needle, asked.replaceText, asked.containsText.matchCase)
        return Reply(replaceAllText=ReplaceAllTextReply(occurrencesChanged=changed))

    def _segments(self) -> Iterator[tuple[Segment, int]]:
        """The body and every cell, each with its first index, deepest last."""

        def walk(segment: Segment, base: int) -> Iterator[tuple[Segment, int]]:
            yield segment, base
            at = base
            for unit in segment:
                if unit.kind is UnitKind.TABLE:
                    assert unit.table is not None
                    inner = at + 1
                    for row in unit.table:
                        inner += 1
                        for cell in row:
                            inner += 1
                            yield from walk(cell, inner)
                            inner += span(cell)
                at += width(unit)

        yield from walk(self.body, 1)

    def _replace_in(self, segment: Segment, base: int, needle: str, replacement: str, match_case: bool) -> int:
        text = "".join(u.char if u.kind is UnitKind.CHAR else "\x00" for u in segment)
        haystack, target = (text, needle) if match_case else (text.lower(), needle.lower())
        found = [m.start() for m in re.finditer(re.escape(target), haystack)]
        for position in reversed(found):
            index = base + span(segment[:position])
            end = index + utf16(needle)
            style = segment[position].style
            closing = self._closing(segment, position)
            first = Spot(segment, position, base, False)
            last = Spot(segment, position + len(needle), base, False)
            self._delete(index, end, first, last)
            self._insert(index, first, self._text_units(replacement, style, closing))
        return len(found)

    def _createNamedRange(self, asked: CreateNamedRange) -> Reply:
        if not 1 <= utf16(asked.name) <= 256:
            raise _invalid(f"{self._where}: The name must be between 1 and 256 UTF-16 code units long.")
        _, _, start, end = self._range(asked.range)
        range_id = self._mint("kix.")
        self.ranges.append(KeptRange(id=range_id, name=asked.name, start=start, end=end))
        return Reply(createNamedRange=CreateNamedRangeReply(namedRangeId=range_id))

    def _deleteNamedRange(self, asked: DeleteNamedRange) -> Reply:
        if (asked.namedRangeId is None) == (asked.name is None):
            raise _invalid(f"{self._where}: Exactly one of namedRangeId or name must be set.")
        before = len(self.ranges)
        self.ranges = [r for r in self.ranges if r.id != asked.namedRangeId and r.name != asked.name]
        if asked.namedRangeId is not None and len(self.ranges) == before:
            raise _invalid(f"{self._where}: The named range with ID {asked.namedRangeId} was not found.")
        return Reply()

    def _replaceNamedRangeContent(self, asked: ReplaceNamedRangeContent) -> Reply:
        if (asked.namedRangeId is None) == (asked.namedRangeName is None):
            raise _invalid(f"{self._where}: Exactly one of namedRangeId or namedRangeName must be set.")
        chosen = [r for r in self.ranges if r.id == asked.namedRangeId or r.name == asked.namedRangeName]
        if not chosen:
            raise _invalid(f"{self._where}: The named range was not found.")
        for kept in sorted(chosen, key=lambda r: r.start, reverse=True):
            first = self._find(kept.start)
            last = self._find(kept.end, for_range_end=True)
            style = first.segment[first.position].style
            closing = self._closing(first.segment, first.position)
            self._delete(kept.start, kept.end, first, last)
            spot = self._find(kept.start)
            self._insert(kept.start, spot, self._text_units(asked.text, style, closing))
            for i, r in enumerate(self.ranges):
                if r.id == kept.id:
                    self.ranges[i] = r.model_copy(update={"start": kept.start, "end": kept.start + utf16(asked.text)})
        return Reply()

    def _table_at(self, location: TableCellLocation) -> tuple[Unit, int]:
        self._check_segment(location.tableStartLocation.segmentId, location.tableStartLocation.tabId)
        index = location.tableStartLocation.index
        spot = self._find(index)
        if spot.position >= len(spot.segment) or spot.segment[spot.position].kind is not UnitKind.TABLE:
            raise _invalid(f"{self._where}: The provided table start location is invalid.")
        table = spot.segment[spot.position]
        assert table.table is not None
        if not 0 <= location.rowIndex < len(table.table) or not 0 <= location.columnIndex < len(table.table[0]):
            raise _invalid(f"{self._where}: The table cell location is outside the table.")
        return table, index

    def _shift(self, index: int, size: int) -> None:
        """Named ranges after an insertion (positive) or deletion (negative) of `size` at `index`."""
        if size > 0:
            for i, kept in enumerate(self.ranges):
                start = kept.start + size if kept.start >= index else kept.start
                end = kept.end + size if kept.end > index else kept.end
                self.ranges[i] = kept.model_copy(update={"start": start, "end": end})
        else:
            gone = -size

            def moved(at: int) -> int:
                return at if at <= index else index if at <= index + gone else at - gone

            self.ranges = [
                r.model_copy(update={"start": moved(r.start), "end": moved(r.end)})
                for r in self.ranges
                if moved(r.end) > moved(r.start)
            ]

    @staticmethod
    def _row_start(table: FlatTable, start: int, row: int) -> int:
        return start + 1 + sum(1 + sum(1 + span(c) for c in r) for r in table[:row])

    @staticmethod
    def _empty_cell() -> Segment:
        return [Unit(UnitKind.NEWLINE, TextStyle(), paragraph=ParagraphStyle(namedStyleType=NamedStyle.NORMAL_TEXT))]

    def _insertTableRow(self, asked: InsertTableRow) -> Reply:
        unit, start = self._table_at(asked.tableCellLocation)
        assert unit.table is not None
        row = asked.tableCellLocation.rowIndex + (1 if asked.insertBelow else 0)
        index = self._row_start(unit.table, start, row)
        unit.table.insert(row, [self._empty_cell() for _ in unit.table[0]])
        self._shift(index, 1 + 2 * len(unit.table[0]))
        return Reply()

    def _insertTableColumn(self, asked: InsertTableColumn) -> Reply:
        unit, start = self._table_at(asked.tableCellLocation)
        assert unit.table is not None
        column = asked.tableCellLocation.columnIndex + (1 if asked.insertRight else 0)
        for r in reversed(range(len(unit.table))):
            row = unit.table[r]
            index = self._row_start(unit.table, start, r) + 1 + sum(1 + span(c) for c in row[:column])
            row.insert(column, self._empty_cell())
            self._shift(index, 2)
        return Reply()

    def _deleteTableRow(self, asked: DeleteTableRow) -> Reply:
        unit, start = self._table_at(asked.tableCellLocation)
        assert unit.table is not None
        if len(unit.table) == 1:
            raise _invalid(f"{self._where}: A table must keep at least one row; delete the table instead.")
        row = asked.tableCellLocation.rowIndex
        index = self._row_start(unit.table, start, row)
        size = 1 + sum(1 + span(c) for c in unit.table[row])
        del unit.table[row]
        self._shift(index, -size)
        return Reply()

    def _deleteTableColumn(self, asked: DeleteTableColumn) -> Reply:
        unit, start = self._table_at(asked.tableCellLocation)
        assert unit.table is not None
        if len(unit.table[0]) == 1:
            raise _invalid(f"{self._where}: A table must keep at least one column; delete the table instead.")
        column = asked.tableCellLocation.columnIndex
        for r in reversed(range(len(unit.table))):
            row = unit.table[r]
            index = self._row_start(unit.table, start, r) + 1 + sum(1 + span(c) for c in row[:column])
            size = 1 + span(row[column])
            del row[column]
            self._shift(index, -size)
        return Reply()


FIRST_TAB = "t.0"


def revision(document_id: str, version: str) -> str:
    """A document's revision id: the same for the same version of the same file, as opaque as Docs'."""
    return "AL" + hashlib.sha256(f"{document_id}:{version}".encode()).hexdigest()[:40]


def read_batch(raw: bytes) -> BatchUpdate:
    """The request body, refusing a request kind this fake does not build with 501 before anything else."""
    try:
        return BatchUpdate.model_validate_json(raw or b"{}")
    except ValidationError as error:
        for problem in error.errors():
            location = [str(part) for part in problem["loc"]]
            if len(location) >= 3 and location[0] == "requests" and location[2] in NOT_BUILT:
                raise NotImplementedError(f"this simulation does not build the Docs request {location[2]}") from error
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise _invalid(f"Invalid JSON payload received. Unknown name or bad value at '{where}'") from error


def update(document_id: str, doc: DocBody, current_revision: str, asked: BatchUpdate) -> tuple[DocBody, list[Reply]]:
    """Apply a batch: every request or none of them."""
    control = asked.writeControl
    if control is not None and control.requiredRevisionId is not None and control.targetRevisionId is not None:
        raise _invalid("Only one of requiredRevisionId and targetRevisionId may be set.")
    if (
        control is not None
        and control.requiredRevisionId is not None
        and control.requiredRevisionId != current_revision
    ):
        raise Refused(
            400,
            "FAILED_PRECONDITION",
            f"The required revision ID {control.requiredRevisionId} does not match the latest revision of the document.",
        )
    if (
        control is not None
        and control.targetRevisionId is not None
        and control.targetRevisionId not in [*doc.revisions, current_revision]
    ):
        raise _invalid(f"The target revision ID {control.targetRevisionId} is not a revision of this document.")
    editor = Editor(document_id, doc)
    replies = [editor.apply(request, position) for position, request in enumerate(asked.requests)]
    return editor.result(), replies


# --------------------------------------------------------------------------- the Document served


class TextRunOut(Model):
    content: str
    textStyle: TextStyle = TextStyle()


class PageBreakOut(Model):
    textStyle: TextStyle = TextStyle()


class InlineObjectElementOut(Model):
    inlineObjectId: str
    textStyle: TextStyle = TextStyle()


class ParagraphElement(Model):
    startIndex: int
    endIndex: int
    textRun: TextRunOut | None = None
    pageBreak: PageBreakOut | None = None
    inlineObjectElement: InlineObjectElementOut | None = None


class ParagraphOut(Model):
    elements: list[ParagraphElement]
    paragraphStyle: ParagraphStyle
    bullet: Bullet | None = None


class TableCellStyle(Model):
    rowSpan: int = 1
    columnSpan: int = 1
    contentAlignment: Literal["TOP"] = "TOP"


class TableCellOut(Model):
    startIndex: int
    endIndex: int
    content: list[StructuralElement]
    tableCellStyle: TableCellStyle = TableCellStyle()


class TableRowStyle(Model):
    minRowHeight: Dimension = Dimension(unit="PT")


class TableRowOut(Model):
    startIndex: int
    endIndex: int
    tableCells: list[TableCellOut]
    tableRowStyle: TableRowStyle = TableRowStyle()


class TableColumnProperties(Model):
    widthType: Literal["EVENLY_DISTRIBUTED"] = "EVENLY_DISTRIBUTED"


class TableStyle(Model):
    tableColumnProperties: list[TableColumnProperties]


class TableOut(Model):
    rows: int
    columns: int
    tableRows: list[TableRowOut]
    tableStyle: TableStyle


class SectionStyle(Model):
    columnSeparatorStyle: Literal["NONE"] = "NONE"
    contentDirection: Literal["LEFT_TO_RIGHT"] = "LEFT_TO_RIGHT"
    sectionType: Literal["CONTINUOUS"] = "CONTINUOUS"


class SectionBreak(Model):
    sectionStyle: SectionStyle = SectionStyle()


class StructuralElement(Model):
    startIndex: int | None = Field(default=None, description="Absent on the opening section break, as Docs sends it")
    endIndex: int
    paragraph: ParagraphOut | None = None
    table: TableOut | None = None
    sectionBreak: SectionBreak | None = None


class Body(Model):
    content: list[StructuralElement]


class NamedRangeOut(Model):
    namedRangeId: str
    name: str
    ranges: list[Range]


class NamedRanges(Model):
    name: str
    namedRanges: list[NamedRangeOut]


class EmbeddedObjectImage(Model):
    contentUri: str
    sourceUri: str


class EmbeddedObject(Model):
    imageProperties: EmbeddedObjectImage
    size: Size | None = None


class InlineObjectProperties(Model):
    embeddedObject: EmbeddedObject


class InlineObject(Model):
    objectId: str
    inlineObjectProperties: InlineObjectProperties


class DocumentStyle(Model):
    pageSize: Size = Size(height=Dimension(magnitude=792, unit="PT"), width=Dimension(magnitude=612, unit="PT"))
    marginTop: Dimension = Dimension(magnitude=72, unit="PT")
    marginBottom: Dimension = Dimension(magnitude=72, unit="PT")
    marginLeft: Dimension = Dimension(magnitude=72, unit="PT")
    marginRight: Dimension = Dimension(magnitude=72, unit="PT")


class NamedStyleOut(Model):
    namedStyleType: NamedStyle
    textStyle: TextStyle
    paragraphStyle: ParagraphStyle


class NamedStylesOut(Model):
    styles: list[NamedStyleOut]


class TabProperties(Model):
    tabId: str
    title: str
    index: int


class DocumentTab(Model):
    body: Body
    documentStyle: DocumentStyle
    namedStyles: NamedStylesOut
    lists: dict[str, DocList] | None = None
    namedRanges: dict[str, NamedRanges] | None = None
    inlineObjects: dict[str, InlineObject] | None = None


class Tab(Model):
    tabProperties: TabProperties
    documentTab: DocumentTab


class Document(Model):
    documentId: str
    title: str
    revisionId: str
    suggestionsViewMode: Literal["SUGGESTIONS_INLINE"] = "SUGGESTIONS_INLINE"
    body: Body | None = None
    documentStyle: DocumentStyle | None = None
    namedStyles: NamedStylesOut | None = None
    lists: dict[str, DocList] | None = None
    namedRanges: dict[str, NamedRanges] | None = None
    inlineObjects: dict[str, InlineObject] | None = None
    tabs: list[Tab] | None = None


_NAMED_SIZES = {
    NamedStyle.NORMAL_TEXT: 11,
    NamedStyle.TITLE: 26,
    NamedStyle.SUBTITLE: 15,
    NamedStyle.HEADING_1: 20,
    NamedStyle.HEADING_2: 16,
    NamedStyle.HEADING_3: 14,
    NamedStyle.HEADING_4: 12,
    NamedStyle.HEADING_5: 11,
    NamedStyle.HEADING_6: 11,
}


def _named_styles() -> NamedStylesOut:
    return NamedStylesOut(
        styles=[
            NamedStyleOut(
                namedStyleType=name,
                textStyle=TextStyle(fontSize=Dimension(magnitude=size, unit="PT")),
                paragraphStyle=ParagraphStyle(namedStyleType=name, direction="LEFT_TO_RIGHT"),
            )
            for name, size in _NAMED_SIZES.items()
        ]
    )


def _served_style(style: ParagraphStyle) -> ParagraphStyle:
    update: dict[str, object] = {}
    if style.namedStyleType is None:
        update["namedStyleType"] = NamedStyle.NORMAL_TEXT
    if style.direction is None:
        update["direction"] = "LEFT_TO_RIGHT"
    return style.model_copy(update=update) if update else style


def _render_para(para: Para, at: int) -> tuple[StructuralElement, int]:
    start = at
    elements: list[ParagraphElement] = []
    pieces = list(para.pieces)
    for i, piece in enumerate(pieces):
        if isinstance(piece, TextPiece):
            content = piece.text
            last = i == len(pieces) - 1 and piece.style == para.newline
            if last:
                content += "\n"
            end = at + utf16(content)
            elements.append(
                ParagraphElement(
                    startIndex=at, endIndex=end, textRun=TextRunOut(content=content, textStyle=piece.style)
                )
            )
            at = end
            if last:
                break
        elif isinstance(piece, PageBreakPiece):
            elements.append(
                ParagraphElement(startIndex=at, endIndex=at + 1, pageBreak=PageBreakOut(textStyle=piece.style))
            )
            at += 1
        else:
            out = InlineObjectElementOut(inlineObjectId=piece.object_id, textStyle=piece.style)
            elements.append(ParagraphElement(startIndex=at, endIndex=at + 1, inlineObjectElement=out))
            at += 1
    else:
        elements.append(
            ParagraphElement(startIndex=at, endIndex=at + 1, textRun=TextRunOut(content="\n", textStyle=para.newline))
        )
        at += 1
    paragraph = ParagraphOut(elements=elements, paragraphStyle=_served_style(para.style), bullet=para.bullet)
    return StructuralElement(startIndex=start, endIndex=at, paragraph=paragraph), at


def _render_blocks(blocks: list[Block], at: int) -> tuple[list[StructuralElement], int]:
    content: list[StructuralElement] = []
    for block in blocks:
        if isinstance(block, Para):
            element, at = _render_para(block, at)
            content.append(element)
            continue
        start = at
        at += 1
        rows: list[TableRowOut] = []
        for row in block.rows:
            row_start = at
            at += 1
            cells: list[TableCellOut] = []
            for cell in row.cells:
                cell_start = at
                at += 1
                inner, at = _render_blocks(list(cell.content), at)
                cells.append(TableCellOut(startIndex=cell_start, endIndex=at, content=inner))
            rows.append(TableRowOut(startIndex=row_start, endIndex=at, tableCells=cells))
        columns = len(block.rows[0].cells)
        table = TableOut(
            rows=len(block.rows),
            columns=columns,
            tableRows=rows,
            tableStyle=TableStyle(tableColumnProperties=[TableColumnProperties() for _ in range(columns)]),
        )
        content.append(StructuralElement(startIndex=start, endIndex=at, table=table))
    return content, at


def render(document_id: str, title: str, revision_id: str, doc: DocBody, *, tabs: bool) -> Document:
    """The `Document` Docs serves: the body with every index, its lists, named ranges and images. With `tabs`
    (`includeTabsContent=true`) the content is under the one tab and the top-level content fields are absent."""
    content, _ = _render_blocks(list(doc.blocks), 1)
    body = Body(content=[StructuralElement(endIndex=1, sectionBreak=SectionBreak()), *content])
    lists = {list_id: kept.properties for list_id, kept in doc.lists.items()} or None
    grouped: dict[str, list[NamedRangeOut]] = {}
    for kept in doc.named_ranges:
        grouped.setdefault(kept.name, []).append(
            NamedRangeOut(
                namedRangeId=kept.id, name=kept.name, ranges=[Range(startIndex=kept.start, endIndex=kept.end)]
            )
        )
    ranges = {name: NamedRanges(name=name, namedRanges=found) for name, found in grouped.items()} or None
    images = {
        object_id: InlineObject(
            objectId=object_id,
            inlineObjectProperties=InlineObjectProperties(
                embeddedObject=EmbeddedObject(
                    imageProperties=EmbeddedObjectImage(contentUri=image.uri, sourceUri=image.uri), size=image.size
                )
            ),
        )
        for object_id, image in doc.images.items()
    } or None
    if tabs:
        tab = Tab(
            tabProperties=TabProperties(tabId=FIRST_TAB, title="Tab 1", index=0),
            documentTab=DocumentTab(
                body=body,
                documentStyle=DocumentStyle(),
                namedStyles=_named_styles(),
                lists=lists,
                namedRanges=ranges,
                inlineObjects=images,
            ),
        )
        return Document(documentId=document_id, title=title, revisionId=revision_id, tabs=[tab])
    return Document(
        documentId=document_id,
        title=title,
        revisionId=revision_id,
        body=body,
        documentStyle=DocumentStyle(),
        namedStyles=_named_styles(),
        lists=lists,
        namedRanges=ranges,
        inlineObjects=images,
    )


# --------------------------------------------------------------------------- import and export


def from_text(text: str) -> DocBody:
    """Plain text as Drive converts it into a Doc: one NORMAL_TEXT paragraph per line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    paragraphs: list[Block] = [Para(pieces=[TextPiece(text=line)] if line else []) for line in lines]
    return DocBody(blocks=paragraphs or [Para()])


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_INLINE = re.compile(r"\*\*(?P<bold>[^*]+)\*\*|\[(?P<label>[^\]]+)\]\((?P<url>[^)\s]+)\)|\*(?P<italic>[^*]+)\*")


def _inline(text: str) -> list[Piece]:
    pieces: list[Piece] = []
    at = 0
    for found in _INLINE.finditer(text):
        if found.start() > at:
            pieces.append(TextPiece(text=text[at : found.start()]))
        if found.group("bold") is not None:
            pieces.append(TextPiece(text=found.group("bold"), style=TextStyle(bold=True)))
        elif found.group("italic") is not None:
            pieces.append(TextPiece(text=found.group("italic"), style=TextStyle(italic=True)))
        else:
            link = Link(url=found.group("url"))
            pieces.append(TextPiece(text=found.group("label"), style=TextStyle(link=link, underline=True)))
        at = found.end()
    if at < len(text):
        pieces.append(TextPiece(text=text[at:]))
    return pieces


def from_markdown(document_id: str, text: str) -> DocBody:
    """Markdown as Drive converts it into a Doc: `#` headings, `-` and `1.` lists (nested by indentation),
    pipe tables, and `**bold**`, `*italic*` and `[links](url)` inside a line. Anything else is a paragraph."""
    editor = Editor(document_id, empty())
    blocks: list[Block] = []
    bullets: dict[str, str] = {}
    previous_list: str | None = None
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|") and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
            header = _cells(line)
            body: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(_cells(lines[i]))
                i += 1
            columns = len(header)
            rows = [header, *[(row + [""] * columns)[:columns] for row in body]]
            if not blocks or not isinstance(blocks[-1], Para):
                blocks.append(Para())
            blocks.append(
                TableBlock(
                    rows=[Row(cells=[Cell(content=[Para(pieces=_inline(cell))]) for cell in row]) for row in rows]
                )
            )
            previous_list = None
            continue
        heading, bullet, numbered = _HEADING.match(line), _BULLET.match(line), _NUMBERED.match(line)
        if heading:
            level = len(heading.group(1))
            style = ParagraphStyle(namedStyleType=HEADINGS[level - 1], headingId=editor._mint("h."))
            blocks.append(Para(pieces=_inline(heading.group(2)), style=style))
            previous_list = None
        elif bullet or numbered:
            found = bullet or numbered
            assert found is not None
            preset = "BULLET_DISC_CIRCLE_SQUARE" if bullet else "NUMBERED_DECIMAL_ALPHA_ROMAN"
            if previous_list is None or bullets[previous_list] != preset:
                previous_list = editor._mint("kix.")
                bullets[previous_list] = preset
            level = min(len(found.group(1).replace("\t", "  ")) // 2, 8)
            blocks.append(
                Para(pieces=_inline(found.group(2)), bullet=Bullet(listId=previous_list, nestingLevel=level or None))
            )
        else:
            blocks.append(Para(pieces=_inline(line)) if line else Para())
            previous_list = None
        i += 1
    while len(blocks) > 1 and isinstance(blocks[-1], Para) and not blocks[-1].pieces and blocks[-1].bullet is None:
        blocks.pop()
    if not blocks or not isinstance(blocks[-1], Para):
        blocks.append(Para())
    lists = {list_id: List(preset=preset, properties=_list_for(preset)) for list_id, preset in bullets.items()}
    return DocBody(blocks=blocks, lists=lists, minted=editor.minted)


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [cell.strip() for cell in inner.split("|")]


def _para_text(para: Para, *, markdown: bool) -> str:
    parts: list[str] = []
    for piece in para.pieces:
        if not isinstance(piece, TextPiece):
            continue
        text = piece.text
        if markdown and piece.style.link is not None and piece.style.link.url:
            text = f"[{text}]({piece.style.link.url})"
        elif markdown and piece.style.bold:
            text = f"**{text}**"
        elif markdown and piece.style.italic:
            text = f"*{text}*"
        parts.append(text)
    return "".join(parts)


def text_of(doc: DocBody, *, markdown: bool = False) -> str:
    """The document as text, each paragraph a line: what a search reads and an export writes.

    Plain text marks a bulleted paragraph `* ` and a numbered one `1. `, indented by its nesting level, and
    writes a table one row to a line with its cells separated by tabs. Markdown writes headings with `#`,
    lists with `-` and `1.`, tables as pipe tables, and bold, italic and links inline."""
    lines: list[str] = []
    for block in doc.blocks:
        if isinstance(block, TableBlock):
            rows = [
                [" ".join(_para_text(p, markdown=markdown) for p in cell.content).strip() for cell in row.cells]
                for row in block.rows
            ]
            if markdown:
                lines.append("| " + " | ".join(rows[0]) + " |")
                lines.append("|" + "|".join(" --- " for _ in rows[0]) + "|")
                lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
            else:
                lines += ["\t".join(row) for row in rows]
            continue
        text = _para_text(block, markdown=markdown)
        if block.bullet is not None:
            level = block.bullet.nestingLevel or 0
            numbered = (
                doc.lists[block.bullet.listId].preset.startswith("NUMBERED")
                if block.bullet.listId in doc.lists
                else False
            )
            mark = "1." if numbered else ("-" if markdown else "*")
            text = ("  " if markdown else "    ") * level + f"{mark} {text}"
        elif markdown and block.style.namedStyleType in HEADINGS:
            text = "#" * (HEADINGS.index(block.style.namedStyleType) + 1) + " " + text
        elif markdown and block.style.namedStyleType is NamedStyle.TITLE:
            text = "# " + text
        lines.append(text)
    return "\n".join(lines)


def append_paragraph(doc: DocBody, text: str) -> DocBody:
    """`text` added as paragraphs at the end of the document, in the style of a plain paragraph."""
    blocks = list(doc.blocks)
    for line in text.split("\n"):
        blocks.append(Para(pieces=[TextPiece(text=line)] if line else []))
    return doc.model_copy(update={"blocks": blocks})
