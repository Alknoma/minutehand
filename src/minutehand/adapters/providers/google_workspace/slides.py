"""Slides v1: a presentation as the store keeps it, the `batchUpdate` requests that change it, and the
`Presentation` it is served as. Slides' own JSON is parsed and built here, as Drive's is in `wire.py`.

A presentation made through Drive (`files.create` with the presentation type) has one slide on the `TITLE`
layout, as Google's own does, with a centred title and a subtitle placeholder. Text indexes count UTF-16
code units from 0 within one shape or table cell; text that is not empty always ends in a newline, which
holds its paragraph's style.

Images are not fetched. An image taken from a Drive file (`https://drive.google.com/uc?id=...`) must be a
file the run holds that anyone with the link may read, or the request is refused the way Slides refuses an
image it cannot retrieve; any other URL is taken on trust.

**Not built**, answered with 501 `UNIMPLEMENTED`: the request kinds no caller of this fake sends (named in
`NOT_BUILT`), videos, charts, lines, groups and word art, and `writeControl`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Literal, NamedTuple

from pydantic import Field, ValidationError

from minutehand.domain.errors import Asked, NotServed, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

EMU_PER_PT = 12700
SLIDE_WIDTH = 9144000
SLIDE_HEIGHT = 5143500

# --------------------------------------------------------------------------- Slides' own shapes


class Dimension(Model):
    magnitude: float | None = None
    unit: Literal["EMU", "PT", "UNIT_UNSPECIFIED"] | None = None


class Size(Model):
    width: Dimension | None = None
    height: Dimension | None = None


class AffineTransform(Model):
    scaleX: float | None = None
    scaleY: float | None = None
    shearX: float | None = None
    shearY: float | None = None
    translateX: float | None = None
    translateY: float | None = None
    unit: Literal["EMU", "PT", "UNIT_UNSPECIFIED"] | None = None


class RgbColor(Model):
    red: float | None = None
    green: float | None = None
    blue: float | None = None


class OpaqueColor(Model):
    rgbColor: RgbColor | None = None
    themeColor: str | None = None


class OptionalColor(Model):
    opaqueColor: OpaqueColor | None = None


class SolidFill(Model):
    color: OpaqueColor | None = None
    alpha: float | None = None


class Link(Model):
    url: str | None = None
    relativeLink: str | None = None
    pageObjectId: str | None = None
    slideIndex: int | None = None


class WeightedFontFamily(Model):
    fontFamily: str | None = None
    weight: int | None = None


class TextStyle(Model):
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikethrough: bool | None = None
    smallCaps: bool | None = None
    fontFamily: str | None = None
    fontSize: Dimension | None = None
    foregroundColor: OptionalColor | None = None
    backgroundColor: OptionalColor | None = None
    link: Link | None = None
    baselineOffset: str | None = None
    weightedFontFamily: WeightedFontFamily | None = None


class ParagraphStyle(Model):
    alignment: Literal["START", "CENTER", "END", "JUSTIFIED", "ALIGNMENT_UNSPECIFIED"] | None = None
    lineSpacing: float | None = None
    direction: Literal["LEFT_TO_RIGHT", "RIGHT_TO_LEFT", "TEXT_DIRECTION_UNSPECIFIED"] | None = None
    spacingMode: Literal["NEVER_COLLAPSE", "COLLAPSE_LISTS", "SPACING_MODE_UNSPECIFIED"] | None = None
    spaceAbove: Dimension | None = None
    spaceBelow: Dimension | None = None
    indentStart: Dimension | None = None
    indentEnd: Dimension | None = None
    indentFirstLine: Dimension | None = None


class ShapeBackgroundFill(Model):
    solidFill: SolidFill | None = None
    propertyState: Literal["RENDERED", "NOT_RENDERED", "INHERIT"] | None = None


class OutlineFill(Model):
    solidFill: SolidFill | None = None


class Outline(Model):
    outlineFill: OutlineFill | None = None
    weight: Dimension | None = None
    dashStyle: str | None = None
    propertyState: Literal["RENDERED", "NOT_RENDERED", "INHERIT"] | None = None


class ShapeProperties(Model):
    shapeBackgroundFill: ShapeBackgroundFill | None = None
    outline: Outline | None = None
    contentAlignment: Literal["TOP", "MIDDLE", "BOTTOM", "CONTENT_ALIGNMENT_UNSPECIFIED"] | None = None
    link: Link | None = None


class StretchedPictureFill(Model):
    contentUrl: str
    size: Size | None = None


class PageBackgroundFill(Model):
    solidFill: SolidFill | None = None
    stretchedPictureFill: StretchedPictureFill | None = None
    propertyState: Literal["RENDERED", "NOT_RENDERED", "INHERIT"] | None = None


class PageProperties(Model):
    pageBackgroundFill: PageBackgroundFill | None = None


class TableCellBackgroundFill(Model):
    solidFill: SolidFill | None = None
    propertyState: Literal["RENDERED", "NOT_RENDERED", "INHERIT"] | None = None


class TableCellProperties(Model):
    tableCellBackgroundFill: TableCellBackgroundFill | None = None
    contentAlignment: Literal["TOP", "MIDDLE", "BOTTOM", "CONTENT_ALIGNMENT_UNSPECIFIED"] | None = None


class TableColumnProperties(Model):
    columnWidth: Dimension | None = None


class Placeholder(Model):
    type: str
    index: int | None = None
    parentObjectId: str | None = None


# --------------------------------------------------------------------------- what the store keeps


class Run(Model):
    text: str
    style: TextStyle = TextStyle()


class Paragraph(Model):
    runs: list[Run] = Field(default=[], description="Without the newline that ends it")
    style: ParagraphStyle = ParagraphStyle()
    newline: TextStyle = TextStyle()


class StoredCell(Model):
    text: list[Paragraph] = []
    properties: TableCellProperties = TableCellProperties()


class StoredShape(Model):
    kind: Literal["shape"] = "shape"
    shape_type: str
    text: list[Paragraph] = []
    properties: ShapeProperties = ShapeProperties()
    placeholder: Placeholder | None = None


class StoredTable(Model):
    kind: Literal["table"] = "table"
    rows: list[list[StoredCell]]
    columns: list[TableColumnProperties]


class StoredImage(Model):
    kind: Literal["image"] = "image"
    source: str


class Element(Model):
    object_id: str
    size: Size | None = None
    transform: AffineTransform = AffineTransform(scaleX=1, scaleY=1, unit="EMU")
    content: StoredShape | StoredTable | StoredImage = Field(discriminator="kind")


class Slide(Model):
    object_id: str
    layout: str
    elements: list[Element] = []
    notes_id: str
    notes_shape_id: str
    notes: list[Paragraph] = []
    properties: PageProperties = PageProperties()


class Deck(Model):
    """A Slides presentation as the store keeps it, between the file's metadata and Drive's other content kinds."""

    kind: Literal["deck"] = "deck"
    slides: list[Slide]
    minted: int = 0


LAYOUTS: dict[str, tuple[str, ...]] = {
    "BLANK": (),
    "CAPTION_ONLY": ("BODY",),
    "TITLE": ("CENTERED_TITLE", "SUBTITLE"),
    "TITLE_AND_BODY": ("TITLE", "BODY"),
    "TITLE_AND_TWO_COLUMNS": ("TITLE", "BODY", "BODY"),
    "TITLE_ONLY": ("TITLE",),
    "SECTION_HEADER": ("TITLE",),
    "SECTION_TITLE_AND_DESCRIPTION": ("TITLE", "SUBTITLE", "BODY"),
    "ONE_COLUMN_TEXT": ("TITLE", "BODY"),
    "MAIN_POINT": ("TITLE",),
    "BIG_NUMBER": ("TITLE", "BODY"),
}
"""The predefined layouts of Google's default theme, and the placeholders each one puts on a new slide."""

MASTER_ID = "p"
NOTES_MASTER_ID = "n"


def layout_id(name: str) -> str:
    return f"p{list(LAYOUTS).index(name) + 1}"


def _placeholder_size(kind: str) -> tuple[Size, AffineTransform]:
    tall = kind in ("BODY",)
    size = Size(
        width=Dimension(magnitude=8520600, unit="EMU"),
        height=Dimension(magnitude=3416400 if tall else 572700, unit="EMU"),
    )
    top = 1152475 if tall else (1583350 if kind == "CENTERED_TITLE" else 445025)
    return size, AffineTransform(scaleX=1, scaleY=1, translateX=311700, translateY=top, unit="EMU")


# --------------------------------------------------------------------------- requests


class LayoutReference(Model):
    predefinedLayout: str | None = None
    layoutId: str | None = None


class CreateSlide(Model):
    objectId: str | None = None
    insertionIndex: int | None = None
    slideLayoutReference: LayoutReference | None = None


class DeleteObject(Model):
    objectId: str


class UpdatePageProperties(Model):
    objectId: str
    pageProperties: PageProperties
    fields: str


class PageElementProperties(Model):
    pageObjectId: str
    size: Size | None = None
    transform: AffineTransform | None = None


class CreateImage(Model):
    objectId: str | None = None
    url: str
    elementProperties: PageElementProperties


class CreateShape(Model):
    objectId: str | None = None
    shapeType: str
    elementProperties: PageElementProperties


class UpdateShapeProperties(Model):
    objectId: str
    shapeProperties: ShapeProperties
    fields: str


class CreateTable(Model):
    objectId: str | None = None
    elementProperties: PageElementProperties
    rows: int
    columns: int


class CellLocation(Model):
    rowIndex: int = 0
    columnIndex: int = 0


class TableRange(Model):
    location: CellLocation = CellLocation()
    rowSpan: int = 1
    columnSpan: int = 1


class UpdateTableColumnProperties(Model):
    objectId: str
    columnIndices: list[int] = []
    tableColumnProperties: TableColumnProperties
    fields: str


class UpdateTableCellProperties(Model):
    objectId: str
    tableRange: TableRange | None = None
    tableCellProperties: TableCellProperties
    fields: str


class TextRange(Model):
    type: Literal["FIXED_RANGE", "FROM_START_INDEX", "ALL"] = "ALL"
    startIndex: int | None = None
    endIndex: int | None = None


class InsertText(Model):
    objectId: str
    cellLocation: CellLocation | None = None
    text: str
    insertionIndex: int = 0


class DeleteText(Model):
    objectId: str
    cellLocation: CellLocation | None = None
    textRange: TextRange = TextRange()


class UpdateTextStyle(Model):
    objectId: str
    cellLocation: CellLocation | None = None
    style: TextStyle
    textRange: TextRange = TextRange()
    fields: str


class UpdateParagraphStyle(Model):
    objectId: str
    cellLocation: CellLocation | None = None
    style: ParagraphStyle
    textRange: TextRange = TextRange()
    fields: str


class SubstringMatchCriteria(Model):
    text: str
    matchCase: bool = False


class ReplaceAllText(Model):
    containsText: SubstringMatchCriteria
    replaceText: str = ""
    pageObjectIds: list[str] = []


class Request(Model):
    createSlide: CreateSlide | None = None
    deleteObject: DeleteObject | None = None
    updatePageProperties: UpdatePageProperties | None = None
    createImage: CreateImage | None = None
    createShape: CreateShape | None = None
    updateShapeProperties: UpdateShapeProperties | None = None
    createTable: CreateTable | None = None
    updateTableColumnProperties: UpdateTableColumnProperties | None = None
    updateTableCellProperties: UpdateTableCellProperties | None = None
    insertText: InsertText | None = None
    deleteText: DeleteText | None = None
    updateTextStyle: UpdateTextStyle | None = None
    updateParagraphStyle: UpdateParagraphStyle | None = None
    replaceAllText: ReplaceAllText | None = None


NOT_BUILT = frozenset(
    {
        "createLine",
        "createVideo",
        "createSheetsChart",
        "refreshSheetsChart",
        "createParagraphBullets",
        "deleteParagraphBullets",
        "insertTableRows",
        "insertTableColumns",
        "deleteTableRow",
        "deleteTableColumn",
        "mergeTableCells",
        "unmergeTableCells",
        "updateTableBorderProperties",
        "updateTableRowProperties",
        "updatePageElementTransform",
        "updatePageElementAltText",
        "updatePageElementsZOrder",
        "updateSlidesPosition",
        "updateSlideProperties",
        "updateImageProperties",
        "updateLineProperties",
        "updateLineCategory",
        "updateVideoProperties",
        "replaceAllShapesWithImage",
        "replaceAllShapesWithSheetsChart",
        "replaceImage",
        "duplicateObject",
        "groupObjects",
        "ungroupObjects",
        "rerouteLine",
    }
)


class WriteControl(Model):
    requiredRevisionId: str | None = None


class BatchUpdate(Model):
    requests: list[Request] = []
    writeControl: WriteControl | None = None


class ObjectReply(Model):
    objectId: str


class ReplaceAllTextReply(Model):
    occurrencesChanged: int = 0


class Reply(Model):
    createSlide: ObjectReply | None = None
    createImage: ObjectReply | None = None
    createShape: ObjectReply | None = None
    createTable: ObjectReply | None = None
    replaceAllText: ReplaceAllTextReply | None = None


class WriteControlAnswer(Model):
    requiredRevisionId: str


class BatchUpdateAnswer(Model):
    presentationId: str
    replies: list[Reply]
    writeControl: WriteControlAnswer


class Refused(ServiceRefusal):
    """A request Slides refuses: the status, Google's status word, and its message."""

    def __init__(self, code: int, status: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message

    def render(self, asked: Asked) -> Rendered:
        """Google's v1 envelope, code, message and status word, as the app answers it."""
        body = {"error": {"code": self.code, "message": self.message, "status": self.status}}
        return Rendered(
            status=self.code,
            content_type="application/json; charset=UTF-8",
            body=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(),
        )


class NotBuilt(Refused, NotServed):
    """A request real Slides takes that this fake does not build: 501 `UNIMPLEMENTED`, naming it."""

    def __init__(self, message: str) -> None:
        super().__init__(501, "UNIMPLEMENTED", message)


def _invalid(message: str) -> Refused:
    return Refused(400, "INVALID_ARGUMENT", message)


_OBJECT_ID = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_\-:]{4,49}$")


def new_deck(presentation_id: str) -> Deck:
    """A presentation as Google makes a new one: one slide on the TITLE layout."""
    editor = Editor(presentation_id, Deck(slides=[]), image_readable=lambda _: True)
    editor.create_slide(None, None, "TITLE")
    return editor.result()


def deck_from_text(presentation_id: str, text: str) -> Deck:
    """A seeded presentation: one TITLE_AND_BODY slide per block of lines, the first line its title."""
    editor = Editor(presentation_id, Deck(slides=[]), image_readable=lambda _: True)
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", text) if b.strip()]
    for block in blocks or [""]:
        slide = editor.create_slide(None, None, "TITLE_AND_BODY")
        title, _, body = block.partition("\n")
        shapes = [e for e in editor.slide(slide).elements if isinstance(e.content, StoredShape)]
        if title:
            editor.insert_text(shapes[0].object_id, None, title, 0)
        if body:
            editor.insert_text(shapes[1].object_id, None, body, 0)
    return editor.result()


def append_slide(presentation_id: str, deck: Deck, text: str) -> Deck:
    """A person adds a slide at the end whose body is `text`."""
    editor = Editor(presentation_id, deck, image_readable=lambda _: True)
    slide = editor.create_slide(None, None, "TITLE_AND_BODY")
    shapes = [e for e in editor.slide(slide).elements if isinstance(e.content, StoredShape)]
    editor.insert_text(shapes[1].object_id, None, text, 0)
    return editor.result()


class Spot(NamedTuple):
    slide: int
    element: int


class Editor:
    def __init__(self, presentation_id: str, deck: Deck, *, image_readable: Callable[[str], bool]) -> None:
        self._id = presentation_id
        self.slides = [s.model_copy(deep=True) for s in deck.slides]
        self.minted = deck.minted
        self._readable = image_readable
        self._where = ""

    def result(self) -> Deck:
        return Deck(slides=self.slides, minted=self.minted)

    def _mint(self) -> str:
        self.minted += 1
        return "g" + hashlib.sha256(f"{self._id}\x1f{self.minted}".encode()).hexdigest()[:12]

    def _ids(self) -> set[str]:
        found = {MASTER_ID, NOTES_MASTER_ID, *(layout_id(name) for name in LAYOUTS)}
        for slide in self.slides:
            found |= {slide.object_id, slide.notes_id, slide.notes_shape_id, *(e.object_id for e in slide.elements)}
        return found

    def _new_id(self, asked: str | None) -> str:
        if asked is None:
            return self._mint()
        if not _OBJECT_ID.match(asked):
            raise _invalid(
                f"{self._where}: The object ID ({asked}) should be 5-50 characters of letters, digits and _-:."
            )
        if asked in self._ids():
            raise _invalid(
                f"{self._where}: The object ID ({asked}) should be unique among all pages and page elements in the presentation."
            )
        return asked

    def slide(self, object_id: str) -> Slide:
        for slide in self.slides:
            if slide.object_id == object_id:
                return slide
        raise _invalid(f"{self._where}: The object ({object_id}) could not be found.")

    def _element(self, object_id: str) -> Spot:
        for s, slide in enumerate(self.slides):
            for e, element in enumerate(slide.elements):
                if element.object_id == object_id:
                    return Spot(s, e)
        raise _invalid(f"{self._where}: The object ({object_id}) could not be found.")

    def _put(self, spot: Spot, element: Element) -> None:
        slide = self.slides[spot.slide]
        elements = list(slide.elements)
        elements[spot.element] = element
        self.slides[spot.slide] = slide.model_copy(update={"elements": elements})

    # ---------------------------------------------------------------- requests

    def apply(self, request: Request, position: int) -> Reply:
        set_fields = [name for name in type(request).model_fields if getattr(request, name) is not None]
        if len(set_fields) != 1:
            raise _invalid(f"Invalid requests[{position}]: exactly one kind of request must be set.")
        kind = set_fields[0]
        self._where = f"Invalid requests[{position}].{kind}"
        handler: Callable[[Model], Reply] = getattr(self, f"_{kind}")
        return handler(getattr(request, kind))

    def create_slide(self, object_id: str | None, index: int | None, layout: str) -> str:
        slide_id = self._new_id(object_id)
        elements: list[Element] = []
        for kind in LAYOUTS[layout]:
            size, transform = _placeholder_size(kind)
            shape = StoredShape(
                shape_type="TEXT_BOX", placeholder=Placeholder(type=kind, parentObjectId=layout_id(layout))
            )
            elements.append(Element(object_id=self._mint(), size=size, transform=transform, content=shape))
        slide = Slide(
            object_id=slide_id, layout=layout, elements=elements, notes_id=self._mint(), notes_shape_id=self._mint()
        )
        at = len(self.slides) if index is None else index
        if not 0 <= at <= len(self.slides):
            raise _invalid(f"{self._where}: The insertion index {at} is out of range.")
        self.slides.insert(at, slide)
        return slide_id

    def _createSlide(self, asked: CreateSlide) -> Reply:
        layout = "BLANK"
        if asked.slideLayoutReference is not None:
            reference = asked.slideLayoutReference
            if reference.predefinedLayout is not None:
                if reference.predefinedLayout not in LAYOUTS:
                    raise _invalid(f"{self._where}: Invalid value for predefinedLayout: {reference.predefinedLayout}")
                layout = reference.predefinedLayout
            elif reference.layoutId is not None:
                names = [name for name in LAYOUTS if layout_id(name) == reference.layoutId]
                if not names:
                    raise _invalid(f"{self._where}: The layout ({reference.layoutId}) could not be found.")
                layout = names[0]
        return Reply(createSlide=ObjectReply(objectId=self.create_slide(asked.objectId, asked.insertionIndex, layout)))

    def _deleteObject(self, asked: DeleteObject) -> Reply:
        for s, slide in enumerate(self.slides):
            if slide.object_id == asked.objectId:
                del self.slides[s]
                return Reply()
        spot = self._element(asked.objectId)
        slide = self.slides[spot.slide]
        self.slides[spot.slide] = slide.model_copy(
            update={"elements": [e for e in slide.elements if e.object_id != asked.objectId]}
        )
        return Reply()

    def _updatePageProperties(self, asked: UpdatePageProperties) -> Reply:
        slide = self.slide(asked.objectId)
        names = _names(asked.fields, PageProperties, self._where)
        fill = asked.pageProperties.pageBackgroundFill
        if fill is not None and fill.stretchedPictureFill is not None:
            self._image(fill.stretchedPictureFill.contentUrl)
        properties = _merged(slide.properties, asked.pageProperties, names)
        self.slides[self.slides.index(slide)] = slide.model_copy(update={"properties": properties})
        return Reply()

    def _place(self, asked: PageElementProperties) -> Slide:
        return self.slide(asked.pageObjectId)

    def _add(self, slide: Slide, element: Element) -> None:
        self.slides[self.slides.index(slide)] = slide.model_copy(update={"elements": [*slide.elements, element]})

    def _image(self, url: str) -> None:
        if not url.startswith(("https://", "http://")) or len(url) > 2000:
            raise _invalid(f"{self._where}: The URL must be public and shorter than 2 kB.")
        if not self._readable(url):
            raise _invalid(
                f"{self._where}: There was a problem retrieving the image. The provided image should be publicly "
                "accessible, within size limit, and in supported formats."
            )

    def _createImage(self, asked: CreateImage) -> Reply:
        slide = self._place(asked.elementProperties)
        self._image(asked.url)
        object_id = self._new_id(asked.objectId)
        transform = asked.elementProperties.transform or AffineTransform(scaleX=1, scaleY=1, unit="EMU")
        element = Element(
            object_id=object_id,
            size=asked.elementProperties.size,
            transform=transform,
            content=StoredImage(source=asked.url),
        )
        self._add(slide, element)
        return Reply(createImage=ObjectReply(objectId=object_id))

    def _createShape(self, asked: CreateShape) -> Reply:
        slide = self._place(asked.elementProperties)
        object_id = self._new_id(asked.objectId)
        transform = asked.elementProperties.transform or AffineTransform(scaleX=1, scaleY=1, unit="EMU")
        element = Element(
            object_id=object_id,
            size=asked.elementProperties.size,
            transform=transform,
            content=StoredShape(shape_type=asked.shapeType),
        )
        self._add(slide, element)
        return Reply(createShape=ObjectReply(objectId=object_id))

    def _updateShapeProperties(self, asked: UpdateShapeProperties) -> Reply:
        spot = self._element(asked.objectId)
        element = self.slides[spot.slide].elements[spot.element]
        if not isinstance(element.content, StoredShape):
            raise _invalid(f"{self._where}: The object ({asked.objectId}) is not a shape.")
        names = _names(asked.fields, ShapeProperties, self._where)
        content = element.content.model_copy(
            update={"properties": _merged(element.content.properties, asked.shapeProperties, names)}
        )
        self._put(spot, element.model_copy(update={"content": content}))
        return Reply()

    def _createTable(self, asked: CreateTable) -> Reply:
        if asked.rows < 1 or asked.columns < 1:
            raise _invalid(f"{self._where}: A table must have at least one row and one column.")
        slide = self._place(asked.elementProperties)
        object_id = self._new_id(asked.objectId)
        width = 3000000 // asked.columns
        table = StoredTable(
            rows=[[StoredCell() for _ in range(asked.columns)] for _ in range(asked.rows)],
            columns=[
                TableColumnProperties(columnWidth=Dimension(magnitude=width, unit="EMU")) for _ in range(asked.columns)
            ],
        )
        transform = asked.elementProperties.transform or AffineTransform(scaleX=1, scaleY=1, unit="EMU")
        self._add(
            slide, Element(object_id=object_id, size=asked.elementProperties.size, transform=transform, content=table)
        )
        return Reply(createTable=ObjectReply(objectId=object_id))

    def _table(self, object_id: str) -> tuple[Spot, Element, StoredTable]:
        spot = self._element(object_id)
        element = self.slides[spot.slide].elements[spot.element]
        if not isinstance(element.content, StoredTable):
            raise _invalid(f"{self._where}: The object ({object_id}) is not a table.")
        return spot, element, element.content

    def _updateTableColumnProperties(self, asked: UpdateTableColumnProperties) -> Reply:
        spot, element, table = self._table(asked.objectId)
        names = _names(asked.fields, TableColumnProperties, self._where)
        columns = list(table.columns)
        for index in asked.columnIndices or range(len(columns)):
            if not 0 <= index < len(columns):
                raise _invalid(f"{self._where}: The column index {index} is out of range.")
            columns[index] = _merged(columns[index], asked.tableColumnProperties, names)
        self._put(spot, element.model_copy(update={"content": table.model_copy(update={"columns": columns})}))
        return Reply()

    def _updateTableCellProperties(self, asked: UpdateTableCellProperties) -> Reply:
        spot, element, table = self._table(asked.objectId)
        names = _names(asked.fields, TableCellProperties, self._where)
        reach = asked.tableRange or TableRange(
            location=CellLocation(), rowSpan=len(table.rows), columnSpan=len(table.columns)
        )
        rows = [list(row) for row in table.rows]
        top, left = reach.location.rowIndex, reach.location.columnIndex
        if top + reach.rowSpan > len(rows) or left + reach.columnSpan > len(table.columns) or top < 0 or left < 0:
            raise _invalid(f"{self._where}: The table range is outside the table.")
        for r in range(top, top + reach.rowSpan):
            for c in range(left, left + reach.columnSpan):
                cell = rows[r][c]
                rows[r][c] = cell.model_copy(
                    update={"properties": _merged(cell.properties, asked.tableCellProperties, names)}
                )
        self._put(spot, element.model_copy(update={"content": table.model_copy(update={"rows": rows})}))
        return Reply()

    # ---------------------------------------------------------------- text

    def _text(
        self, object_id: str, cell: CellLocation | None
    ) -> tuple[list[Paragraph], Callable[[list[Paragraph]], None]]:
        for s, slide in enumerate(self.slides):
            if slide.notes_shape_id == object_id and cell is None:

                def keep_notes(text: list[Paragraph], s: int = s) -> None:
                    self.slides[s] = self.slides[s].model_copy(update={"notes": text})

                return list(slide.notes), keep_notes
        spot = self._element(object_id)
        element = self.slides[spot.slide].elements[spot.element]
        content = element.content
        if isinstance(content, StoredShape) and cell is None:

            def keep_shape(text: list[Paragraph]) -> None:
                self._put(spot, element.model_copy(update={"content": content.model_copy(update={"text": text})}))

            return list(content.text), keep_shape
        if isinstance(content, StoredTable) and cell is not None:
            if not (0 <= cell.rowIndex < len(content.rows) and 0 <= cell.columnIndex < len(content.columns)):
                raise _invalid(f"{self._where}: The cell location is outside the table.")
            found = content.rows[cell.rowIndex][cell.columnIndex]

            def keep_cell(text: list[Paragraph]) -> None:
                rows = [list(row) for row in content.rows]
                rows[cell.rowIndex][cell.columnIndex] = found.model_copy(update={"text": text})
                self._put(spot, element.model_copy(update={"content": content.model_copy(update={"rows": rows})}))

            return list(found.text), keep_cell
        if isinstance(content, StoredTable):
            raise _invalid(f"{self._where}: A table's text is addressed by its cellLocation.")
        raise _invalid(f"{self._where}: The object ({object_id}) has no text.")

    def insert_text(self, object_id: str, cell: CellLocation | None, text: str, at: int) -> None:
        paragraphs, keep = self._text(object_id, cell)
        chars = _flat(paragraphs)
        if not 0 <= at <= max(len(chars) - 1, 0):
            raise _invalid(
                f"{self._where}: The insertion index {at} must be within the text, 0 to {max(len(chars) - 1, 0)}."
            )
        style = chars[at - 1].style if at > 0 else (chars[0].style if chars else TextStyle())
        closing = next((c for c in chars[at:] if c.char == "\n"), None)
        paragraph = closing.paragraph if closing is not None else ParagraphStyle()
        new = [Char(ch, style, paragraph if ch == "\n" else None) for ch in text]
        if not chars:
            new.append(Char("\n", style, ParagraphStyle()))
        keep(_unflat(chars[:at] + new + chars[at:]))

    def _insertText(self, asked: InsertText) -> Reply:
        self.insert_text(
            asked.objectId,
            asked.cellLocation,
            asked.text,
            _units_to_chars(self._chars_of(asked.objectId, asked.cellLocation), asked.insertionIndex, self._where),
        )
        return Reply()

    def _chars_of(self, object_id: str, cell: CellLocation | None) -> list[Char]:
        return _flat(self._text(object_id, cell)[0])

    def _bounds(self, chars: list[Char], reach: TextRange) -> tuple[int, int]:
        total = sum(_utf16(c.char) for c in chars)
        if reach.type == "ALL":
            start, end = 0, total
        elif reach.type == "FROM_START_INDEX":
            start, end = reach.startIndex or 0, total
        else:
            if reach.startIndex is None or reach.endIndex is None:
                raise _invalid(f"{self._where}: A FIXED_RANGE needs startIndex and endIndex.")
            start, end = reach.startIndex, reach.endIndex
        if not 0 <= start <= end <= total:
            raise _invalid(
                f"{self._where}: The text range [{start}, {end}) is out of bounds; the text has {total} characters."
            )
        return _units_to_chars(chars, start, self._where), _units_to_chars(chars, end, self._where)

    def _deleteText(self, asked: DeleteText) -> Reply:
        paragraphs, keep = self._text(asked.objectId, asked.cellLocation)
        chars = _flat(paragraphs)
        start, end = self._bounds(chars, asked.textRange)
        left = chars[:start] + chars[end:]
        if left and left[-1].char != "\n":
            left.append(Char("\n", left[-1].style, ParagraphStyle()))
        if len(left) == 1:
            left = []
        keep(_unflat(left))
        return Reply()

    def _updateTextStyle(self, asked: UpdateTextStyle) -> Reply:
        paragraphs, keep = self._text(asked.objectId, asked.cellLocation)
        chars = _flat(paragraphs)
        start, end = self._bounds(chars, asked.textRange)
        names = _names(asked.fields, TextStyle, self._where)
        for i in range(start, end):
            chars[i] = chars[i]._replace(style=_merged(chars[i].style, asked.style, names))
        keep(_unflat(chars))
        return Reply()

    def _updateParagraphStyle(self, asked: UpdateParagraphStyle) -> Reply:
        paragraphs, keep = self._text(asked.objectId, asked.cellLocation)
        chars = _flat(paragraphs)
        start, end = self._bounds(chars, asked.textRange)
        names = _names(asked.fields, ParagraphStyle, self._where)
        for i in range(start, len(chars)):
            if chars[i].char == "\n":
                chars[i] = chars[i]._replace(
                    paragraph=_merged(chars[i].paragraph or ParagraphStyle(), asked.style, names)
                )
                if i >= end - 1:
                    break
        keep(_unflat(chars))
        return Reply()

    def _replaceAllText(self, asked: ReplaceAllText) -> Reply:
        needle = asked.containsText.text
        if not needle:
            raise _invalid(f"{self._where}: The search text must not be empty.")
        changed = 0
        for slide in list(self.slides):
            if asked.pageObjectIds and slide.object_id not in asked.pageObjectIds:
                continue
            targets: list[tuple[str, CellLocation | None]] = []
            for element in slide.elements:
                if isinstance(element.content, StoredShape):
                    targets.append((element.object_id, None))
                elif isinstance(element.content, StoredTable):
                    targets += [
                        (element.object_id, CellLocation(rowIndex=r, columnIndex=c))
                        for r in range(len(element.content.rows))
                        for c in range(len(element.content.columns))
                    ]
            for object_id, cell in targets:
                paragraphs, keep = self._text(object_id, cell)
                chars = _flat(paragraphs)
                text = "".join(c.char for c in chars)
                haystack, target = (text, needle) if asked.containsText.matchCase else (text.lower(), needle.lower())
                found = [m.start() for m in re.finditer(re.escape(target), haystack)]
                for at in reversed(found):
                    style = chars[at].style
                    chars[at : at + len(needle)] = [Char(ch, style, None) for ch in asked.replaceText]
                changed += len(found)
                if found:
                    keep(_unflat(chars))
        return Reply(replaceAllText=ReplaceAllTextReply(occurrencesChanged=changed))


class Char(NamedTuple):
    """One code point of a shape's text while it is edited; a newline carries its paragraph's style."""

    char: str
    style: TextStyle
    paragraph: ParagraphStyle | None


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _units_to_chars(chars: list[Char], index: int, where: str) -> int:
    """A UTF-16 index as a position in `chars`."""
    at = 0
    for position, char in enumerate(chars):
        if at == index:
            return position
        at += _utf16(char.char)
        if at > index:
            raise _invalid(f"{where}: The index {index} falls inside a surrogate pair.")
    if at == index:
        return len(chars)
    raise _invalid(f"{where}: The index {index} is beyond the end of the text.")


def _flat(paragraphs: list[Paragraph]) -> list[Char]:
    chars: list[Char] = []
    for paragraph in paragraphs:
        for run in paragraph.runs:
            chars += [Char(ch, run.style, None) for ch in run.text]
        chars.append(Char("\n", paragraph.newline, paragraph.style))
    return chars


def _unflat(chars: list[Char]) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    runs: list[Run] = []
    for char in chars:
        if char.char == "\n":
            paragraphs.append(Paragraph(runs=runs, style=char.paragraph or ParagraphStyle(), newline=char.style))
            runs = []
        elif runs and runs[-1].style == char.style:
            runs[-1] = Run(text=runs[-1].text + char.char, style=char.style)
        else:
            runs.append(Run(text=char.char, style=char.style))
    return paragraphs


def _names(fields: str, model: type[Model], where: str) -> list[str] | None:
    names = [f.strip() for f in fields.split(",") if f.strip()]
    if not names:
        raise _invalid(f"{where}: At least one field must be listed in 'fields'. (Use '*' to indicate all fields.)")
    if names == ["*"]:
        return None
    tops = [name.split(".", 1)[0] for name in names]
    for top in tops:
        if top not in model.model_fields:
            raise _invalid(f"{where}: Invalid field: {top}")
    return tops


def _merged[S: Model](current: S, asked: S, names: list[str] | None) -> S:
    chosen = list(type(current).model_fields) if names is None else names
    return current.model_copy(update={name: getattr(asked, name) for name in chosen})


def read_batch(raw: bytes) -> BatchUpdate:
    try:
        return BatchUpdate.model_validate_json(raw or b"{}")
    except ValidationError as error:
        for problem in error.errors():
            location = [str(part) for part in problem["loc"]]
            if len(location) >= 3 and location[0] == "requests" and location[2] in NOT_BUILT:
                raise NotBuilt(f"this simulation does not build the Slides request {location[2]}") from error
        where = ".".join(str(part) for part in error.errors()[0]["loc"])
        raise _invalid(f"Invalid JSON payload received. Unknown name or bad value at '{where}'") from error


def update(
    presentation_id: str, deck: Deck, asked: BatchUpdate, *, image_readable: Callable[[str], bool]
) -> tuple[Deck, list[Reply]]:
    if asked.writeControl is not None:
        raise NotBuilt("this simulation does not take writeControl on a Slides batchUpdate")
    editor = Editor(presentation_id, deck, image_readable=image_readable)
    replies = [editor.apply(request, position) for position, request in enumerate(asked.requests)]
    return editor.result(), replies


def text_of(deck: Deck) -> str:
    """Every slide's text, shape by shape, a blank line between slides: what a search reads and an export writes."""
    slides: list[str] = []
    for slide in deck.slides:
        parts: list[str] = []
        for element in slide.elements:
            if isinstance(element.content, StoredShape) and element.content.text:
                parts.append(_plain(element.content.text))
            elif isinstance(element.content, StoredTable):
                for row in element.content.rows:
                    parts.append("\t".join(_plain(cell.text) for cell in row))
        slides.append("\n".join(parts))
    return "\n\n".join(slides)


def _plain(paragraphs: list[Paragraph]) -> str:
    return "\n".join("".join(run.text for run in p.runs) for p in paragraphs)


# --------------------------------------------------------------------------- the Presentation served


class ParagraphMarker(Model):
    style: ParagraphStyle = ParagraphStyle()


class TextRunOut(Model):
    content: str
    style: TextStyle = TextStyle()


class TextElement(Model):
    startIndex: int | None = Field(default=None, description="Absent when 0, as Slides serves it")
    endIndex: int
    paragraphMarker: ParagraphMarker | None = None
    textRun: TextRunOut | None = None


class TextContent(Model):
    textElements: list[TextElement]


class ShapeOut(Model):
    shapeType: str
    text: TextContent | None = None
    shapeProperties: ShapeProperties = ShapeProperties()
    placeholder: Placeholder | None = None


class CellLocationOut(Model):
    rowIndex: int | None = None
    columnIndex: int | None = None


class TableCellOut(Model):
    location: CellLocationOut
    rowSpan: int = 1
    columnSpan: int = 1
    text: TextContent | None = None
    tableCellProperties: TableCellProperties = TableCellProperties()


class TableRowOut(Model):
    rowHeight: Dimension = Dimension(magnitude=370840, unit="EMU")
    tableCells: list[TableCellOut]


class TableOut(Model):
    rows: int
    columns: int
    tableRows: list[TableRowOut]
    tableColumns: list[TableColumnProperties]


class ImageOut(Model):
    contentUrl: str
    sourceUrl: str


class PageElement(Model):
    objectId: str
    size: Size | None = None
    transform: AffineTransform
    shape: ShapeOut | None = None
    table: TableOut | None = None
    image: ImageOut | None = None


class NotesProperties(Model):
    speakerNotesObjectId: str


class NotesPage(Model):
    objectId: str
    pageType: Literal["NOTES"] = "NOTES"
    pageElements: list[PageElement]
    notesProperties: NotesProperties


class SlideProperties(Model):
    layoutObjectId: str
    masterObjectId: str = MASTER_ID
    notesPage: NotesPage


class LayoutProperties(Model):
    masterObjectId: str = MASTER_ID
    name: str
    displayName: str


class Page(Model):
    objectId: str
    pageType: Literal["SLIDE", "LAYOUT", "MASTER", "NOTES_MASTER"]
    pageElements: list[PageElement] = []
    slideProperties: SlideProperties | None = None
    layoutProperties: LayoutProperties | None = None
    pageProperties: PageProperties = PageProperties()
    revisionId: str | None = None


class Presentation(Model):
    presentationId: str
    title: str
    locale: str = "en"
    pageSize: Size = Size(
        width=Dimension(magnitude=SLIDE_WIDTH, unit="EMU"), height=Dimension(magnitude=SLIDE_HEIGHT, unit="EMU")
    )
    slides: list[Page]
    masters: list[Page]
    layouts: list[Page]
    notesMaster: Page
    revisionId: str


def _content(paragraphs: list[Paragraph]) -> TextContent | None:
    if not paragraphs:
        return None
    elements: list[TextElement] = []
    at = 0
    for paragraph in paragraphs:
        size = sum(_utf16(r.text) for r in paragraph.runs) + 1
        elements.append(
            TextElement(
                startIndex=at or None, endIndex=at + size, paragraphMarker=ParagraphMarker(style=paragraph.style)
            )
        )
        runs = list(paragraph.runs)
        for i, run in enumerate(runs):
            content = run.text + ("\n" if i == len(runs) - 1 and run.style == paragraph.newline else "")
            end = at + _utf16(content)
            elements.append(
                TextElement(startIndex=at or None, endIndex=end, textRun=TextRunOut(content=content, style=run.style))
            )
            at = end
        if not runs or runs[-1].style != paragraph.newline:
            elements.append(
                TextElement(
                    startIndex=at or None, endIndex=at + 1, textRun=TextRunOut(content="\n", style=paragraph.newline)
                )
            )
            at += 1
    return TextContent(textElements=elements)


def _element_out(element: Element) -> PageElement:
    content = element.content
    if isinstance(content, StoredShape):
        shape = ShapeOut(
            shapeType=content.shape_type,
            text=_content(content.text),
            shapeProperties=content.properties,
            placeholder=content.placeholder,
        )
        return PageElement(objectId=element.object_id, size=element.size, transform=element.transform, shape=shape)
    if isinstance(content, StoredTable):
        rows = [
            TableRowOut(
                tableCells=[
                    TableCellOut(
                        location=CellLocationOut(rowIndex=r or None, columnIndex=c or None),
                        text=_content(cell.text),
                        tableCellProperties=cell.properties,
                    )
                    for c, cell in enumerate(row)
                ]
            )
            for r, row in enumerate(content.rows)
        ]
        table = TableOut(
            rows=len(content.rows), columns=len(content.columns), tableRows=rows, tableColumns=content.columns
        )
        return PageElement(objectId=element.object_id, size=element.size, transform=element.transform, table=table)
    image = ImageOut(contentUrl=f"https://lh3.googleusercontent.com/sim/{element.object_id}", sourceUrl=content.source)
    return PageElement(objectId=element.object_id, size=element.size, transform=element.transform, image=image)


def render(presentation_id: str, title: str, revision_id: str, deck: Deck) -> Presentation:
    slides: list[Page] = []
    for slide in deck.slides:
        notes_shape = PageElement(
            objectId=slide.notes_shape_id,
            transform=AffineTransform(scaleX=1, scaleY=1, unit="EMU"),
            shape=ShapeOut(
                shapeType="TEXT_BOX", text=_content(slide.notes), placeholder=Placeholder(type="BODY", index=1)
            ),
        )
        notes = NotesPage(
            objectId=slide.notes_id,
            pageElements=[notes_shape],
            notesProperties=NotesProperties(speakerNotesObjectId=slide.notes_shape_id),
        )
        slides.append(
            Page(
                objectId=slide.object_id,
                pageType="SLIDE",
                pageElements=[_element_out(e) for e in slide.elements],
                slideProperties=SlideProperties(layoutObjectId=layout_id(slide.layout), notesPage=notes),
                pageProperties=slide.properties,
                revisionId=revision_id,
            )
        )
    layouts = [
        Page(
            objectId=layout_id(name),
            pageType="LAYOUT",
            layoutProperties=LayoutProperties(name=name, displayName=name.replace("_", " ").title()),
        )
        for name in LAYOUTS
    ]
    return Presentation(
        presentationId=presentation_id,
        title=title,
        slides=slides,
        masters=[Page(objectId=MASTER_ID, pageType="MASTER")],
        layouts=layouts,
        notesMaster=Page(objectId=NOTES_MASTER_ID, pageType="NOTES_MASTER"),
        revisionId=revision_id,
    )
