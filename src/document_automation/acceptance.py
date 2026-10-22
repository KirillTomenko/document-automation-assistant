"""Operator decisions outside immutable business results, with append-only history."""

from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from document_automation.contracts import ContractModel, Sha256
from document_automation.run_requests import RunId
from document_automation.schemas import result_fingerprint


class AcceptanceAction(ContractModel):
    remarks_reviewed: bool = False


class AcceptanceEvent(ContractModel):
    run_id: RunId
    sequence: int = Field(ge=1)
    action: Literal["accepted", "revoked"]
    timestamp: AwareDatetime
    result_sha256: Sha256
    previous_sha256: Sha256
    remarks_reviewed: bool
    document_sha256: Sha256 | None
    report_sha256: Sha256 | None

    @model_validator(mode="after")
    def evidence(self):
        if self.action == "accepted":
            if self.document_sha256 is None or self.report_sha256 is None:
                raise ValueError("Acceptance requires evidence for both artifacts")
        elif self.remarks_reviewed or self.document_sha256 or self.report_sha256:
            raise ValueError("Revocation cannot attest documents or remarks")
        return self


class AcceptanceStatus(ContractModel):
    run_id: RunId
    accepted: bool
    eligible: bool
    block_code: str | None
    history: tuple[AcceptanceEvent, ...]


def history(db, result):
    # Stage 8 backups predate this additive table. Reading them never runs DDL.
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='acceptance_events'"
    ).fetchone():
        return ()
    rows = db.execute(
        "SELECT sequence,record,digest FROM acceptance_events WHERE run_id=? ORDER BY sequence",
        (result.run_id,),
    ).fetchall()
    events, previous = [], "0" * 64
    for expected, (number, text, digest) in enumerate(rows, 1):
        if number != expected or sha256(text.encode()).hexdigest() != digest:
            raise ValueError("Acceptance sequence or digest mismatch")
        event = AcceptanceEvent.model_validate_json(text)
        if (
            event.run_id != result.run_id
            or event.sequence != number
            or event.previous_sha256 != previous
            or event.result_sha256 != result_fingerprint(result)
            or result.processing_status != "completed"
            or event.action != ("accepted" if number % 2 else "revoked")
            or event.action == "accepted"
            and result.needs_review
            and not event.remarks_reviewed
            or events
            and event.timestamp < events[-1].timestamp
        ):
            raise ValueError("Acceptance history is inconsistent")
        events.append(event)
        previous = digest
    return tuple(events)


def append_decision(db, result, action, remarks_reviewed):
    events = history(db, result)
    accepted = bool(events and events[-1].action == "accepted")
    if accepted == (action == "accepted"):
        return events  # Repeated clicks never insert a second event.
    previous = (
        db.execute(
            "SELECT digest FROM acceptance_events WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
            (result.run_id,),
        ).fetchone()[0]
        if events
        else "0" * 64
    )
    event = AcceptanceEvent(
        run_id=result.run_id,
        sequence=len(events) + 1,
        action=action,
        timestamp=datetime.now(timezone.utc),
        result_sha256=result_fingerprint(result),
        previous_sha256=previous,
        remarks_reviewed=remarks_reviewed if action == "accepted" else False,
        document_sha256=result.generated_document.sha256
        if action == "accepted"
        else None,
        report_sha256=result.generated_report.sha256 if action == "accepted" else None,
    )
    text = event.model_dump_json()
    db.execute(
        "INSERT INTO acceptance_events(run_id,sequence,record,digest) VALUES(?,?,?,?)",
        (result.run_id, event.sequence, text, sha256(text.encode()).hexdigest()),
    )
    return history(
        db, result
    )  # Validate the new transition before its transaction commits.
