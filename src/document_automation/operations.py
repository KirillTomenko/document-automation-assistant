"""Fixed application guards. No I/O, roles, configurable policies or executors."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from pydantic import model_validator

from document_automation.contracts import ContractModel, Text

if TYPE_CHECKING:
    from document_automation.schemas import AuditEvent, ProcessingResult

Operation = Literal[
    "read_source",
    "create_draft",
    "modify_confirmed",
    "export_document",
    "create_verification_report",
    "export_verification_report",
    "execute_external_action",
]


class OperationDecision(ContractModel):
    operation: Operation
    allowed: bool
    reason: Literal[
        "source_registered",
        "source_not_registered",
        "processing_completed",
        "processing_not_completed",
        "no_substantive_content",
        "run_mismatch",
        "prohibited_in_mvp",
        "artifact_not_verified",
        "artifact_verified",
        "report_allowed",
        "report_not_allowed",
        "report_verified",
        "report_not_verified",
    ]
    resource_id: Text | None = None
    draft_required: bool = False

    @model_validator(mode="after")
    def consistency(self):
        reasons = {
            "read_source": {"source_registered", "source_not_registered"},
            "create_draft": {
                "processing_completed",
                "processing_not_completed",
                "no_substantive_content",
                "run_mismatch",
            },
            "modify_confirmed": {"prohibited_in_mvp"},
            "execute_external_action": {"prohibited_in_mvp"},
            "export_document": {"artifact_not_verified", "artifact_verified"},
            "create_verification_report": {
                "report_allowed",
                "report_not_allowed",
                "processing_not_completed",
                "run_mismatch",
            },
            "export_verification_report": {"report_not_verified", "report_verified"},
        }
        if self.reason not in reasons[self.operation]:
            raise ValueError("reason does not belong to this operation")
        permitted = {
            ("read_source", "source_registered"),
            ("create_draft", "processing_completed"),
            ("export_document", "artifact_verified"),
            ("create_verification_report", "report_allowed"),
            ("export_verification_report", "report_verified"),
        }
        if self.allowed != ((self.operation, self.reason) in permitted):
            raise ValueError("decision disagrees with fixed operation rules")
        if self.draft_required and not (
            self.allowed and self.operation == "create_draft"
        ):
            raise ValueError("draft requirement applies only to permitted generation")
        return self


def check_operation(
    operation: Operation,
    *,
    run_id: str,
    registered_sources: tuple[tuple[str, str], ...] = (),
    source_id: str | None = None,
    result: "ProcessingResult | None" = None,
    artifact_bytes: bytes | None = None,
    report_bytes: bytes | None = None,
) -> OperationDecision:
    """Registration is application-owned (run_id, source_id), never supplied by LLM.

    Work request and report exports require their own byte-level verification;
    artifact metadata alone never authorizes an export.
    """
    if operation == "read_source":
        allowed = (run_id, source_id) in registered_sources
        return OperationDecision(
            operation=operation,
            allowed=allowed,
            reason="source_registered" if allowed else "source_not_registered",
            resource_id=source_id,
        )
    if operation == "create_draft":
        reason = "processing_not_completed"
        if result is not None:
            if result.run_id != run_id:
                reason = "run_mismatch"
            elif result.processing_status == "completed":
                reason = (
                    "processing_completed"
                    if has_substantive_content(result)
                    else "no_substantive_content"
                )
        return OperationDecision(
            operation=operation,
            allowed=reason == "processing_completed",
            reason=reason,
            draft_required=reason == "processing_completed" and result.needs_review,
        )
    if operation == "create_verification_report":
        reason = "processing_not_completed"
        if result is not None:
            if result.run_id != run_id:
                reason = "run_mismatch"
            elif result.processing_status == "completed" and (
                result.issues or result.documents
            ):
                reason = "report_allowed"
            else:
                reason = "report_not_allowed"
        return OperationDecision(
            operation=operation, allowed=reason == "report_allowed", reason=reason
        )
    if (
        operation == "export_verification_report"
        and result is not None
        and result.run_id == run_id
        and report_bytes
    ):
        from document_automation.generation import verify_verification_report

        if verify_verification_report(result, report_bytes):
            return OperationDecision(
                operation=operation, allowed=True, reason="report_verified"
            )
        return OperationDecision(
            operation=operation, allowed=False, reason="report_not_verified"
        )
    if (
        operation == "export_document"
        and result is not None
        and result.run_id == run_id
        and artifact_bytes
    ):
        from document_automation.generation import verify_document

        if verify_document(result, artifact_bytes):
            return OperationDecision(
                operation=operation, allowed=True, reason="artifact_verified"
            )
    reason = {
        "export_document": "artifact_not_verified",
        "export_verification_report": "report_not_verified",
    }.get(operation, "prohibited_in_mvp")
    return OperationDecision(operation=operation, allowed=False, reason=reason)


def has_substantive_content(result):
    if result.data is None:
        return False

    def usable(path, field):
        return (
            field.state == "found"
            and bool(field.value)
            and not any(
                issue.path == path and issue.code == "invalid_value"
                for issue in result.issues
            )
        )

    return any(
        usable(name, getattr(result.data, name))
        for name in ("customer_name", "request_title", "work_description")
    ) or any(
        usable(f"items[{index}].name", item.name)
        for index, item in enumerate(result.data.items)
    )


def audit_operation(
    operation: Operation,
    *,
    run_id: str,
    registered_sources: tuple[tuple[str, str], ...] = (),
    source_id: str | None = None,
    result: "ProcessingResult | None" = None,
    artifact_bytes: bytes | None = None,
    report_bytes: bytes | None = None,
) -> tuple[OperationDecision, "AuditEvent"]:
    """Application entry point: every evaluated operation returns its audit event.

    The caller retains the event; persistence and execution are deliberately absent.
    Local import avoids coupling the decision model to the public result schema.
    """
    from document_automation.schemas import AuditEvent

    decision = check_operation(
        operation,
        run_id=run_id,
        registered_sources=registered_sources,
        source_id=source_id,
        result=result,
        artifact_bytes=artifact_bytes,
        report_bytes=report_bytes,
    )
    event = AuditEvent(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc),
        stage="ingestion" if operation == "read_source" else "review",
        event="operation_decision",
        message=f"{operation}: {decision.reason}",
        decision=decision,
    )
    return decision, event
