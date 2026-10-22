"""Bounded extraction boundary shared by offline demo and one provider adapter."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from document_automation.adapters.parsers import parse_document
from document_automation.contracts import (
    ClassificationResult,
    ContractModel,
    DocumentType,
    Text,
)
from document_automation.metering import automatic_attempts, extract_attempt
from document_automation.operations import audit_operation
from document_automation.schemas import AuditEvent, ProcessingResult
from document_automation.workflows.work_request import WorkItem, demo_contract
from document_automation.workflows.work_request_context import (
    ALIASES,
    _cells,
    _context_matches,
    _description_sections,
    is_summary_row,
    item_layout,
    label_values,
    type_evidence,
)
from document_automation.workflows.work_request_inputs import (
    RawCandidate,
    RawFields,
    RawItem,
    RegisteredSource,
    WorkRequestInput,
)
from document_automation.workflows.work_request_processing import process_work_request


class ExtractionLimits(ContractModel):
    max_output_tokens: int = Field(default=8000, ge=100, le=16000)
    max_input_bytes: int = Field(default=150_000, ge=1, le=1_000_000)
    max_output_bytes: int = Field(default=200_000, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    total_seconds: float = Field(default=65.0, gt=0, le=300)
    attempts: int = Field(default=2, ge=1, le=3)


class TypeClaim(ContractModel):
    document_id: Text
    document_type: DocumentType
    evidence_ids: tuple[Text, ...] = Field(max_length=100)


class CandidateClaim(ContractModel):
    field: Literal[
        "customer_name",
        "request_title",
        "work_description",
        "requested_completion_date",
        "contact_email",
        "item_code",
        "name",
        "quantity",
        "unit",
    ]
    document_id: Text
    fragment_id: Text
    raw_value: Text
    row: int | None

    @model_validator(mode="after")
    def row_scope(self):
        if self.field in WorkItem.model_fields:
            if self.row is None or not 2 <= self.row <= 100_000:
                raise ValueError("item candidate requires source row")
        elif self.row is not None:
            raise ValueError("scalar candidate cannot have row")
        return self


class Extraction(ContractModel):
    classifications: tuple[TypeClaim, ...] = Field(max_length=10)
    candidates: tuple[CandidateClaim, ...] = Field(max_length=2000)


class ExtractionError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class Extractor(Protocol):
    name: str

    async def extract(self, payload: str, limits: ExtractionLimits) -> str: ...


class DemoExtractor:
    """Deterministic extraction for the documented layout, never an LLM simulation."""

    name = "demo-rules-v1"
    is_llm = False

    async def extract(self, payload, limits):
        from document_automation.parsing import ParsedDocument

        types, candidates = [], []
        for raw in json.loads(payload)["documents"]:
            p = ParsedDocument.model_validate_json(json.dumps(raw))
            fragments = {f.fragment_id: f for f in p.fragments}
            kind, evidence = type_evidence(p)
            types.append(
                dict(
                    document_id=p.document.document_id,
                    document_type=kind,
                    evidence_ids=evidence,
                )
            )

            def add(field, f, value, row=None):
                if value.strip():
                    candidates.append(
                        dict(
                            field=field,
                            document_id=f.document_id,
                            fragment_id=f.fragment_id,
                            raw_value=value,
                            row=row,
                        )
                    )

            if kind in ("customer_card", "work_description"):
                for f in p.fragments:
                    for field in ALIASES:
                        if kind not in demo_contract()["fields"][field]["sources"]:
                            continue
                        for value in label_values(f.text, field, f.location.format):
                            add(field, f, value.strip())
                if kind == "work_description":
                    for section in _description_sections(p):
                        for block in section:
                            if block.fragment_id:
                                add(
                                    "work_description",
                                    fragments[block.fragment_id],
                                    block.text,
                                )
            elif kind == "work_items":
                layout = item_layout(p)
                cells = _cells(p)
                summary_rows = {
                    r.index
                    for s in p.sheets
                    for r in s.table.rows
                    if layout and s.name == layout[0] and is_summary_row(r, layout[2])
                }
                for f in p.fragments:
                    candidate = RawCandidate(raw_value=f.text, source=f)
                    if _context_matches(
                        "requested_completion_date", candidate, p, cells=cells
                    )[0]:
                        add("requested_completion_date", f, f.text)
                    if layout and f.location.sheet == layout[0]:
                        from openpyxl.utils.cell import coordinate_from_string

                        column, row = coordinate_from_string(f.location.cell)
                        if row > layout[1] and row not in summary_rows:
                            for field, col in layout[2].items():
                                if col == column:
                                    add(field, f, f.text, row)
        return json.dumps(
            dict(classifications=types, candidates=candidates), ensure_ascii=False
        )


def _event(run_id, event, **kwargs):
    return AuditEvent(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc),
        stage="extraction",
        event=event,
        message=f"extraction: {event}",
        **kwargs,
    )


async def process_paths(
    paths: tuple[Path, ...],
    run_id: str,
    extractor: Extractor | None = None,
    limits: ExtractionLimits | None = None,
    *,
    contract_version: str = "1.0",
    attempt_journal=None,
) -> ProcessingResult:
    """Caller supplies only registered local paths; no paths are accepted from model output."""
    if contract_version == "2.0":
        from document_automation.extraction_v2 import process_paths_v2

        return await process_paths_v2(paths, run_id, extractor, limits, attempt_journal)
    if contract_version != "1.0":
        raise ValueError("Unsupported work_request contract version")
    extractor, limits = extractor or DemoExtractor(), limits or ExtractionLimits()
    if not 1 <= len(paths) <= 10:
        raise ValueError("Upload between 1 and 10 files")
    events, sources = [], []
    registrations = tuple((run_id, str(i)) for i in range(len(paths)))
    for i, path in enumerate(paths):
        decision, event = audit_operation(
            "read_source",
            run_id=run_id,
            source_id=str(i),
            registered_sources=registrations,
        )
        events.append(event)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        sources.append(
            RegisteredSource(
                run_id=run_id, source_id=str(i), parsed=parse_document(path)
            )
        )
    initial = WorkRequestInput(run_id=run_id, sources=tuple(sources))
    if any(s.parsed.status == "failed" for s in sources):
        result, _ = process_work_request(initial)
        return result
    docs = {s.parsed.document.document_id: s.parsed for s in sources}
    # Filenames are not classification evidence and never sent to the provider.
    payload_docs = []
    for p in docs.values():
        raw = p.model_dump(mode="json")
        raw["filename"] = "input." + p.document.format
        raw["document"]["filename"] = raw["filename"]
        payload_docs.append(raw)
    payload = json.dumps({"documents": payload_docs}, ensure_ascii=False)
    events.append(_event(run_id, "started"))
    try:
        if getattr(extractor, "contract_version", "1.0") != "1.0":
            raise ExtractionError("extraction_contract_mismatch")
        if len(payload.encode()) > limits.max_input_bytes:
            raise ExtractionError("extraction_input_limit")
        async with asyncio.timeout(limits.total_seconds):
            attempts = automatic_attempts(extractor, limits)
            for attempt in range(attempts):
                try:
                    events.append(_event(run_id, "model_used", model=extractor.name))
                    extracted = await extract_attempt(
                        extractor,
                        payload,
                        limits,
                        Extraction,
                        attempt_journal,
                        attempt + 1,
                    )
                    break
                except (ValidationError, TimeoutError) as exc:
                    if attempt + 1 == attempts:
                        raise ExtractionError(
                            "extraction_invalid_output"
                            if isinstance(exc, ValidationError)
                            else "extraction_timeout"
                        ) from None
                except ExtractionError as exc:
                    if not exc.retryable or attempt + 1 == attempts:
                        raise
        fragments = {
            (f.document_id, f.fragment_id): f
            for p in docs.values()
            for f in p.fragments
        }
        types = []
        seen = set()
        for claim in extracted.classifications:
            if claim.document_id not in docs or claim.document_id in seen:
                raise ExtractionError("extraction_invalid_references")
            seen.add(claim.document_id)
            refs = tuple(
                fragments.get((claim.document_id, fid)) for fid in claim.evidence_ids
            )
            if claim.document_type != "unknown" and (
                not refs or any(f is None for f in refs)
            ):
                raise ExtractionError("extraction_invalid_references")
            types.append(
                ClassificationResult(
                    document_id=claim.document_id,
                    document_type=claim.document_type,
                    evidence=tuple(f for f in refs if f),
                )
            )
        if seen != docs.keys():
            raise ExtractionError("extraction_incomplete_classification")
        fields = {name: [] for name in RawFields.model_fields}
        rows = {}
        for claim in extracted.candidates:
            c = RawCandidate(
                raw_value=claim.raw_value,
                source=fragments.get((claim.document_id, claim.fragment_id)),
            )
            if claim.row is None:
                fields[claim.field].append(c)
            else:
                row = rows.setdefault(
                    (claim.document_id, claim.row),
                    {name: [] for name in WorkItem.model_fields},
                )
                row[claim.field].append(c)
        prepared = WorkRequestInput(
            run_id=run_id,
            sources=tuple(sources),
            classifications=tuple(types),
            fields=RawFields(**{k: tuple(v) for k, v in fields.items()}),
            items=tuple(
                RawItem(
                    document_id=doc, row=row, **{k: tuple(v) for k, v in values.items()}
                )
                for (doc, row), values in rows.items()
            ),
        )
        result, _ = process_work_request(prepared)
        events.append(_event(run_id, "completed"))
        return ProcessingResult.model_validate(
            {**result.model_dump(), "audit": tuple(events) + result.audit}
        )
    except (ExtractionError, TimeoutError) as exc:
        code = exc.code if isinstance(exc, ExtractionError) else "extraction_timeout"
        if attempt_journal:
            attempt_journal.reject_last(code)
        events.append(_event(run_id, "failed", error_code=code))
        return ProcessingResult(
            run_id=run_id,
            processing_status="failed",
            documents=tuple(p.document for p in docs.values()),
            classifications=(),
            data=None,
            issues=(),
            needs_review=False,
            audit=tuple(events),
        )
