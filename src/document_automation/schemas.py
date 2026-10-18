"""Public data contracts and consistency checks for supplied processing results."""

from typing import Literal

from pydantic import AwareDatetime, field_validator, model_validator

from document_automation.contracts import (
    ClassificationResult,
    ContractModel,
    FieldCandidate,
    FieldResult,
    FieldState,
    InputDocument,
    ProcessingStatus,
    Sha256,
    SourceFragment,
    Text,
    ValidationIssue,
)
from document_automation.operations import OperationDecision
from document_automation.workflows.work_request import (
    WorkItem,
    WorkRequestData,
    demo_contract,
)

__all__ = [
    "InputDocument",
    "SourceFragment",
    "ClassificationResult",
    "FieldCandidate",
    "FieldResult",
    "FieldState",
    "ProcessingStatus",
    "WorkItem",
    "WorkRequestData",
    "ValidationIssue",
    "ProcessingResult",
    "GeneratedDocument",
    "GeneratedReport",
    "AuditEvent",
]

Stage = Literal[
    "ingestion",
    "parsing",
    "classification",
    "extraction",
    "normalization",
    "validation",
    "review",
    "generation",
]


class AuditEvent(ContractModel):
    run_id: Text
    timestamp: AwareDatetime
    stage: Stage
    event: Literal[
        "started",
        "completed",
        "failed",
        "model_used",
        "needs_review",
        "generated",
        "operation_decision",
    ]
    message: Text
    error_code: Text | None = None
    model: Text | None = None
    decision: OperationDecision | None = None

    @model_validator(mode="after")
    def event_details(self):
        if (self.event == "operation_decision") != (self.decision is not None):
            raise ValueError("operation_decision requires decision details exclusively")
        if (self.event == "failed") != (self.error_code is not None):
            raise ValueError("only failed events require an error code")
        if self.event == "model_used" and self.model is None:
            raise ValueError("model_used requires model identification")
        return self


class GeneratedDocument(ContractModel):
    run_id: Text
    filename: Text
    sha256: Sha256
    template_sha256: Sha256
    needs_review: bool
    human_review_required: Literal[True] = True

    @field_validator("human_review_required", mode="before")
    @classmethod
    def strict_true(cls, value):
        if value is not True:
            raise ValueError("human_review_required must be the boolean true")
        return value


class GeneratedReport(ContractModel):
    run_id: Text
    filename: Literal["verification_report.docx"] = "verification_report.docx"
    sha256: Sha256
    result_sha256: Sha256
    audit_snapshot: tuple[AuditEvent, ...]


def result_fingerprint(result):
    from hashlib import sha256

    return sha256(
        result.model_dump_json(
            exclude={"audit", "generated_document", "generated_report"}
        ).encode()
    ).hexdigest()


class ProcessingResult(ContractModel):
    run_id: Text
    processing_status: ProcessingStatus
    documents: tuple[InputDocument, ...]
    classifications: tuple[ClassificationResult, ...]
    data: WorkRequestData | None
    issues: tuple[ValidationIssue, ...]
    needs_review: bool
    audit: tuple[AuditEvent, ...]
    generated_document: GeneratedDocument | None = None
    generated_report: GeneratedReport | None = None

    @property
    def can_generate(self) -> bool:
        from document_automation.operations import has_substantive_content

        return self.processing_status == "completed" and has_substantive_content(self)

    @model_validator(mode="after")
    def result_consistency(self):
        documents = {d.document_id: d for d in self.documents}
        if len(documents) != len(self.documents):
            raise ValueError("duplicate document IDs")
        types = {c.document_id: c.document_type for c in self.classifications}
        if (
            len(types) != len(self.classifications)
            or not types.keys() <= documents.keys()
        ):
            raise ValueError("invalid classification document references")
        fragments = {}

        def check_source(source):
            if source is None:
                return
            doc = documents.get(source.document_id)
            if doc is None or doc.format != source.location.format:
                raise ValueError(
                    "source references missing document or mismatched format"
                )
            key = (source.document_id, source.fragment_id)
            if key in fragments and fragments[key] != source:
                raise ValueError("inconsistent fragment identity")
            fragments[key] = source

        for classification in self.classifications:
            for source in classification.evidence:
                check_source(source)
        for issue in self.issues:
            for source in issue.sources:
                check_source(source)
        if any(event.run_id != self.run_id for event in self.audit):
            raise ValueError("audit belongs to another run")
        if self.issues and not self.needs_review:
            raise ValueError(
                "validation issues require needs_review even on a failed run"
            )
        if self.processing_status == "failed":
            if (
                self.generated_document is not None
                or self.generated_report is not None
                or self.data is not None
            ):
                raise ValueError("failed result has no completed business data or DOCX")
            if not any(e.event == "failed" for e in self.audit):
                raise ValueError("failed result requires an audit error")
            return self
        if self.data is None:
            raise ValueError("completed result requires business data")
        if any(e.event == "failed" for e in self.audit):
            raise ValueError("completed result cannot contain a terminal failure")

        contract = demo_contract()
        issue_keys = {(i.code, i.path) for i in self.issues}

        def require_issue(code, path):
            if (code, path) not in issue_keys:
                raise ValueError(f"missing issue {code} at {path}")

        for kind, fmt in contract["document_types"].items():
            matches = [c for c in self.classifications if c.document_type == kind]
            if not matches:
                require_issue("missing_document", "documents")
            if len(matches) > 1:
                require_issue("duplicate_document", "documents")
            if any(documents[c.document_id].format != fmt for c in matches):
                raise ValueError("demo document type/format mismatch")
        if types.keys() != documents.keys() or "unknown" in types.values():
            require_issue("unknown_document", "documents")

        def check_field(path, result, rule):
            if result.state == "missing" and rule["required"]:
                require_issue("required_missing", path)
            if result.state == "ambiguous":
                require_issue("ambiguous_value", path)
            if result.state == "conflict":
                require_issue("conflicting_values", path)
            if any(not c.verified for c in result.candidates):
                require_issue("unverified_source", path)
            for candidate in result.candidates:
                # Unverified claimed sources are diagnostics, not trusted references.
                if candidate.verified:
                    check_source(candidate.source)
                    if types.get(candidate.source.document_id) not in rule["sources"]:
                        raise ValueError(f"source type not allowed for {path}")
            if result.ambiguity_reason and not path.endswith(".item_code"):
                raise ValueError("duplicate_item_code applies only to item codes")

        for name, rule in contract["fields"].items():
            if name != "items":
                check_field(name, getattr(self.data, name), rule)
        if not self.data.items:
            require_issue("required_missing", "items")
        for index, item in enumerate(self.data.items):
            for name, rule in contract["item_fields"].items():
                check_field(f"items[{index}].{name}", getattr(item, name), rule)
        if self.needs_review != bool(self.issues):
            raise ValueError("completed needs_review must reflect validation issues")
        generated = self.generated_document
        if generated:
            if (
                generated.run_id != self.run_id
                or generated.needs_review != self.needs_review
            ):
                raise ValueError("generated document disagrees with processing result")
            expected_name = (
                "work_request_needs_review.docx"
                if self.needs_review
                else "work_request.docx"
            )
            if generated.filename != expected_name:
                raise ValueError("generated filename disagrees with demo contract")
        report = self.generated_report
        if report and (
            report.run_id != self.run_id
            or report.result_sha256 != result_fingerprint(self)
            or any(event.run_id != self.run_id for event in report.audit_snapshot)
            or report.audit_snapshot != self.audit[: len(report.audit_snapshot)]
        ):
            raise ValueError("verification report disagrees with processing result")
        return self
