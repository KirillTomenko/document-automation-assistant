"""SQLite snapshots, immutable input manifests and append-only audit per local run."""

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256

from document_automation import acceptance
from document_automation.artifact_files import FIELDS, ArtifactFiles, ArtifactWrite
from document_automation.data_lease import DataLease
from document_automation.llm_usage import AttemptRecord, RunMeter, summarize_usage
from document_automation.run_requests import RunRequest, request_fingerprint
from document_automation.schemas import AuditEvent, ProcessingResult, result_fingerprint


class StorageIntegrityError(Exception):
    pass


class RequestConflictError(Exception):
    def __init__(self, code):
        self.code = code


_REQUEST_SELECT = """SELECT q.run_id,q.key_sha256,q.retry_of,q.record,q.digest,
    (r.snapshot IS NOT NULL) FROM run_requests q JOIN runs r ON r.run_id=q.run_id"""


class ResultStore:
    def __init__(self, root):
        self.root, self.path = root, root / "workflow.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, snapshot TEXT, digest TEXT);
                CREATE TABLE IF NOT EXISTS inputs (run_id TEXT REFERENCES runs(run_id), source_id TEXT,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(run_id,source_id));
                CREATE TABLE IF NOT EXISTS documents (run_id TEXT REFERENCES runs(run_id), document_id TEXT,
                    metadata TEXT NOT NULL, PRIMARY KEY(run_id,document_id));
                CREATE TABLE IF NOT EXISTS events (run_id TEXT REFERENCES runs(run_id), sequence INTEGER,
                    event TEXT NOT NULL, PRIMARY KEY(run_id,sequence));
                CREATE TABLE IF NOT EXISTS artifacts (run_id TEXT PRIMARY KEY REFERENCES runs(run_id), metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS report_artifacts (run_id TEXT PRIMARY KEY REFERENCES runs(run_id), metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS llm_runs (run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    record TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS llm_attempts (run_id TEXT REFERENCES llm_runs(run_id),
                    attempt_no INTEGER NOT NULL CHECK(attempt_no>0), record TEXT NOT NULL,
                    digest TEXT NOT NULL, PRIMARY KEY(run_id,attempt_no));
                CREATE TABLE IF NOT EXISTS run_requests (run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    key_sha256 TEXT UNIQUE, retry_of TEXT REFERENCES runs(run_id),
                    record TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact_writes (
                    operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    kind TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_artifact_write
                    ON artifact_writes(run_id,kind) WHERE state='prepared';
                CREATE TABLE IF NOT EXISTS acceptance_events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id), sequence INTEGER NOT NULL,
                    record TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(run_id,sequence));
            """)
            if (
                db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or db.execute("PRAGMA foreign_key_check").fetchall()
            ):
                raise StorageIntegrityError("SQLite integrity check failed")
            interrupted = [
                row[0]
                for row in db.execute("SELECT run_id FROM runs WHERE snapshot IS NULL")
            ]
        # Single-process MVP: recover interrupted processing at service startup.
        self.recover_llm_attempts()
        for run_id in interrupted:
            try:
                self.fail_unfinished_request(run_id, "processing_interrupted")
            except StorageIntegrityError:
                # A corrupt pending identity must stay blocked, while unrelated
                # confirmed results remain available. Never erase its reservation.
                continue

    @contextmanager
    def connection(self):
        with DataLease(self.root):
            db = sqlite3.connect(self.path, timeout=10)
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def acceptance_history(self, result):
        try:
            with self.connection() as db:
                return acceptance.history(db, result)
        except ValueError:
            raise StorageIntegrityError("Invalid acceptance history") from None

    def record_acceptance(self, result, action, remarks_reviewed=False):
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT snapshot,digest FROM runs WHERE run_id=?", (result.run_id,)
                ).fetchone()
                if (
                    not row
                    or row[0] is None
                    or sha256(row[0].encode()).hexdigest() != row[1]
                    or ProcessingResult.model_validate_json(row[0]) != result
                ):
                    raise StorageIntegrityError("Result changed before acceptance")
                return acceptance.append_decision(db, result, action, remarks_reviewed)
        except ValueError:
            raise StorageIntegrityError("Invalid acceptance history") from None

    def _input_rows(self, run_id, paths):
        if not re.fullmatch("[0-9a-f]{32}", run_id):
            raise StorageIntegrityError("Invalid run identifier")
        rows = []
        for i, path in enumerate(paths):
            path = path.resolve()
            if not path.is_relative_to(self.root / run_id):
                raise StorageIntegrityError("Foreign input path")
            rows.append(
                (
                    run_id,
                    str(i),
                    str(path.relative_to(self.root)),
                    sha256(path.read_bytes()).hexdigest(),
                )
            )
        return rows

    def begin(self, run_id, paths, meter=None):
        rows = self._input_rows(run_id, paths)
        with self.connection() as db:
            db.execute("INSERT INTO runs(run_id) VALUES (?)", (run_id,))
            db.executemany("INSERT INTO inputs VALUES (?,?,?,?)", rows)
            if meter is not None:
                if meter.run_id != run_id:
                    raise StorageIntegrityError("Foreign run metering")
                text = meter.model_dump_json()
                db.execute(
                    "INSERT INTO llm_runs VALUES (?,?,?)",
                    (run_id, text, sha256(text.encode()).hexdigest()),
                )

    def _request_record(self, row):
        if row is None:
            return None
        run_id, key, retry_of, text, digest, has_snapshot = row
        record = self._ledger_record(RunRequest, text, digest)
        if (record.run_id, record.key_sha256, record.retry_of) != (
            run_id,
            key,
            retry_of,
        ):
            raise StorageIntegrityError("Foreign request identity")
        if record.request_sha256 != request_fingerprint(
            record.content_sha256, record.retry_of
        ):
            raise StorageIntegrityError("Invalid request fingerprint")
        if (record.state in ("finished", "interrupted")) != bool(has_snapshot):
            raise StorageIntegrityError("Request/result state mismatch")
        return record

    def get_request(self, run_id):
        with self.connection() as db:
            return self._request_record(
                db.execute(_REQUEST_SELECT + " WHERE q.run_id=?", (run_id,)).fetchone()
            )

    def retry_eligibility(self, run_id):
        operation = self.get_request(run_id)
        if operation and operation.state in ("registered", "processing"):
            return False, "retry_requires_failed_run"
        try:
            result = self.get(run_id)
        except KeyError:
            return False, "retry_run_not_found"
        if result.processing_status != "failed":
            return False, "retry_requires_failed_run"
        meter = self.get_meter(run_id)
        attempts = self.get_attempts(run_id)
        if (
            meter is None
            or meter.mode == "untracked"
            or any(a.outcome_unknown for a in attempts)
        ):
            return False, "retry_outcome_unknown"
        return True, None

    def reserve_request(self, record, meter):
        if record.run_id != meter.run_id or record.state != "registered":
            raise StorageIntegrityError("Invalid request registration")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if record.key_sha256 is not None:
                previous = self._request_record(
                    db.execute(
                        _REQUEST_SELECT + " WHERE q.key_sha256=?", (record.key_sha256,)
                    ).fetchone()
                )
                if previous:
                    if previous.request_sha256 != record.request_sha256:
                        raise RequestConflictError("idempotency_conflict")
                    return previous, False
            if record.retry_of:
                allowed, code = self.retry_eligibility(record.retry_of)
                if not allowed:
                    raise RequestConflictError(code)
            db.execute("INSERT INTO runs(run_id) VALUES (?)", (record.run_id,))
            text = meter.model_dump_json()
            db.execute(
                "INSERT INTO llm_runs VALUES (?,?,?)",
                (record.run_id, text, sha256(text.encode()).hexdigest()),
            )
            text = record.model_dump_json()
            db.execute(
                "INSERT INTO run_requests VALUES (?,?,?,?,?)",
                (
                    record.run_id,
                    record.key_sha256,
                    record.retry_of,
                    text,
                    sha256(text.encode()).hexdigest(),
                ),
            )
        return record, True

    def _update_request(self, db, old, new):
        if old.model_dump(exclude={"state"}) != new.model_dump(exclude={"state"}):
            raise StorageIntegrityError("Request identity is immutable")
        old_digest = sha256(old.model_dump_json().encode()).hexdigest()
        text = new.model_dump_json()
        changed = db.execute(
            "UPDATE run_requests SET record=?,digest=? WHERE run_id=? AND digest=?",
            (text, sha256(text.encode()).hexdigest(), old.run_id, old_digest),
        )
        if changed.rowcount != 1:
            raise StorageIntegrityError("Concurrent request state update")

    def register_request_inputs(self, run_id, paths):
        rows = self._input_rows(run_id, paths)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._request_record(
                db.execute(_REQUEST_SELECT + " WHERE q.run_id=?", (run_id,)).fetchone()
            )
            if record is None or record.state != "registered":
                raise StorageIntegrityError("Request already started or unavailable")
            db.executemany("INSERT INTO inputs VALUES (?,?,?,?)", rows)
            self._update_request(
                db, record, record.model_copy(update={"state": "processing"})
            )

    def fail_unfinished_request(self, run_id, code):
        with self.connection() as db:
            row = db.execute(
                "SELECT snapshot FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None or row[0] is not None:
            return
        request = self.get_request(run_id)
        event = AuditEvent(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc),
            stage="ingestion",
            event="failed",
            message="Processing stopped before result persistence",
            error_code=code,
        )
        self.save(
            ProcessingResult(
                run_id=run_id,
                contract_version=request.contract_version if request else "1.0",
                processing_status="failed",
                documents=(),
                classifications=(),
                data=None,
                issues=(),
                needs_review=False,
                audit=(event,),
            )
        )

    @staticmethod
    def _ledger_record(model, text, digest):
        if sha256(text.encode()).hexdigest() != digest:
            raise StorageIntegrityError("LLM ledger digest mismatch")
        try:
            return model.model_validate_json(text)
        except ValueError:
            raise StorageIntegrityError("Invalid LLM ledger record") from None

    def get_meter(self, run_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT record,digest FROM llm_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        meter = self._ledger_record(RunMeter, *row)
        if meter.run_id != run_id:
            raise StorageIntegrityError("Foreign LLM meter")
        return meter

    def get_attempts(self, run_id):
        with self.connection() as db:
            rows = db.execute(
                "SELECT attempt_no,record,digest FROM llm_attempts WHERE run_id=? ORDER BY attempt_no",
                (run_id,),
            ).fetchall()
        attempts = []
        for number, text, digest in rows:
            record = self._ledger_record(AttemptRecord, text, digest)
            if (record.run_id, record.attempt_no) != (run_id, number):
                raise StorageIntegrityError("Foreign LLM attempt")
            attempts.append(record)
        if [a.attempt_no for a in attempts] != list(range(1, len(attempts) + 1)):
            raise StorageIntegrityError("LLM attempt sequence gap")
        return tuple(attempts)

    def start_attempt(self, record):
        try:
            record = AttemptRecord.model_validate(record.model_dump())
        except ValueError:
            raise StorageIntegrityError("Invalid LLM attempt") from None
        meter = self.get_meter(record.run_id)
        if (
            meter is None
            or meter.mode != "llm"
            or (meter.provider, meter.model) != (record.provider, record.model)
        ):
            raise StorageIntegrityError("Unregistered LLM identity")
        if (
            record.status != "started"
            or record.attempt_no != len(self.get_attempts(record.run_id)) + 1
        ):
            raise StorageIntegrityError(
                "LLM attempt already recorded or out of sequence"
            )
        text = record.model_dump_json()
        with self.connection() as db:
            try:
                db.execute(
                    "INSERT INTO llm_attempts VALUES (?,?,?,?)",
                    (
                        record.run_id,
                        record.attempt_no,
                        text,
                        sha256(text.encode()).hexdigest(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise StorageIntegrityError("LLM attempt already recorded") from None

    def finish_attempt(self, record):
        # Compare-and-swap: duplicate persistence is harmless; a different terminal
        # record cannot overwrite costs or identity under the same primary key.
        try:
            record = AttemptRecord.model_validate(record.model_dump())
        except ValueError:
            raise StorageIntegrityError("Invalid LLM attempt") from None
        with self.connection() as db:
            row = db.execute(
                "SELECT record,digest FROM llm_attempts WHERE run_id=? AND attempt_no=?",
                (record.run_id, record.attempt_no),
            ).fetchone()
            if row is None:
                raise StorageIntegrityError("Unregistered LLM attempt")
            previous = self._ledger_record(AttemptRecord, *row)
            if previous == record:
                return
            if previous.status != "started" or record.status == "started":
                raise StorageIntegrityError("LLM attempt cannot be rewritten")
            immutable = (
                "run_id",
                "provider",
                "model",
                "attempt_no",
                "started_at",
                "tariff",
            )
            if any(getattr(previous, key) != getattr(record, key) for key in immutable):
                raise StorageIntegrityError("LLM attempt identity changed")
            text = record.model_dump_json()
            updated = db.execute(
                "UPDATE llm_attempts SET record=?,digest=? WHERE run_id=? AND attempt_no=? AND digest=?",
                (
                    text,
                    sha256(text.encode()).hexdigest(),
                    record.run_id,
                    record.attempt_no,
                    row[1],
                ),
            )
            if updated.rowcount != 1:
                raise StorageIntegrityError("Concurrent LLM attempt update")

    def reject_attempt_output(self, run_id, number, code):
        with self.connection() as db:
            row = db.execute(
                "SELECT record,digest FROM llm_attempts WHERE run_id=? AND attempt_no=?",
                (run_id, number),
            ).fetchone()
            if row is None:
                return
            record = self._ledger_record(AttemptRecord, *row)
            if record.status != "succeeded":
                return
            record = AttemptRecord.model_validate(
                {**record.model_dump(), "status": "failed", "error_code": code}
            )
            text = record.model_dump_json()
            db.execute(
                "UPDATE llm_attempts SET record=?,digest=? WHERE run_id=? AND attempt_no=? AND digest=?",
                (text, sha256(text.encode()).hexdigest(), run_id, number, row[1]),
            )

    def recover_llm_attempts(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT run_id,attempt_no,record,digest FROM llm_attempts"
            ).fetchall()
        for run_id, number, text, digest in rows:
            try:
                record = self._ledger_record(AttemptRecord, text, digest)
            except StorageIntegrityError:
                # Corrupt telemetry stays unavailable, never becomes a zero cost.
                # Do not prevent access to independently valid business results.
                continue
            if (record.run_id, record.attempt_no) != (run_id, number):
                continue
            if record.status == "started":
                self.finish_attempt(
                    AttemptRecord.model_validate(
                        {
                            **record.model_dump(),
                            "status": "interrupted",
                            "finished_at": datetime.now(timezone.utc),
                            "error_code": "attempt_interrupted",
                        }
                    )
                )

    def usage_summary(self, run_id):
        return summarize_usage(
            run_id, self.get_meter(run_id), self.get_attempts(run_id)
        )

    def get(self, run_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT snapshot,digest FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None or row[0] is None:
                raise KeyError(run_id)
            text, digest = row
            if sha256(text.encode()).hexdigest() != digest:
                raise StorageIntegrityError("Result digest mismatch")
            try:
                result = ProcessingResult.model_validate_json(text)
            except ValueError:
                raise StorageIntegrityError("Invalid saved result") from None
            if result.run_id != run_id:
                raise StorageIntegrityError("Foreign result")
            events = db.execute(
                "SELECT sequence,event FROM events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            expected = [(i, e.model_dump_json()) for i, e in enumerate(result.audit)]
            if events != expected:
                raise StorageIntegrityError("Audit mismatch")
            documents = dict(
                db.execute(
                    "SELECT document_id,metadata FROM documents WHERE run_id=?",
                    (run_id,),
                ).fetchall()
            )
            if documents != {
                d.document_id: d.model_dump_json() for d in result.documents
            }:
                raise StorageIntegrityError("Document metadata mismatch")
            artifact = db.execute(
                "SELECT metadata FROM artifacts WHERE run_id=?", (run_id,)
            ).fetchone()
            expected_artifact = (
                (result.generated_document.model_dump_json(),)
                if result.generated_document
                else None
            )
            if artifact != expected_artifact:
                raise StorageIntegrityError("Artifact metadata mismatch")
            report = db.execute(
                "SELECT metadata FROM report_artifacts WHERE run_id=?", (run_id,)
            ).fetchone()
            expected_report = (
                (result.generated_report.model_dump_json(),)
                if result.generated_report
                else None
            )
            if report != expected_report:
                raise StorageIntegrityError("Report metadata mismatch")
            return result

    def artifact_writes(self, run_id, kind):
        with self.connection() as db:
            rows = db.execute(
                "SELECT operation_id,run_id,kind,state,record,digest FROM artifact_writes WHERE run_id=? AND kind=? ORDER BY rowid",
                (run_id, kind),
            ).fetchall()
        return tuple(self._artifact_record(row) for row in rows)

    def _artifact_record(self, row):
        if row is None:
            raise StorageIntegrityError("Missing artifact write")
        operation_id, run_id, kind, state, text, digest = row
        record = self._ledger_record(ArtifactWrite, text, digest)
        if (record.operation_id, record.run_id, record.kind, record.state) != (
            operation_id,
            run_id,
            kind,
            state,
        ):
            raise StorageIntegrityError("Foreign artifact write")
        return record

    def prepare_artifact(self, record):
        result = self.get(record.run_id)
        if (
            record.state != "prepared"
            or record.result_sha256 != result_fingerprint(result)
            or record.previous_metadata != getattr(result, FIELDS[record.kind])
        ):
            raise StorageIntegrityError("Invalid artifact intent")
        text = record.model_dump_json()
        with self.connection() as db:
            db.execute(
                "INSERT INTO artifact_writes VALUES (?,?,?,?,?,?)",
                (
                    record.operation_id,
                    record.run_id,
                    record.kind,
                    record.state,
                    text,
                    sha256(text.encode()).hexdigest(),
                ),
            )

    def _finish_artifact(self, db, write, state):
        row = db.execute(
            "SELECT operation_id,run_id,kind,state,record,digest FROM artifact_writes WHERE operation_id=?",
            (write.operation_id,),
        ).fetchone()
        if self._artifact_record(row) != write or write.state != "prepared":
            raise StorageIntegrityError("Artifact write already finished or changed")
        record = ArtifactWrite.model_validate(
            {
                **write.model_dump(),
                "state": state,
                "finished_at": datetime.now(timezone.utc),
            }
        )
        text = record.model_dump_json()
        changed = db.execute(
            "UPDATE artifact_writes SET state=?,record=?,digest=? WHERE operation_id=? AND digest=?",
            (
                state,
                text,
                sha256(text.encode()).hexdigest(),
                write.operation_id,
                row[-1],
            ),
        )
        if changed.rowcount != 1:
            raise StorageIntegrityError("Concurrent artifact write update")

    def save(self, result, *, artifact_write=None, abandon_write=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            request = self._request_record(
                db.execute(
                    _REQUEST_SELECT + " WHERE q.run_id=?", (result.run_id,)
                ).fetchone()
            )
            row = db.execute(
                "SELECT snapshot FROM runs WHERE run_id=?", (result.run_id,)
            ).fetchone()
            if row is None:
                raise StorageIntegrityError("Unregistered run")
            old = []
            previous = None
            if row[0]:
                previous = self.get(result.run_id)
                if previous.model_dump(
                    exclude={"audit", "generated_document", "generated_report"}
                ) != result.model_dump(
                    exclude={"audit", "generated_document", "generated_report"}
                ):
                    raise StorageIntegrityError("Processed data is immutable")
                old = list(previous.audit)
            changed_artifacts = [
                kind
                for kind, field in FIELDS.items()
                if getattr(result, field)
                != (getattr(previous, field) if previous else None)
            ]
            if artifact_write is not None:
                write = artifact_write
                meta = getattr(result, FIELDS[write.kind])
                if (
                    changed_artifacts not in ([], [write.kind])
                    or write.run_id != result.run_id
                    or write.result_sha256 != result_fingerprint(result)
                    or write.previous_metadata
                    != (getattr(previous, FIELDS[write.kind]) if previous else None)
                    or meta is None
                    or meta.filename != write.filename
                    or meta.sha256 != write.expected_sha256
                ):
                    raise StorageIntegrityError("Artifact commit disagrees with intent")
                ArtifactFiles(self.root, result.run_id).validate(
                    result, write.kind, meta.filename, meta.sha256
                )
                self._finish_artifact(db, write, "committed")
            elif changed_artifacts:
                raise StorageIntegrityError(
                    "Artifact metadata requires verified publication"
                )
            if abandon_write is not None:
                if artifact_write is not None or abandon_write.run_id != result.run_id:
                    raise StorageIntegrityError("Invalid artifact recovery")
                self._finish_artifact(db, abandon_write, "abandoned")
            if list(result.audit[: len(old)]) != old or len(result.audit) < len(old):
                raise StorageIntegrityError("Audit cannot be rewritten")
            inputs = db.execute(
                "SELECT path,sha256 FROM inputs WHERE run_id=?", (result.run_id,)
            ).fetchall()
            from pathlib import Path

            registered = {(Path(path).name, digest) for path, digest in inputs}
            if any(
                (doc.filename, doc.sha256) not in registered for doc in result.documents
            ):
                raise StorageIntegrityError("Foreign source document")
            db.executemany(
                "INSERT INTO events VALUES (?,?,?)",
                [
                    (result.run_id, i, e.model_dump_json())
                    for i, e in enumerate(result.audit)
                    if i >= len(old)
                ],
            )
            for document in result.documents:
                db.execute(
                    "INSERT OR IGNORE INTO documents VALUES (?,?,?)",
                    (result.run_id, document.document_id, document.model_dump_json()),
                )
            if result.generated_document:
                db.execute(
                    "INSERT INTO artifacts VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET metadata=excluded.metadata",
                    (result.run_id, result.generated_document.model_dump_json()),
                )
            if result.generated_report:
                db.execute(
                    "INSERT INTO report_artifacts VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET metadata=excluded.metadata",
                    (result.run_id, result.generated_report.model_dump_json()),
                )
            text = result.model_dump_json()
            db.execute(
                "UPDATE runs SET snapshot=?,digest=? WHERE run_id=?",
                (text, sha256(text.encode()).hexdigest(), result.run_id),
            )
            if request and request.state in ("registered", "processing"):
                state = (
                    "interrupted"
                    if any(
                        e.error_code == "processing_interrupted" for e in result.audit
                    )
                    else "finished"
                )
                self._update_request(
                    db, request, request.model_copy(update={"state": state})
                )

    def verify_inputs(self, run_id):
        with self.connection() as db:
            rows = db.execute(
                "SELECT path,sha256 FROM inputs WHERE run_id=?", (run_id,)
            ).fetchall()
        for relative, digest in rows:
            path = (self.root / relative).resolve()
            if (
                not path.is_relative_to(self.root / run_id)
                or not path.is_file()
                or path.stat().st_size > 10 * 1024 * 1024
            ):
                raise StorageIntegrityError("Input unavailable or outside run")
            if sha256(path.read_bytes()).hexdigest() != digest:
                raise StorageIntegrityError("Input bytes changed")
