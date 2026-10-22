"""Provider-independent primitives. Validators check supplied data, never extract it."""

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DocumentType = Literal["customer_card", "work_description", "work_items", "unknown"]
FieldState = Literal["found", "missing", "ambiguous", "conflict"]
ProcessingStatus = Literal["completed", "failed"]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, allow_inf_nan=False
    )


class InputDocument(ContractModel):
    document_id: Text
    filename: Text
    format: Literal["pdf", "docx", "xlsx"]
    sha256: Sha256


class PdfLocation(ContractModel):
    format: Literal["pdf"] = "pdf"
    page: Annotated[int, Field(ge=1)] | None = None
    line: Annotated[int, Field(ge=1)] | None = None
    column: Annotated[int, Field(ge=1)] | None = None

    @model_serializer(mode="wrap")
    def compatible_location(self, handler):
        data = handler(self)
        for key in ("line", "column"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def text_coordinates(self):
        if self.line is not None and self.page is None:
            raise ValueError("PDF line requires page")
        if self.column is not None and self.line is None:
            raise ValueError("PDF column requires line")
        return self


class DocxLocation(ContractModel):
    format: Literal["docx"] = "docx"
    paragraph: Annotated[int, Field(ge=1)] | None = None
    section: Text | None = None
    table: Annotated[int, Field(ge=1)] | None = None
    row: Annotated[int, Field(ge=1)] | None = None
    column: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def table_coordinates(self):
        if (self.row is not None or self.column is not None) and self.table is None:
            raise ValueError("row/column require a table")
        return self


class XlsxLocation(ContractModel):
    format: Literal["xlsx"] = "xlsx"
    sheet: Text
    cell: Annotated[str, Field(pattern=r"^[A-Z]+[1-9][0-9]*$")]


Location = Annotated[
    PdfLocation | DocxLocation | XlsxLocation, Field(discriminator="format")
]


class SourceFragment(ContractModel):
    fragment_id: Text
    document_id: Text
    text: Text
    location: Location


class ClassificationResult(ContractModel):
    document_id: Text
    document_type: DocumentType
    evidence: tuple[SourceFragment, ...]

    @model_validator(mode="after")
    def classification_evidence(self):
        if self.document_type != "unknown" and not self.evidence:
            raise ValueError("known classification requires evidence")
        if any(s.document_id != self.document_id for s in self.evidence):
            raise ValueError("classification evidence belongs to another document")
        return self


T = TypeVar("T")


class FieldCandidate(ContractModel, Generic[T]):
    """Interpretations: one definite value, several alternatives, or none if unparseable.

    verified records an upstream assertion, not proof that the original file was read.
    An unverified candidate may retain a claimed fragment for diagnostics.
    """

    raw_value: Text
    interpretations: tuple[T, ...]
    verified: bool
    source: SourceFragment | None

    @model_validator(mode="after")
    def provenance(self):
        if self.verified:
            if self.source is None:
                raise ValueError("verified candidate requires provenance")
            if self.raw_value not in self.source.text:
                raise ValueError("raw value must occur in the verified fragment")
        if len(set(self.interpretations)) != len(self.interpretations):
            raise ValueError("interpretations must be distinct")
        return self


class FieldResult(ContractModel, Generic[T]):
    state: FieldState
    value: T | None
    candidates: tuple[FieldCandidate[T], ...]
    ambiguity_reason: Literal["duplicate_item_code"] | None = None

    def comparison_key(self, value):
        return value

    @model_validator(mode="after")
    def state_consistency(self):
        confirmed = [c for c in self.candidates if c.verified]
        definite = [
            c.interpretations[0] for c in confirmed if len(c.interpretations) == 1
        ]
        distinct = {self.comparison_key(value) for value in definite}
        uncertain = any(len(c.interpretations) != 1 for c in confirmed)
        if self.state != "found" and self.value is not None:
            raise ValueError("only found may have a selected value")
        if self.ambiguity_reason and self.state != "ambiguous":
            raise ValueError("ambiguity reason requires ambiguous state")
        if self.state == "found":
            if (
                self.value is None
                or self.value not in definite
                or distinct != {self.comparison_key(self.value)}
                or uncertain
            ):
                raise ValueError("found requires equal, definite, verified candidates")
        elif self.state == "missing":
            if confirmed:
                raise ValueError("missing cannot discard confirmed evidence")
        elif self.state == "conflict":
            if len(distinct) < 2:
                raise ValueError("conflict requires distinct definite verified values")
        elif (
            not confirmed
            or len(distinct) > 1
            or not (uncertain or self.ambiguity_reason)
        ):
            raise ValueError(
                "ambiguous requires unresolved evidence, without a proven conflict"
            )
        return self


IssueCode = Literal[
    "required_missing",
    "ambiguous_value",
    "conflicting_values",
    "invalid_value",
    "unverified_source",
    "unsupported_content",
    "missing_document",
    "unknown_document",
    "duplicate_document",
]


class ValidationIssue(ContractModel):
    code: IssueCode
    path: Text
    message: Text
    sources: tuple[SourceFragment, ...] = ()
