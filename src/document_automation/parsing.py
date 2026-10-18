"""Technical parsing contracts, independent of the demo workflow."""

from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from document_automation.contracts import (
    ContractModel,
    InputDocument,
    Location,
    SourceFragment,
    Text,
)

PositiveInt = Annotated[int, Field(gt=0)]


class ParserLimits(ContractModel):
    max_file_bytes: PositiveInt = 10 * 1024 * 1024
    max_pdf_pages: PositiveInt = 100
    max_xlsx_sheets: PositiveInt = 20
    max_xlsx_cells: PositiveInt = 100_000
    max_zip_bytes: PositiveInt = 50 * 1024 * 1024
    max_zip_members: PositiveInt = 2_000
    max_blocks: PositiveInt = 100_000
    max_table_depth: PositiveInt = 8
    max_text_chars: PositiveInt = 5_000_000
    max_pdf_stream_bytes: PositiveInt = 10 * 1024 * 1024


class ParseDiagnostic(ContractModel):
    code: Literal[
        "empty_file",
        "empty_content",
        "no_text",
        "file_read_error",
        "encrypted_file",
        "unsupported_format",
        "unsupported_content",
        "resource_limit",
    ]
    severity: Literal["warning", "error"]
    message: Text
    location: Location | None = None


class TextValue(ContractModel):
    kind: Literal["string", "formula", "error"]
    value: str


class IntegerValue(ContractModel):
    kind: Literal["integer"] = "integer"
    value: int


class NumberValue(ContractModel):
    kind: Literal["number"] = "number"
    value: float


class BooleanValue(ContractModel):
    kind: Literal["boolean"] = "boolean"
    value: bool


class DateValue(ContractModel):
    kind: Literal["date"] = "date"
    value: date


class DateTimeValue(ContractModel):
    kind: Literal["datetime"] = "datetime"
    value: datetime


class TimeValue(ContractModel):
    kind: Literal["time"] = "time"
    value: time


class DurationValue(ContractModel):
    kind: Literal["duration"] = "duration"
    value: timedelta


class EmptyValue(ContractModel):
    kind: Literal["empty"] = "empty"
    value: None = None


CellValue = Annotated[
    TextValue
    | IntegerValue
    | NumberValue
    | BooleanValue
    | DateValue
    | DateTimeValue
    | TimeValue
    | DurationValue
    | EmptyValue,
    Field(discriminator="kind"),
]


class ParsedCell(ContractModel):
    location: Location
    value: CellValue = EmptyValue()
    number_format: str | None = None
    stored_value: str | None = None
    storage_type: str | None = None
    fragment_id: Text | None = None
    usable: bool = True
    merged_range: str | None = None
    blocks: tuple["ParsedBlock", ...] = ()

    @model_validator(mode="after")
    def supported_value(self):
        if (
            self.merged_range or self.value.kind in ("formula", "error")
        ) and self.usable:
            raise ValueError("unsupported cell cannot be usable")
        if not self.usable and self.fragment_id is not None:
            raise ValueError("unsupported cell cannot be extraction evidence")
        return self


class ParsedRow(ContractModel):
    index: PositiveInt
    cells: tuple[ParsedCell, ...]
    hidden: bool = False


class ParsedTable(ContractModel):
    table_id: Text
    rows: tuple[ParsedRow, ...]


class ParsedBlock(ContractModel):
    kind: Literal["page", "paragraph", "table"]
    location: Location
    text: str = ""
    fragment_id: Text | None = None
    table: ParsedTable | None = None

    @model_validator(mode="after")
    def block_shape(self):
        if (self.kind == "table") != (self.table is not None):
            raise ValueError(
                "table blocks require a table and other blocks cannot have one"
            )
        return self


class ParsedSheet(ContractModel):
    name: Text
    state: Literal["visible", "hidden", "veryHidden"]
    table: ParsedTable


class ParsedDocument(ContractModel):
    filename: Text
    document: InputDocument | None
    status: Literal["completed", "failed"]
    fragments: tuple[SourceFragment, ...] = ()
    blocks: tuple[ParsedBlock, ...] = ()
    sheets: tuple[ParsedSheet, ...] = ()
    diagnostics: tuple[ParseDiagnostic, ...] = ()

    @model_validator(mode="after")
    def integrity(self):
        errors = any(d.severity == "error" for d in self.diagnostics)
        if (self.status == "failed") != errors:
            raise ValueError("failed requires an error; completed cannot hide errors")
        if self.status == "failed":
            if self.fragments or self.blocks or self.sheets:
                raise ValueError(
                    "failed parse cannot expose partial data as a complete result"
                )
            return self
        if self.document is None:
            raise ValueError("completed parse requires InputDocument")
        ids = {f.fragment_id: f for f in self.fragments}
        if len(ids) != len(self.fragments):
            raise ValueError("duplicate fragment IDs")
        for f in self.fragments:
            if (
                f.document_id != self.document.document_id
                or f.location.format != self.document.format
            ):
                raise ValueError("foreign fragment")
        referenced = set()

        def check(fragment_id, location, text=None):
            if location.format != self.document.format:
                raise ValueError("foreign location format")
            if fragment_id is not None:
                f = ids.get(fragment_id)
                if (
                    f is None
                    or f.location != location
                    or (text is not None and f.text != text)
                ):
                    raise ValueError("invalid fragment reference")
                referenced.add(fragment_id)

        def table(t):
            for row in t.rows:
                for cell in row.cells:
                    check(cell.fragment_id, cell.location)
                    blocks(cell.blocks)

        def blocks(items):
            for block in items:
                check(block.fragment_id, block.location, block.text)
                if block.table is not None:
                    table(block.table)

        blocks(self.blocks)
        for sheet in self.sheets:
            table(sheet.table)
        if referenced != ids.keys():
            raise ValueError("orphan fragment")
        return self


ParsedCell.model_rebuild()
