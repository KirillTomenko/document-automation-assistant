"""Prepared extraction inputs; no extraction, classification or filesystem access."""

from typing import Annotated

from pydantic import Field, model_validator

from document_automation.contracts import (
    ClassificationResult,
    ContractModel,
    SourceFragment,
    Text,
)
from document_automation.parsing import ParsedDocument


class RawCandidate(ContractModel):
    raw_value: Text
    source: SourceFragment | None = None


class RawFields(ContractModel):
    customer_name: tuple[RawCandidate, ...] = ()
    request_title: tuple[RawCandidate, ...] = ()
    work_description: tuple[RawCandidate, ...] = ()
    requested_completion_date: tuple[RawCandidate, ...] = ()
    contact_email: tuple[RawCandidate, ...] = ()


class RawItem(ContractModel):
    document_id: Text
    row: Annotated[int, Field(ge=2)]
    item_code: tuple[RawCandidate, ...] = ()
    name: tuple[RawCandidate, ...] = ()
    quantity: tuple[RawCandidate, ...] = ()
    unit: tuple[RawCandidate, ...] = ()


class RegisteredSource(ContractModel):
    """Application registration survives even when parsing cannot produce a hash."""

    run_id: Text
    source_id: Text
    parsed: ParsedDocument


class WorkRequestInput(ContractModel):
    run_id: Text
    sources: tuple[RegisteredSource, ...]
    classifications: tuple[ClassificationResult, ...] = ()
    fields: RawFields = RawFields()
    items: tuple[RawItem, ...] = ()

    @model_validator(mode="after")
    def unique_inputs(self):
        keys = [(s.run_id, s.source_id) for s in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate source registration")
        rows = [(i.document_id, i.row) for i in self.items]
        if len(rows) != len(set(rows)):
            raise ValueError("duplicate prepared row")
        return self
