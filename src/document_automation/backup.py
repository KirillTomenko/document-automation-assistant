"""Offline backup, independent verification and restore of the local MVP data."""

import argparse
import json
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError, field_validator

from document_automation.artifact_files import (
    FIELDS,
    NAMES,
    ArtifactFiles,
    ArtifactProblem,
    verifies,
)
from document_automation.backup_paths import (
    BackupError,
    check_tree,
    checksum,
    copy_file,
    directory,
    file_path,
    new_target,
    publish,
    relative_path,
)
from document_automation.contracts import ContractModel, Sha256
from document_automation.data_lease import DataBusyError, DataLease
from document_automation.run_requests import RunId
from document_automation.schemas import result_fingerprint
from document_automation.storage import ResultStore, StorageIntegrityError

DATABASE = "workflow.sqlite3"
MANIFEST = "manifest.json"
MANIFEST_HASH = "manifest.sha256"
SCHEMA = {
    "runs": ("run_id", "snapshot", "digest"),
    "inputs": ("run_id", "source_id", "path", "sha256"),
    "documents": ("run_id", "document_id", "metadata"),
    "events": ("run_id", "sequence", "event"),
    "artifacts": ("run_id", "metadata"),
    "report_artifacts": ("run_id", "metadata"),
    "llm_runs": ("run_id", "record", "digest"),
    "llm_attempts": ("run_id", "attempt_no", "record", "digest"),
    "run_requests": ("run_id", "key_sha256", "retry_of", "record", "digest"),
    "artifact_writes": ("operation_id", "run_id", "kind", "state", "record", "digest"),
    "acceptance_events": ("run_id", "sequence", "record", "digest"),
}
PRIMARY_KEYS = {
    **{name: ("run_id",) for name in SCHEMA},
    "inputs": ("run_id", "source_id"),
    "documents": ("run_id", "document_id"),
    "events": ("run_id", "sequence"),
    "llm_attempts": ("run_id", "attempt_no"),
    "artifact_writes": ("operation_id",),
    "acceptance_events": ("run_id", "sequence"),
}


class BackupFile(ContractModel):
    path: str
    role: Literal["database", "input", "document", "report"]
    run_id: RunId | None = None
    size: int = Field(ge=0)
    sha256: Sha256


class BackupResult(ContractModel):
    run_id: RunId
    contract_version: Literal["1.0", "2.0"]
    result_sha256: Sha256
    snapshot_sha256: Sha256


class BackupManifest(ContractModel):
    format_version: Literal[1]
    scope: Literal["registered-inputs-and-confirmed-artifacts"]
    created_at: AwareDatetime
    files: tuple[BackupFile, ...]
    results: tuple[BackupResult, ...]

    @field_validator("format_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported backup format version")
        return value


class BackupReport(ContractModel):
    format_version: Literal[1] = 1
    runs: int
    files: int
    inputs: int
    documents: int
    reports: int
    contracts: tuple[str, ...]


class ReadOnlyStore(ResultStore):
    """Reuse domain validators, never run DDL, migrations or startup recovery."""

    def __init__(self, root):
        self.root, self.path = root, root / DATABASE

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            yield db
        finally:
            db.close()


def _schema(db):
    rows = db.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
    tables = {name for kind, name, _ in rows if kind == "table"}
    if tables not in (set(SCHEMA), set(SCHEMA) - {"acceptance_events"}) or any(
        kind not in ("table", "index")
        or kind == "index"
        and sql is not None
        and name != "one_pending_artifact_write"
        or kind == "table"
        and not sql.lstrip().upper().startswith("CREATE TABLE ")
        for kind, name, sql in rows
    ):
        raise BackupError(
            "unsupported_schema",
            "Нужна штатная схема приложения без посторонних таблиц, views и triggers.",
        )
    for table, columns in SCHEMA.items():
        if table not in tables:
            continue  # Compatible Stage 8 backup, before operator acceptance.
        info = db.execute(f"PRAGMA table_xinfo({table})").fetchall()
        if tuple(row[1] for row in info) != columns or any(
            row[-1] != 0 for row in info
        ):
            raise BackupError(
                "unsupported_schema",
                "Структура SQLite не соответствует поддерживаемой версии.",
            )
        if (
            tuple(row[1] for row in sorted(info, key=lambda r: r[5]) if row[5])
            != PRIMARY_KEYS[table]
        ):
            raise BackupError(
                "unsupported_schema",
                "Первичные ключи SQLite не соответствуют версии приложения.",
            )
        nullable = {
            ("runs", "snapshot"),
            ("runs", "digest"),
            ("run_requests", "key_sha256"),
            ("run_requests", "retry_of"),
        }
        for column in columns:
            kind = "integer" if column in ("sequence", "attempt_no") else "text"
            condition = f"typeof({column})!='{kind}'"
            if (table, column) in nullable:
                condition = f"{column} IS NOT NULL AND ({condition})"
            if db.execute(
                f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1"
            ).fetchone():
                raise BackupError(
                    "record_type_invalid", "Некорректный тип данных SQLite."
                )
    if (
        db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
        or db.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise BackupError(
            "sqlite_integrity",
            "SQLite не прошла проверку целостности или внешних ключей.",
        )


def _inspect(root, check_files=True):
    file_path(root, DATABASE)
    if any(
        (root / (DATABASE + suffix)).exists()
        or (root / (DATABASE + suffix)).is_symlink()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise BackupError(
            "sqlite_sidecars_present",
            "Обнаружены SQLite WAL/journal-файлы. Нужны проверка восстановления и корректная остановка приложения; не удаляйте их вручную.",
        )
    store = ReadOnlyStore(root)
    with store.connection() as db:
        _schema(db)
        rows = db.execute(
            "SELECT run_id,snapshot,digest FROM runs ORDER BY run_id"
        ).fetchall()
        run_ids = {row[0] for row in rows}
        for table in SCHEMA.keys() - {"runs"}:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                continue
            owners = {
                row[0] for row in db.execute(f"SELECT DISTINCT run_id FROM {table}")
            }
            if not owners <= run_ids:
                raise BackupError(
                    "orphan_record", "Обнаружены записи без соответствующего запуска."
                )
        inputs = db.execute(
            "SELECT run_id,source_id,path,sha256 FROM inputs"
        ).fetchall()
        if db.execute(
            "SELECT 1 FROM artifact_writes WHERE kind NOT IN ('document','report') OR state NOT IN ('prepared','committed','abandoned') LIMIT 1"
        ).fetchone():
            raise BackupError(
                "artifact_inconsistent",
                "Неизвестное состояние журнала DOCX; требуется диагностика.",
            )
        if db.execute(
            "SELECT 1 FROM run_requests WHERE key_sha256 IS NOT NULL GROUP BY key_sha256 HAVING count(*)>1 LIMIT 1"
        ).fetchone():
            raise BackupError(
                "request_inconsistent",
                "Ключ идемпотентности связан с несколькими запусками.",
            )
    files, results = {}, []

    def register(name, run_id, role, digest):
        relative_path(name)
        if name.casefold() in {p.casefold() for p in files} or not re.fullmatch(
            "[0-9a-f]{64}", digest
        ):
            raise BackupError(
                "file_registration_invalid",
                "Некорректная или повторная регистрация файла.",
            )
        files[name] = (run_id, role, digest)

    for run_id, _, raw, digest in inputs:
        name = raw.replace("\\", "/")
        parts = relative_path(name).parts
        if (
            len(parts) != 3
            or parts[:2] != (run_id, "inputs")
            or Path(name).suffix.lower() not in (".pdf", ".docx", ".xlsx")
        ):
            raise BackupError(
                "file_registration_invalid",
                "Источник должен быть зарегистрирован внутри inputs своего запуска.",
            )
        register(name, run_id, "input", digest)
    for run_id, snapshot, digest in rows:
        if not re.fullmatch("[0-9a-f]{32}", run_id) or snapshot is None:
            raise BackupError(
                "recovery_required",
                "Есть незавершённая обработка. Сначала восстановите состояние приложения и остановите backend.",
            )
        result = store.get(run_id)
        store.acceptance_history(result)
        fingerprint = result_fingerprint(result)
        results.append(
            BackupResult(
                run_id=run_id,
                contract_version=result.contract_version,
                result_sha256=fingerprint,
                snapshot_sha256=digest,
            )
        )
        request = store.get_request(run_id)
        if request and (
            request.state in ("registered", "processing")
            or request.contract_version != result.contract_version
        ):
            raise BackupError(
                "recovery_required",
                "Состояние запроса требует восстановления до backup.",
            )
        if request and request.retry_of and request.retry_of not in run_ids:
            raise BackupError("orphan_record", "Нарушена связь повторного запуска.")
        meter, attempts = store.get_meter(run_id), store.get_attempts(run_id)
        if any(a.status == "started" for a in attempts):
            raise BackupError(
                "recovery_required",
                "Есть незавершённая LLM-попытка; требуется восстановление состояния.",
            )
        if attempts and (
            meter is None
            or meter.mode != "llm"
            or any(
                (a.provider, a.model) != (meter.provider, meter.model) for a in attempts
            )
        ):
            raise BackupError(
                "ledger_inconsistent", "Журнал LLM-попыток не согласован с запуском."
            )
        if (
            any(a.status == "interrupted" for a in attempts)
            and result.processing_status != "failed"
        ):
            raise BackupError(
                "ledger_inconsistent",
                "Прерванная попытка не согласована с результатом.",
            )
        registered = {
            (Path(raw.replace("\\", "/")).name, sha)
            for owner, _, raw, sha in inputs
            if owner == run_id
        }
        if any((d.filename, d.sha256) not in registered for d in result.documents):
            raise BackupError(
                "file_registration_invalid",
                "Метаданные источников не соответствуют регистрации.",
            )
        for kind, field in FIELDS.items():
            writes = store.artifact_writes(run_id, kind)
            if any(w.state == "prepared" for w in writes):
                raise BackupError(
                    "recovery_required",
                    "Есть незавершённый выпуск DOCX. Выполните явное восстановление этапа 7B.",
                )
            if any(w.result_sha256 != fingerprint for w in writes):
                raise BackupError(
                    "artifact_inconsistent",
                    "Журнал DOCX относится к другому результату.",
                )
            meta = getattr(result, field)
            committed = [w for w in writes if w.state == "committed"]
            if committed and (
                meta is None
                or (committed[-1].filename, committed[-1].expected_sha256)
                != (meta.filename, meta.sha256)
            ):
                raise BackupError(
                    "artifact_inconsistent",
                    "Подтверждённый DOCX не соответствует журналу.",
                )
            if meta:
                register(f"{run_id}/{meta.filename}", run_id, kind, meta.sha256)
            if check_files:
                if meta:
                    path = file_path(root, f"{run_id}/{meta.filename}")
                    if path.stat().st_size > 10 * 1024 * 1024 or not verifies(
                        result, kind, path.read_bytes()
                    ):
                        raise BackupError(
                            "artifact_damaged",
                            "Подтверждённый DOCX повреждён. Требуется восстановление этапа 7B.",
                        )
                if (root / run_id).exists():
                    leftovers = ArtifactFiles(root, run_id).leftovers(kind, writes)
                    if any(
                        name != (meta.filename if meta else None) for name in leftovers
                    ):
                        raise BackupError(
                            "recovery_required",
                            "Остались неподтверждённые DOCX или временные файлы; выполните восстановление этапа 7B.",
                        )
    if check_files:
        for name, (_, _, expected) in files.items():
            size, actual = checksum(file_path(root, name))
            if size > 10 * 1024 * 1024 or actual != expected:
                raise BackupError(
                    "file_checksum_mismatch",
                    "Зарегистрированный файл отсутствует, повреждён или превышает лимит.",
                )
    return files, tuple(results)


def _read_manifest(root):
    path, seal = file_path(root, MANIFEST), file_path(root, MANIFEST_HASH)
    if path.stat().st_size > 16 * 1024 * 1024 or seal.stat().st_size != 65:
        raise BackupError(
            "manifest_invalid", "Неверный размер manifest или его контрольной суммы."
        )
    data = path.read_bytes()
    if seal.read_bytes() != (sha256(data).hexdigest() + "\n").encode("ascii"):
        raise BackupError(
            "manifest_checksum_mismatch", "Manifest изменён или повреждён."
        )
    manifest = BackupManifest.model_validate_json(data)
    names = [entry.path.casefold() for entry in manifest.files]
    if len(names) != len(set(names)) or len(
        {r.run_id for r in manifest.results}
    ) != len(manifest.results):
        raise BackupError("manifest_invalid", "Manifest содержит повторные записи.")
    for entry in manifest.files:
        parts = relative_path(entry.path).parts
        if entry.role == "database":
            valid = entry.path == DATABASE and entry.run_id is None
        elif entry.role == "input":
            valid = (
                len(parts) == 3
                and parts[:2] == (entry.run_id, "inputs")
                and Path(entry.path).suffix.lower() in (".pdf", ".docx", ".xlsx")
            )
        else:
            valid = (
                len(parts) == 2
                and parts[0] == entry.run_id
                and parts[1] in NAMES[entry.role]
            )
        if (
            not valid
            or entry.run_id is not None
            and entry.run_id not in {r.run_id for r in manifest.results}
        ):
            raise BackupError(
                "manifest_inventory_mismatch",
                "Путь или назначение файла не соответствует формату копии.",
            )
    return manifest


def _verify(root):
    manifest = _read_manifest(root)
    check_tree(
        root, {MANIFEST, MANIFEST_HASH, *(entry.path for entry in manifest.files)}
    )
    for entry in manifest.files:
        if checksum(file_path(root, entry.path)) != (entry.size, entry.sha256):
            raise BackupError(
                "backup_checksum_mismatch",
                "Размер или SHA-256 файла резервной копии не совпадает.",
            )
    files, results = _inspect(root)
    if manifest.results != results or {e.path for e in manifest.files} != {
        DATABASE,
        *files,
    }:
        raise BackupError(
            "manifest_inventory_mismatch",
            "Manifest не соответствует результатам и зарегистрированным файлам SQLite.",
        )
    for entry in manifest.files:
        if entry.path == DATABASE:
            expected = (None, "database", entry.sha256)
        else:
            expected = files[entry.path]
        if (entry.run_id, entry.role, entry.sha256) != expected:
            raise BackupError(
                "manifest_inventory_mismatch",
                "Назначение файла или SHA-256 не соответствует SQLite.",
            )
    return manifest


def _report(manifest):
    return BackupReport(
        runs=len(manifest.results),
        files=len(manifest.files),
        inputs=sum(e.role == "input" for e in manifest.files),
        documents=sum(e.role == "document" for e in manifest.files),
        reports=sum(e.role == "report" for e in manifest.files),
        contracts=tuple(sorted({r.contract_version for r in manifest.results})),
    )


def verify_backup(backup):
    root = directory(backup)
    with DataLease(root, "maintenance"):
        return _report(_verify(root))


def _separate(source, target):
    if (
        source == target
        or source.is_relative_to(target)
        or target.is_relative_to(source)
    ):
        raise BackupError(
            "paths_overlap", "Исходный и целевой каталоги должны быть раздельными."
        )


def _sqlite_backup(source, target):
    target.touch(exist_ok=False)
    with ReadOnlyStore(source).connection() as incoming:
        outgoing = sqlite3.connect(target)
        try:
            incoming.backup(outgoing)
        finally:
            outgoing.close()


def backup(data, output):
    source, target = directory(data), new_target(output)
    _separate(source, target)
    with DataLease(source, "maintenance"), DataLease(target, "maintenance"):
        files, results = _inspect(source)
        original_db = checksum(file_path(source, DATABASE))
        stage = Path(
            tempfile.mkdtemp(prefix=f".{target.name}-staging-", dir=target.parent)
        )
        _sqlite_backup(source, stage / DATABASE)
        snapshot_files, snapshot_results = _inspect(stage, check_files=False)
        if (files, results) != (snapshot_files, snapshot_results):
            raise BackupError(
                "source_changed", "SQLite изменилась во время копирования."
            )
        for name in files:
            copy_file(
                file_path(source, name), stage.joinpath(*relative_path(name).parts)
            )
        entries = []
        for name in sorted({DATABASE, *files}):
            size, digest = checksum(file_path(stage, name))
            owner, role, _ = files.get(name, (None, "database", None))
            entries.append(
                BackupFile(path=name, run_id=owner, role=role, size=size, sha256=digest)
            )
        manifest = BackupManifest(
            format_version=1,
            scope="registered-inputs-and-confirmed-artifacts",
            created_at=datetime.now(timezone.utc),
            files=tuple(entries),
            results=results,
        )
        text = manifest.model_dump_json(indent=2).encode("utf-8") + b"\n"
        (stage / MANIFEST).write_bytes(text)
        (stage / MANIFEST_HASH).write_bytes(
            (sha256(text).hexdigest() + "\n").encode("ascii")
        )
        _verify(stage)
        if (
            _inspect(source) != (files, results)
            or checksum(file_path(source, DATABASE)) != original_db
        ):
            raise BackupError(
                "source_changed", "Исходные данные изменились; копия не опубликована."
            )
        publish(stage, target)
        return _report(manifest)


def restore(backup_directory, target_directory):
    source, target = directory(backup_directory), new_target(target_directory)
    _separate(source, target)
    with DataLease(source, "maintenance"), DataLease(target, "maintenance"):
        manifest = _verify(source)
        stage = Path(
            tempfile.mkdtemp(prefix=f".{target.name}-staging-", dir=target.parent)
        )
        for entry in manifest.files:
            copy_file(
                file_path(source, entry.path),
                stage.joinpath(*relative_path(entry.path).parts),
            )
        check_tree(stage, {e.path for e in manifest.files})
        for entry in manifest.files:
            if checksum(file_path(stage, entry.path)) != (entry.size, entry.sha256):
                raise BackupError(
                    "restore_checksum_mismatch",
                    "Восстановленные байты не совпадают с копией.",
                )
        files, results = _inspect(stage)
        if results != manifest.results or set(files) != {
            e.path for e in manifest.files if e.role != "database"
        }:
            raise BackupError(
                "restore_inconsistent", "Проверка восстановленных данных не пройдена."
            )
        _verify(source)
        publish(stage, target)
        return _report(manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup", help="backend must be stopped")
    create.add_argument("--data", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-backup")
    verify.add_argument("--backup", type=Path, required=True)
    recover = commands.add_parser("restore", help="target must not exist")
    recover.add_argument("--backup", type=Path, required=True)
    recover.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            report = backup(args.data, args.output)
        elif args.command == "verify-backup":
            report = verify_backup(args.backup)
        else:
            report = restore(args.backup, args.target)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "command": args.command,
                    **report.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (
        BackupError,
        DataBusyError,
        OSError,
        sqlite3.Error,
        StorageIntegrityError,
        ValidationError,
        ArtifactProblem,
    ) as exc:
        if isinstance(exc, BackupError):
            code, detail = exc.code, str(exc)
        elif isinstance(exc, DataBusyError):
            code, detail = (
                "data_busy",
                "Остановите backend и дождитесь завершения операций. Данные заняты.",
            )
        else:
            code, detail = (
                "backup_validation_failed",
                "Операция остановлена: данные недоступны, повреждены или не прошли проверку. Сначала проверьте восстановление приложения.",
            )
        print(
            json.dumps(
                {"status": "error", "code": code, "detail": detail}, ensure_ascii=False
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
