"""Versioned bounded extraction; remote retry policy is unchanged in this increment."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from document_automation.adapters.parsers import parse_document
from document_automation.contracts import ContractModel, Text
from document_automation.extraction import ExtractionError, ExtractionLimits, TypeClaim
from document_automation.metering import automatic_attempts, extract_attempt
from document_automation.operations import audit_operation
from document_automation.schemas import AuditEvent, ProcessingResult
from document_automation.workflows.work_request import WorkItem
from document_automation.workflows.work_request_v2_context import inspect_document
from document_automation.workflows.work_request_v2_processing import process_v2


class CandidateClaimV2(ContractModel):
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
    row_id: Text | None

    @model_validator(mode="after")
    def row_scope(self):
        if (self.field in WorkItem.model_fields) != (self.row_id is not None):
            raise ValueError(
                "v2 items require application row_id; scalars require null"
            )
        return self


class ExtractionV2(ContractModel):
    classifications: tuple[TypeClaim, ...] = Field(max_length=30)
    candidates: tuple[CandidateClaimV2, ...] = Field(max_length=2000)


def literal_claims(views):
    types, candidates = [], []
    for view in views:
        for c in view.classifications:
            types.append(
                dict(
                    document_id=c.document_id,
                    document_type=c.document_type,
                    evidence_ids=[f.fragment_id for f in c.evidence],
                )
            )
        for value in view.values:
            candidates.append(
                dict(
                    field=value.field,
                    document_id=value.source.document_id,
                    fragment_id=value.source.fragment_id,
                    raw_value=value.raw_value,
                    row_id=None,
                )
            )
        for row in view.rows:
            for value in row.values:
                candidates.append(
                    dict(
                        field=value.field,
                        document_id=value.source.document_id,
                        fragment_id=value.source.fragment_id,
                        raw_value=value.raw_value,
                        row_id=row.row_id,
                    )
                )
    return dict(classifications=types, candidates=candidates)


class DemoExtractorV2:
    name = "demo-rules-v2"
    is_llm = False

    async def extract(self, payload, limits):
        # App-owned finite structures, not an LLM fallback or usage simulation.
        return json.dumps(json.loads(payload)["supported_claims"], ensure_ascii=False)


async def process_paths_v2(
    paths, run_id, extractor=None, limits=None, attempt_journal=None
):
    extractor, limits = extractor or DemoExtractorV2(), limits or ExtractionLimits()
    if not 1 <= len(paths) <= 10:
        raise ValueError("Upload between 1 and 10 files")
    events, parsed, views = [], [], []

    def event(stage, kind, **kwargs):
        events.append(
            AuditEvent(
                run_id=run_id,
                timestamp=datetime.now(timezone.utc),
                stage=stage,
                event=kind,
                message=f"{stage}: {kind}",
                **kwargs,
            )
        )

    def failed(code, stage="extraction"):
        event(stage, "failed", error_code=code)
        return ProcessingResult(
            run_id=run_id,
            contract_version="2.0",
            processing_status="failed",
            documents=tuple(
                {
                    p.document.document_id: p.document for p in parsed if p.document
                }.values()
            ),
            classifications=(),
            data=None,
            issues=(),
            needs_review=False,
            audit=tuple(events),
        )

    registrations = tuple((run_id, str(i)) for i in range(len(paths)))
    for i, path in enumerate(paths):
        decision, audit = audit_operation(
            "read_source",
            run_id=run_id,
            source_id=str(i),
            registered_sources=registrations,
        )
        events.append(audit)
        if not decision.allowed:
            return failed("source_not_registered", "ingestion")
        p = parse_document(path)
        parsed.append(p)
        if p.status == "failed":
            return failed(
                next(d.code for d in p.diagnostics if d.severity == "error"), "parsing"
            )
    documents = {}
    for p in sorted(parsed, key=lambda p: (p.document.sha256, p.filename)):
        documents.setdefault(p.document.document_id, p)
    views = [inspect_document(p) for p in documents.values()]
    payload_docs = []
    for view in views:
        raw = view.parsed.model_dump(mode="json")
        raw["filename"] = "input." + view.parsed.document.format
        raw["document"]["filename"] = raw["filename"]
        payload_docs.append(raw)
    payload = json.dumps(
        dict(
            contract_version="2.0",
            documents=payload_docs,
            supported_claims=literal_claims(views),
        ),
        ensure_ascii=False,
    )
    event("extraction", "started")
    try:
        if getattr(extractor, "contract_version", "2.0") != "2.0":
            raise ExtractionError("extraction_contract_mismatch")
        if len(payload.encode()) > limits.max_input_bytes:
            raise ExtractionError("extraction_input_limit")
        async with asyncio.timeout(limits.total_seconds):
            attempts = automatic_attempts(extractor, limits)
            for attempt in range(attempts):
                try:
                    event("extraction", "model_used", model=extractor.name)
                    extracted = await extract_attempt(
                        extractor,
                        payload,
                        limits,
                        ExtractionV2,
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
        keys = [(c.document_id, c.document_type) for c in extracted.classifications]
        if len(set(keys)) != len(keys) or any(
            c.document_id not in documents for c in extracted.classifications
        ):
            raise ExtractionError("extraction_invalid_references")
        if {c.document_id for c in extracted.classifications} != set(documents):
            raise ExtractionError("extraction_incomplete_classification")

        # An omitted conflicting source must never turn into a clean selected value.
        def candidate_key(c):
            return (
                c["field"],
                c["document_id"],
                c["fragment_id"],
                c["raw_value"],
                c["row_id"],
            )

        actual = [candidate_key(c.model_dump()) for c in extracted.candidates]
        expected = {candidate_key(c) for c in literal_claims(views)["candidates"]}
        if len(set(actual)) != len(actual):
            raise ExtractionError("extraction_invalid_references")
        if not expected <= set(actual):
            raise ExtractionError("extraction_incomplete_candidates")
        event("extraction", "completed")
        return process_v2(
            run_id, views, extracted, events, duplicate=len(parsed) != len(documents)
        )
    except (ExtractionError, TimeoutError) as exc:
        if attempt_journal:
            attempt_journal.reject_last(
                exc.code if isinstance(exc, ExtractionError) else "extraction_timeout"
            )
        return failed(
            exc.code if isinstance(exc, ExtractionError) else "extraction_timeout"
        )
