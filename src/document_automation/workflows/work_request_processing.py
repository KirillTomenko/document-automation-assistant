"""Deterministic stage 3A over application-owned parsed inputs and prepared candidates."""

from datetime import date, datetime, timezone
from decimal import Decimal

from document_automation.contracts import (
    ClassificationResult,
    FieldCandidate,
    FieldResult,
    Text,
    ValidationIssue,
)
from document_automation.operations import audit_operation
from document_automation.schemas import AuditEvent, ProcessingResult
from document_automation.workflows.work_request import (
    CustomerNameResult,
    WorkItem,
    WorkRequestData,
    demo_contract,
)
from document_automation.workflows.work_request_context import (
    _cells,
    _context_matches,
    _description_sections,
    is_summary_row,
    item_layout,
    type_evidence,
)
from document_automation.workflows.work_request_inputs import RawItem, WorkRequestInput
from document_automation.workflows.work_request_normalization import normalize


def process_work_request(
    inputs: WorkRequestInput,
) -> tuple[ProcessingResult, tuple[AuditEvent, ...]]:
    """No reads/writes/network. Returned audit is also contained in ProcessingResult.

    ParsedDocument and registrations are trusted application products, never model output.
    Classification and raw candidates are claims and are checked against those products.
    """
    contract = demo_contract()
    audit = []
    issues = []

    def event(stage, kind, **kwargs):
        audit.append(
            AuditEvent(
                run_id=inputs.run_id,
                timestamp=datetime.now(timezone.utc),
                stage=stage,
                event=kind,
                message=f"{stage}: {kind}",
                **kwargs,
            )
        )

    def issue(code, path, sources=()):
        # Do not put rejected or fabricated references in trusted issue.sources.
        for index, previous in enumerate(issues):
            if (previous.code, previous.path) == (code, path):
                merged = tuple(dict.fromkeys(previous.sources + tuple(sources)))
                issues[index] = ValidationIssue(
                    code=code, path=path, message=previous.message, sources=merged
                )
                return
        issues.append(
            ValidationIssue(
                code=code, path=path, message=f"{code}: {path}", sources=tuple(sources)
            )
        )

    documents = {}
    for registered in inputs.sources:
        if (
            registered.run_id == inputs.run_id
            and registered.parsed.document is not None
        ):
            documents.setdefault(
                registered.parsed.document.document_id, registered.parsed.document
            )

    def finish(status, data=None, classifications=()):
        result = ProcessingResult(
            run_id=inputs.run_id,
            processing_status=status,
            documents=tuple(documents.values()),
            classifications=tuple(classifications),
            data=data,
            issues=tuple(issues),
            needs_review=bool(issues),
            audit=tuple(audit),
        )
        _, decision_event = audit_operation(
            "create_draft", run_id=inputs.run_id, result=result
        )
        audit.append(decision_event)
        result = ProcessingResult(**{**result.model_dump(), "audit": tuple(audit)})
        return result, result.audit

    event("validation", "started")
    registrations = tuple((s.run_id, s.source_id) for s in inputs.sources)
    for registered in inputs.sources:
        decision, decision_event = audit_operation(
            "read_source",
            run_id=inputs.run_id,
            registered_sources=registrations,
            source_id=registered.source_id,
        )
        # A foreign registration with a reused source_id cannot borrow a current one.
        if registered.run_id != inputs.run_id:
            decision, decision_event = audit_operation(
                "read_source", run_id=inputs.run_id, source_id=registered.source_id
            )
        audit.append(decision_event)
        if not decision.allowed:
            event("ingestion", "failed", error_code="source_not_registered")
            return finish("failed")
        if registered.parsed.status == "failed":
            error = next(
                d for d in registered.parsed.diagnostics if d.severity == "error"
            )
            event("parsing", "failed", error_code=error.code)
            return finish("failed")
    parsed = {}
    multiplicity = {}
    for registered in inputs.sources:
        p = registered.parsed
        doc_id = p.document.document_id
        multiplicity[doc_id] = multiplicity.get(doc_id, 0) + 1
        if doc_id in parsed:
            previous = parsed[doc_id]
            if (
                p.document.sha256 != previous.document.sha256
                or p.document.format != previous.document.format
                or p.model_dump(exclude={"filename", "document"})
                != previous.model_dump(exclude={"filename", "document"})
            ):
                event("ingestion", "failed", error_code="inconsistent_source_identity")
                return finish("failed")
            issue("duplicate_document", "documents")
        else:
            parsed[doc_id] = p
    fragments = {
        (f.document_id, f.fragment_id): f for p in parsed.values() for f in p.fragments
    }
    cell_maps = {
        doc_id: _cells(p) for doc_id, p in parsed.items() if p.document.format == "xlsx"
    }

    def actual(source):
        return (
            source is not None
            and fragments.get((source.document_id, source.fragment_id)) == source
        )

    classifications = []
    for doc_id, doc in documents.items():
        claims = [c for c in inputs.classifications if c.document_id == doc_id]
        claim = claims[0] if len(claims) == 1 else None
        valid = (
            claim is not None
            and claim.document_type != "unknown"
            and contract["document_types"][claim.document_type] == doc.format
            and all(actual(f) for f in claim.evidence)
            and type_evidence(parsed[doc_id])[0] == claim.document_type
            and bool(
                set(type_evidence(parsed[doc_id])[1])
                & {f.fragment_id for f in claim.evidence}
            )
        )
        if valid:
            classifications.append(claim)
        else:
            classifications.append(
                ClassificationResult(
                    document_id=doc_id, document_type="unknown", evidence=()
                )
            )
            issue("unknown_document", "documents")
            if claim is not None and claim.document_type != "unknown":
                issue("unverified_source", "documents")
    if any(c.document_id not in documents for c in inputs.classifications):
        issue("unverified_source", "documents")
    types = {c.document_id: c.document_type for c in classifications}
    for kind in contract["document_types"]:
        count = sum(
            multiplicity[c.document_id]
            for c in classifications
            if c.document_type == kind
        )
        if not count:
            issue("missing_document", "documents")
        if count > 1:
            issue("duplicate_document", "documents")
    for doc_id, p in parsed.items():
        for diagnostic in p.diagnostics:
            if diagnostic.code in ("unsupported_content", "no_text"):
                issue("unsupported_content", "documents")

    def field_result(name, raw_candidates, path, row=None, owner=None):
        rule = contract["item_fields" if row is not None else "fields"][name]
        target = (
            date
            if name == "requested_completion_date"
            else Decimal
            if name == "quantity"
            else Text
        )
        candidates = []
        # Stable display spelling for equal customer names, irrespective of input order.
        if name == "customer_name":
            priority = rule["equal_display_order"]
            raw_candidates = sorted(
                raw_candidates,
                key=lambda c: (
                    priority.index(types[c.source.document_id])
                    if c.source and types.get(c.source.document_id) in priority
                    else len(priority)
                ),
            )
        for raw in raw_candidates:
            source = raw.source
            verified = (
                actual(source) and types.get(source.document_id) in rule["sources"]
            )
            if owner is not None:
                verified = verified and source.document_id == owner
            cell = None
            if verified:
                verified, cell = _context_matches(
                    name,
                    raw,
                    parsed[source.document_id],
                    row,
                    cell_maps.get(source.document_id),
                )
            interpretations, invalid = (
                normalize(name, raw.raw_value, cell) if verified else ((), False)
            )
            candidate = FieldCandidate[target](
                raw_value=raw.raw_value,
                interpretations=interpretations,
                verified=bool(verified),
                source=source,
            )
            candidates.append(candidate)
            if not verified:
                issue("unverified_source", path)
            elif invalid:
                issue("invalid_value", path, (source,))
        if name == "work_description":
            # A section is one value, not several conflicting paragraph values.
            # Compose only if prepared, verified candidates cover every nonempty paragraph.
            for p in parsed.values():
                if p.document.format != "docx":
                    continue
                for section in _description_sections(p):
                    ids = {
                        (b.fragment_id, b.text)
                        for b in section
                        if b.fragment_id is not None
                    }
                    selected = [
                        i
                        for i, c in enumerate(candidates)
                        if c.verified
                        and c.source.document_id == p.document.document_id
                        and (c.source.fragment_id, c.raw_value) in ids
                    ]
                    if not selected:
                        continue
                    complete = {
                        (candidates[i].source.fragment_id, candidates[i].raw_value)
                        for i in selected
                    } == ids
                    value = normalize(name, "\n".join(b.text for b in section))[0]
                    for index in selected:
                        c = candidates[index]
                        candidates[index] = FieldCandidate[Text](
                            raw_value=c.raw_value,
                            source=c.source,
                            verified=c.verified,
                            interpretations=value if complete else (),
                        )
                    if not complete:
                        issue(
                            "unsupported_content",
                            path,
                            tuple(candidates[i].source for i in selected),
                        )
        confirmed = [c for c in candidates if c.verified]
        definite = [
            c.interpretations[0] for c in confirmed if len(c.interpretations) == 1
        ]
        key = (lambda v: v.casefold()) if name == "customer_name" else (lambda v: v)
        distinct = {key(value) for value in definite}
        if not confirmed:
            state = "missing"
        elif len(distinct) > 1:
            state = "conflict"
        elif any(len(c.interpretations) != 1 for c in confirmed):
            state = "ambiguous"
        else:
            state = "found"
        code = {
            "missing": "required_missing" if rule["required"] else None,
            "ambiguous": "ambiguous_value",
            "conflict": "conflicting_values",
            "found": None,
        }[state]
        if code:
            issue(code, path, tuple(c.source for c in confirmed))
        model = CustomerNameResult if name == "customer_name" else FieldResult[target]
        return model(
            state=state,
            value=definite[0] if state == "found" else None,
            candidates=tuple(candidates),
        )

    event("normalization", "started")
    fields = {
        name: field_result(name, getattr(inputs.fields, name), name)
        for name in type(inputs.fields).model_fields
    }
    item_docs = [
        doc_id
        for doc_id, kind in types.items()
        if kind == "work_items"
        for _ in range(multiplicity[doc_id])
    ]
    items = []
    used_rows = set()
    if len(item_docs) == 1:
        doc_id = item_docs[0]
        p = parsed[doc_id]
        prepared = {(i.document_id, i.row): i for i in inputs.items}
        layout = item_layout(p)
        for sheet in p.sheets:
            if layout is None or sheet.name != layout[0]:
                continue
            for row in sheet.table.rows:
                if is_summary_row(row, layout[2]):
                    continue
                relevant = [
                    c
                    for c in row.cells
                    if c.location.cell
                    in {f"{col}{row.index}" for col in layout[2].values()}
                ]
                if row.index <= layout[1] or not any(
                    not c.usable
                    or (c.value.value is not None and str(c.value.value).strip())
                    for c in relevant
                ):
                    continue
                used_rows.add((doc_id, row.index))
                raw = prepared.get(
                    (doc_id, row.index), RawItem(document_id=doc_id, row=row.index)
                )
                index = len(items)
                for cell in relevant:
                    if not cell.usable:
                        name = next(
                            n
                            for n, col in layout[2].items()
                            if cell.location.cell == f"{col}{row.index}"
                        )
                        issue("unsupported_content", f"items[{index}].{name}")
                items.append(
                    WorkItem(
                        **{
                            name: field_result(
                                name,
                                getattr(raw, name),
                                f"items[{index}].{name}",
                                row.index,
                                doc_id,
                            )
                            for name in WorkItem.model_fields
                        }
                    )
                )
    for raw in inputs.items:
        if (raw.document_id, raw.row) not in used_rows:
            issue("unverified_source", "items")
    # Duplicate codes keep all rows and evidence; no automatic merging.
    counts = {}
    for item in items:
        if item.item_code.state == "found":
            counts[item.item_code.value] = counts.get(item.item_code.value, 0) + 1
    for index, item in enumerate(items):
        code = item.item_code
        if code.state == "found" and counts[code.value] > 1:
            replacement = FieldResult[Text](
                state="ambiguous",
                value=None,
                candidates=code.candidates,
                ambiguity_reason="duplicate_item_code",
            )
            items[index] = WorkItem(**{**item.model_dump(), "item_code": replacement})
            issue(
                "ambiguous_value",
                f"items[{index}].item_code",
                tuple(c.source for c in code.candidates if c.verified),
            )
    if not items:
        issue("required_missing", "items")
    event("normalization", "completed")
    event("validation", "completed")
    if issues:
        event("review", "needs_review")
    return finish(
        "completed", WorkRequestData(**fields, items=tuple(items)), classifications
    )
