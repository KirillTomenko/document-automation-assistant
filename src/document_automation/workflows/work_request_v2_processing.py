"""Resolve v2 claims only against application-detected literal structures."""

from datetime import date, datetime, timezone
from decimal import Decimal

from document_automation.contracts import (
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
from document_automation.workflows.work_request_normalization import normalize


def process_v2(run_id, views, extracted, events=(), duplicate=False):
    issues, audit = [], list(events)
    contract = demo_contract("2.0")

    def issue(code, path, sources=()):
        for index, old in enumerate(issues):
            if (old.code, old.path) == (code, path):
                issues[index] = ValidationIssue(
                    code=code,
                    path=path,
                    message=old.message,
                    sources=tuple(dict.fromkeys(old.sources + tuple(sources))),
                )
                return
        issues.append(
            ValidationIssue(
                code=code, path=path, message=f"{code}: {path}", sources=tuple(sources)
            )
        )

    fragments = {
        (f.document_id, f.fragment_id): f for v in views for f in v.parsed.fragments
    }
    expected_types = {
        (c.document_id, c.document_type): c for v in views for c in v.classifications
    }
    classifications, accepted_types = [], set()
    for claim in extracted.classifications:
        expected = expected_types.get((claim.document_id, claim.document_type))
        refs = tuple(
            fragments.get((claim.document_id, fid)) for fid in claim.evidence_ids
        )
        valid = expected is not None and (
            claim.document_type == "unknown"
            or (
                refs
                and all(refs)
                and {r.fragment_id for r in refs}
                <= {r.fragment_id for r in expected.evidence}
            )
        )
        if valid:
            accepted_types.add((claim.document_id, claim.document_type))
            classifications.append(
                expected.model_copy(update={"evidence": tuple(r for r in refs if r)})
            )
        else:
            issue("unverified_source", "documents")
    for view in views:
        doc_id = view.parsed.document.document_id
        if not any(c.document_id == doc_id for c in classifications):
            from document_automation.contracts import ClassificationResult

            classifications.append(
                ClassificationResult(
                    document_id=doc_id, document_type="unknown", evidence=()
                )
            )
        if any(
            c.document_id == doc_id and c.document_type == "unknown"
            for c in classifications
        ):
            issue("unknown_document", "documents")
        if view.unsupported:
            issue("unsupported_content", "documents", view.unsupported_sources)
    if duplicate:
        issue("duplicate_document", "documents")

    scalar_evidence = {
        (v.field, v.source.document_id, v.source.fragment_id, v.raw_value): v
        for view in views
        for v in view.values
    }
    rows = [
        (view.parsed.document.document_id, row) for view in views for row in view.rows
    ]
    item_evidence = {
        (value.field, doc, value.source.fragment_id, value.raw_value, row.row_id): value
        for doc, row in rows
        for value in row.values
    }

    def resolve(name, path, owner=None, row=None):
        target = (
            date
            if name == "requested_completion_date"
            else Decimal
            if name == "quantity"
            else Text
        )
        claims = [
            c
            for c in extracted.candidates
            if c.field == name
            and (
                c.row_id is None
                if row is None
                else c.row_id == row.row_id and c.document_id == owner
            )
        ]
        candidates, metadata = [], []

        # Keep v1 customer spelling preference; remaining ties are independent of upload order.
        def order(claim):
            priority = next(
                (
                    i
                    for i, kind in enumerate(
                        ("customer_card", "work_description", "work_items")
                    )
                    if (claim.document_id, kind) in accepted_types
                ),
                3,
            )
            return (
                priority if name == "customer_name" else 0,
                claim.document_id,
                claim.fragment_id,
                claim.raw_value,
            )

        for claim in sorted(claims, key=order):
            key = (name, claim.document_id, claim.fragment_id, claim.raw_value)
            evidence = (
                scalar_evidence.get(key)
                if row is None
                else item_evidence.get((*key, row.row_id))
            )
            required_role = (
                "work_items"
                if row is not None
                else "customer_card"
                if name in ("customer_name", "contact_email")
                else "work_description"
            )
            verified = (
                evidence is not None
                and (claim.document_id, required_role) in accepted_types
            )
            source = fragments.get((claim.document_id, claim.fragment_id))
            interpretations, invalid = (
                normalize(name, claim.raw_value, evidence.cell)
                if verified
                else ((), False)
            )
            candidates.append(
                FieldCandidate[target](
                    raw_value=claim.raw_value,
                    interpretations=interpretations,
                    verified=verified,
                    source=source,
                )
            )
            metadata.append(evidence if verified else None)
            if not verified:
                issue("unverified_source", path)
            elif invalid:
                issue("invalid_value", path, (source,))
        if name == "work_description":
            sections = {}
            for evidence in scalar_evidence.values():
                if evidence.section is not None:
                    sections.setdefault(
                        (evidence.source.document_id, evidence.section), []
                    ).append(evidence)
            for (doc, section), expected in sections.items():
                selected = [
                    i
                    for i, m in enumerate(metadata)
                    if m and m.source.document_id == doc and m.section == section
                ]
                if not selected:
                    continue
                complete = {
                    (metadata[i].source.fragment_id, metadata[i].raw_value)
                    for i in selected
                } == {(e.source.fragment_id, e.raw_value) for e in expected}
                value = (
                    normalize(name, "\n".join(e.raw_value for e in expected))[0]
                    if complete
                    else ()
                )
                for i in selected:
                    candidates[i] = FieldCandidate[Text](
                        raw_value=candidates[i].raw_value,
                        interpretations=value,
                        verified=True,
                        source=candidates[i].source,
                    )
                if not complete:
                    issue("unsupported_content", path)
        confirmed = [c for c in candidates if c.verified]
        definite = [
            c.interpretations[0] for c in confirmed if len(c.interpretations) == 1
        ]
        keys = {v.casefold() if name == "customer_name" else v for v in definite}
        state = (
            "missing"
            if not confirmed
            else "conflict"
            if len(keys) > 1
            else "ambiguous"
            if any(len(c.interpretations) != 1 for c in confirmed)
            else "found"
        )
        rule = contract["item_fields" if row else "fields"][name]
        code = {
            "missing": "required_missing" if rule["required"] else None,
            "conflict": "conflicting_values",
            "ambiguous": "ambiguous_value",
            "found": None,
        }[state]
        if code:
            issue(code, path, tuple(c.source for c in confirmed))
        cls = CustomerNameResult if name == "customer_name" else FieldResult[target]
        return cls(
            state=state,
            value=definite[0] if state == "found" else None,
            candidates=tuple(candidates),
        )

    fields = {
        name: resolve(name, name)
        for name in WorkRequestData.model_fields
        if name != "items"
    }
    items = []
    for doc, row in rows:
        index = len(items)
        if (doc, "work_items") not in accepted_types:
            continue
        for name in row.unsupported_fields:
            issue("unsupported_content", f"items[{index}].{name}")
        items.append(
            WorkItem(
                **{
                    name: resolve(name, f"items[{index}].{name}", doc, row)
                    for name in WorkItem.model_fields
                }
            )
        )
    actual_rows = {(doc, row.row_id) for doc, row in rows}
    if any(
        c.row_id and (c.document_id, c.row_id) not in actual_rows
        for c in extracted.candidates
    ):
        issue("unverified_source", "items")
    counts, signatures = {}, {}
    for item in items:
        if item.item_code.state == "found":
            counts[item.item_code.value] = counts.get(item.item_code.value, 0) + 1
        signature = tuple(getattr(item, f).value for f in ("name", "quantity", "unit"))
        if all(v is not None for v in signature):
            signatures[signature] = signatures.get(signature, 0) + 1
    for index, item in enumerate(items):
        code = item.item_code
        if code.state == "found" and counts[code.value] > 1:
            items[index] = WorkItem(
                **{
                    **item.model_dump(),
                    "item_code": FieldResult[Text](
                        state="ambiguous",
                        value=None,
                        candidates=code.candidates,
                        ambiguity_reason="duplicate_item_code",
                    ),
                }
            )
            issue(
                "ambiguous_value",
                f"items[{index}].item_code",
                tuple(c.source for c in code.candidates if c.verified),
            )
        elif (
            code.state != "found"
            and signatures.get(
                tuple(getattr(item, f).value for f in ("name", "quantity", "unit")), 0
            )
            > 1
        ):
            issue("ambiguous_value", "items")
    if not items:
        issue("required_missing", "items")
    for stage, event in (
        ("classification", "completed"),
        ("normalization", "completed"),
        ("validation", "completed"),
    ):
        audit.append(
            AuditEvent(
                run_id=run_id,
                timestamp=datetime.now(timezone.utc),
                stage=stage,
                event=event,
                message=f"{stage}: {event}",
            )
        )
    if issues:
        audit.append(
            AuditEvent(
                run_id=run_id,
                timestamp=datetime.now(timezone.utc),
                stage="review",
                event="needs_review",
                message="review: needs_review",
            )
        )
    result = ProcessingResult(
        run_id=run_id,
        contract_version="2.0",
        processing_status="completed",
        documents=tuple(v.parsed.document for v in views),
        classifications=tuple(classifications),
        data=WorkRequestData(**fields, items=tuple(items)),
        issues=tuple(issues),
        needs_review=bool(issues),
        audit=tuple(audit),
    )
    _, decision = audit_operation("create_draft", run_id=run_id, result=result)
    return ProcessingResult.model_validate(
        {**result.model_dump(), "audit": result.audit + (decision,)}
    )
