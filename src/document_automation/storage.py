"""SQLite snapshots, immutable input manifests and append-only audit per local run."""

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256

from document_automation.schemas import AuditEvent, ProcessingResult


class StorageIntegrityError(Exception):
    pass


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
        for run_id in interrupted:
            event = AuditEvent(
                run_id=run_id,
                timestamp=datetime.now(timezone.utc),
                stage="ingestion",
                event="failed",
                message="Processing interrupted before result persistence",
                error_code="processing_interrupted",
            )
            self.save(
                ProcessingResult(
                    run_id=run_id,
                    processing_status="failed",
                    documents=(),
                    classifications=(),
                    data=None,
                    issues=(),
                    needs_review=False,
                    audit=(event,),
                )
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, run_id, paths):
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
        with self.connection() as db:
            db.execute("INSERT INTO runs(run_id) VALUES (?)", (run_id,))
            db.executemany("INSERT INTO inputs VALUES (?,?,?,?)", rows)

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

    def save(self, result):
        with self.connection() as db:
            row = db.execute(
                "SELECT snapshot FROM runs WHERE run_id=?", (result.run_id,)
            ).fetchone()
            if row is None:
                raise StorageIntegrityError("Unregistered run")
            old = []
            if row[0]:
                previous = self.get(result.run_id)
                if previous.model_dump(
                    exclude={"audit", "generated_document", "generated_report"}
                ) != result.model_dump(
                    exclude={"audit", "generated_document", "generated_report"}
                ):
                    raise StorageIntegrityError("Processed data is immutable")
                old = list(previous.audit)
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
