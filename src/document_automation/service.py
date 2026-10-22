"""Local application boundary. Only registered uploads reach the pipeline."""

import asyncio
import base64
import binascii
import os
import re
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from document_automation.acceptance import AcceptanceStatus
from document_automation.adapters.openai_extractor import (
    DEFAULT_BASE_URL,
    OpenAIExtractor,
)
from document_automation.artifact_files import (
    FIELDS,
    ArtifactFiles,
    ArtifactProblem,
    ArtifactStatus,
    ArtifactWrite,
    verifies,
)
from document_automation.data_lease import DataLease, data_operation
from document_automation.extraction import (
    DemoExtractor,
    ExtractionLimits,
    process_paths,
)
from document_automation.generation import (
    generate_document,
    generate_verification_report,
)
from document_automation.llm_usage import TariffCatalog
from document_automation.metering import AttemptJournal, run_meter
from document_automation.operations import audit_operation
from document_automation.run_requests import (
    OperationStatus,
    PendingRun,
    RunRequest,
    Submission,
    content_fingerprint,
    key_hash,
    request_fingerprint,
)
from document_automation.schemas import AuditEvent, ProcessingResult, result_fingerprint
from document_automation.storage import (
    RequestConflictError,
    ResultStore,
    StorageIntegrityError,
)


class ServiceError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def configured_extractor(contract_version="1.0"):
    mode = os.environ.get("DAA_MODE", "demo")
    if mode == "demo":
        if contract_version == "2.0":
            from document_automation.extraction_v2 import DemoExtractorV2

            return DemoExtractorV2()
        return DemoExtractor()
    if mode != "openai":
        raise ValueError("DAA_MODE must be demo or openai")
    return OpenAIExtractor(
        os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        allow_paid=os.environ.get("DAA_ALLOW_PAID", "").lower() == "true",
        base_url=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
        contract_version=contract_version,
    )


class DocumentService:
    def __init__(
        self, root: Path, extractor=None, contract_version="2.0", tariffs=None
    ):
        if contract_version not in ("1.0", "2.0"):
            raise ValueError("Unsupported work_request contract version")
        self.contract_version = contract_version
        self.root = root.resolve()
        self.extractor = extractor or configured_extractor(contract_version)
        self.limits = ExtractionLimits.model_validate_json(
            os.environ.get("DAA_EXTRACTION_LIMITS", "{}")
        )
        self.tariffs = (
            tariffs
            if tariffs is not None
            else TariffCatalog.model_validate_json(
                os.environ.get("DAA_LLM_TARIFFS", "{}")
            )
        )
        with DataLease(self.root):
            self.root.mkdir(parents=True, exist_ok=True)
            self.store = ResultStore(self.root)
        self.lock = asyncio.Lock()

    def get(self, run_id):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ServiceError("run_not_found", "Запуск не найден.", 404)
        try:
            return self.store.get(run_id)
        except KeyError:
            raise ServiceError("run_not_found", "Запуск не найден.", 404) from None

    def usage(self, run_id):
        self.get(run_id)
        return self.store.usage_summary(run_id)

    def business_metrics(self):
        from document_automation.business_metrics import summarize_business

        return summarize_business(self.store)

    def acceptance(self, run_id):
        submission = self.lookup(run_id)
        if submission.status_code == 202:
            return AcceptanceStatus(
                run_id=run_id,
                accepted=False,
                eligible=False,
                block_code="acceptance_not_completed",
                history=(),
            )
        result = submission.body
        events = self.store.acceptance_history(result)
        code = None
        if result.processing_status != "completed":
            code = "acceptance_not_completed"
        elif any(
            self._artifact_info(result, kind)[2].state != "verified" for kind in FIELDS
        ):
            code = "acceptance_artifacts_required"
        return AcceptanceStatus(
            run_id=run_id,
            accepted=bool(events and events[-1].action == "accepted"),
            eligible=code is None,
            block_code=code,
            history=events,
        )

    @data_operation
    async def accept(self, run_id, remarks_reviewed=False):
        async with self.lock:
            status = self.acceptance(run_id)
            if not status.eligible:
                raise ServiceError(
                    status.block_code,
                    "Принятие возможно только после завершения обработки и проверки обоих DOCX.",
                    409,
                )
            result = self.get(run_id)
            if result.needs_review and not remarks_reviewed:
                raise ServiceError(
                    "acceptance_review_required",
                    "Подтвердите, что замечания needs_review рассмотрены.",
                    409,
                )
            self.store.record_acceptance(result, "accepted", remarks_reviewed)
            return self.acceptance(run_id)

    @data_operation
    async def revoke_acceptance(self, run_id):
        async with self.lock:
            self.store.record_acceptance(self.get(run_id), "revoked")
            return self.acceptance(run_id)

    def lookup(self, run_id):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ServiceError("run_not_found", "Запуск не найден.", 404)
        request = self.store.get_request(run_id)
        if request and request.state in ("registered", "processing"):
            return Submission(
                status_code=202,
                replayed=True,
                body=PendingRun(run_id=run_id, operation_status=request.state),
            )
        return Submission(status_code=200, replayed=True, body=self.get(run_id))

    def operation(self, run_id):
        submission = self.lookup(run_id)
        request = self.store.get_request(run_id)
        allowed, reason = self.store.retry_eligibility(run_id)
        return OperationStatus(
            run_id=run_id,
            state=request.state if request else "finished",
            contract_version=request.contract_version
            if request
            else submission.body.contract_version,
            retry_of=request.retry_of if request else None,
            can_retry=allowed,
            retry_block_reason=reason,
        )

    async def process(self, uploads, idempotency_key=None, retry_of=None):
        return (await self.submit(uploads, idempotency_key, retry_of)).body

    @data_operation
    async def submit(self, uploads, idempotency_key=None, retry_of=None):
        try:
            hashed_key = key_hash(idempotency_key)
        except ValueError:
            raise ServiceError(
                "idempotency_key_invalid",
                "Недопустимый Idempotency-Key: используйте 1–128 символов A–Z, a–z, 0–9, . _ : -.",
                422,
            ) from None
        if retry_of is not None and (
            hashed_key is None or not re.fullmatch(r"[0-9a-f]{32}", retry_of)
        ):
            raise ServiceError(
                "retry_invalid",
                "Для явного повтора нужны новый ключ и корректный retry_of.",
                422,
            )
        if not 1 <= len(uploads) <= 10:
            raise ServiceError("file_count", "Загрузите от 1 до 10 файлов.")
        decoded = []
        total = 0
        for index, file in enumerate(uploads):
            name = file["filename"]
            if not re.fullmatch(r"[\w ()\-.]{1,120}", name) or name.startswith("."):
                raise ServiceError("filename", "Недопустимое имя файла.")
            suffix = Path(name).suffix.lower()
            if suffix not in (".pdf", ".docx", ".xlsx"):
                raise ServiceError("format", "Поддерживаются PDF, DOCX и XLSX.")
            try:
                content = base64.b64decode(file["content_base64"], validate=True)
            except (binascii.Error, ValueError):
                raise ServiceError(
                    "encoding", "Некорректное содержимое файла."
                ) from None
            total += len(content)
            if len(content) > 10 * 1024 * 1024 or total > 30 * 1024 * 1024:
                raise ServiceError(
                    "upload_limit", "Лимит: 10 MiB на файл, 30 MiB на запуск.", 413
                )
            decoded.append((name, content))
        decoded.sort(key=lambda file: (file[0], sha256(file[1]).hexdigest()))
        run_id = uuid4().hex
        content_hash = content_fingerprint(decoded)
        operation = RunRequest(
            run_id=run_id,
            key_sha256=hashed_key,
            content_sha256=content_hash,
            request_sha256=request_fingerprint(content_hash, retry_of),
            contract_version=self.contract_version,
            retry_of=retry_of,
            created_at=datetime.now(timezone.utc),
        )
        meter = run_meter(run_id, self.extractor)
        try:
            operation, created = self.store.reserve_request(operation, meter)
        except RequestConflictError as exc:
            messages = {
                "idempotency_conflict": "Этот ключ уже связан с другим запросом. Обработка не запущена повторно.",
                "retry_run_not_found": "Предыдущий запуск не найден.",
                "retry_requires_failed_run": "Явный повтор разрешён только для завершившегося ошибкой запуска.",
                "retry_outcome_unknown": "Исход предыдущего вызова провайдера неизвестен. Повторная отправка заблокирована; сначала необходимо выяснить его состояние.",
            }
            raise ServiceError(exc.code, messages[exc.code], 409) from None
        if not created:
            return self.lookup(operation.run_id)
        try:
            async with self.lock:
                directory = self.root / run_id / "inputs"
                directory.mkdir(parents=True)
                paths = []
                for index, (name, content) in enumerate(decoded):
                    path = directory / f"{index}-{name}"
                    path.write_bytes(content)
                    paths.append(path)
                self.store.register_request_inputs(run_id, paths)
                journal = (
                    AttemptJournal(self.store, meter, self.extractor, self.tariffs)
                    if meter.mode == "llm"
                    else None
                )
                result = await process_paths(
                    tuple(paths),
                    run_id,
                    self.extractor,
                    self.limits,
                    contract_version=self.contract_version,
                    attempt_journal=journal,
                )
                self.store.save(result)
                return Submission(status_code=201, replayed=False, body=result)
        except BaseException as exc:
            try:
                self.store.fail_unfinished_request(run_id, "processing_interrupted")
            except (OSError, sqlite3.Error, StorageIntegrityError):
                # Keep the reservation. Replaying it must never send a new call.
                pass
            if isinstance(
                exc,
                (OSError, sqlite3.Error, StorageIntegrityError, asyncio.CancelledError),
            ):
                raise
            raise ServiceError(
                "processing_failed",
                "Обработка остановлена; успешный результат не подтверждён. Состояние сохранено для повторного запроса с тем же ключом.",
                503,
            ) from None

    def _artifact_info(self, result, kind):
        files = ArtifactFiles(self.root, result.run_id)
        writes = self.store.artifact_writes(result.run_id, kind)
        pending = sum(w.state == "prepared" for w in writes)
        meta = getattr(result, FIELDS[kind])
        data = files.read(meta.filename) if meta else b""
        valid = bool(meta and verifies(result, kind, data))
        leftovers = files.leftovers(kind, writes)
        if valid:
            leftovers = [name for name in leftovers if name != meta.filename]
        if pending or leftovers and (not meta or valid):
            state, code = "recovery_required", "artifact_recovery_required"
        elif meta and not valid:
            state, code = "damaged", "artifact_damaged"
        else:
            state, code = ("verified", None) if valid else ("not_generated", None)
        return (
            files,
            writes,
            ArtifactStatus(
                kind=kind,
                state=state,
                verified=valid,
                pending_operations=pending,
                code=code,
            ),
            data,
        )

    def artifact_status(self, run_id):
        result = self.get(run_id)
        return {kind: self._artifact_info(result, kind)[2] for kind in FIELDS}

    @staticmethod
    def _with_event(result, event):
        return ProcessingResult.model_validate(
            {**result.model_dump(), "audit": result.audit + (event,)}
        )

    def _intent(self, result, kind, meta):
        return ArtifactWrite(
            operation_id=uuid4().hex,
            run_id=result.run_id,
            kind=kind,
            filename=meta.filename,
            result_sha256=result_fingerprint(result),
            expected_sha256=meta.sha256,
            previous_metadata=getattr(result, FIELDS[kind]),
            created_at=datetime.now(timezone.utc),
        )

    def _abandon_artifact(self, result, kind, files, write, protected=None):
        writes = self.store.artifact_writes(result.run_id, kind)
        for name in files.leftovers(kind, writes):
            if name != protected:
                files.isolate(name, write.operation_id)
        event = AuditEvent(
            run_id=result.run_id,
            timestamp=datetime.now(timezone.utc),
            stage="generation",
            event="started",
            message=f"{kind}: explicit recovery; incomplete write abandoned, isolated files are not confirmed",
        )
        result = self._with_event(result, event)
        self.store.save(result, abandon_write=write)
        return result

    def _generate_artifact(self, run_id, kind, recover):
        result = self.get(run_id)
        operation = (
            "create_draft" if kind == "document" else "create_verification_report"
        )
        decision, event = audit_operation(operation, run_id=run_id, result=result)
        if not decision.allowed:
            self.store.save(self._with_event(result, event))
            raise ServiceError(
                "generation_denied" if kind == "document" else "report_denied",
                "Генерация недоступна: нет подтверждённых содержательных данных. Нужны заказчик, название заявки, описание или наименование работы; одного email недостаточно."
                if decision.reason == "no_substantive_content"
                else "Генерация недоступна: обработка не завершена или нет необходимых данных.",
                409,
            )
        self.store.verify_inputs(run_id)
        files, writes, status, _ = self._artifact_info(result, kind)
        meta = getattr(result, FIELDS[kind])
        if status.verified:
            if recover and status.state == "recovery_required":
                pending = [w for w in writes if w.state == "prepared"]
                if not pending:
                    write = self._intent(result, kind, meta)
                    self.store.prepare_artifact(write)
                    pending = [write]
                for write in pending:
                    result = self._abandon_artifact(
                        result, kind, files, write, meta.filename
                    )
            result = self._with_event(result, event)
            self.store.save(result)
            return result
        if not recover and status.state != "not_generated":
            raise ArtifactProblem(
                status.code,
                "DOCX требует явного восстановления. Используйте действие «Восстановить DOCX»; повторная генерация не заменяет существующий файл.",
            )
        if recover:
            for write in writes:
                if write.state == "prepared":
                    result = self._abandon_artifact(result, kind, files, write)
        candidate, data = (
            generate_document(result)
            if kind == "document"
            else generate_verification_report(result)
        )
        meta = getattr(candidate, FIELDS[kind])
        write = self._intent(result, kind, meta)
        self.store.prepare_artifact(write)
        if recover:
            # Keep damaged/legacy bytes; never sweep inputs or the other DOCX.
            for name in files.leftovers(kind, self.store.artifact_writes(run_id, kind)):
                files.isolate(name, write.operation_id)
        files.write_stage(write, data)
        files.validate(candidate, kind, files.stage_name(write), write.expected_sha256)
        files.publish(write)
        files.validate(candidate, kind, write.filename, write.expected_sha256)
        self.store.save(candidate, artifact_write=write)
        return candidate

    @data_operation
    async def generate(self, run_id, recover=False):
        async with self.lock:
            return self._generate_artifact(run_id, "document", recover)

    @data_operation
    async def generate_report(self, run_id, recover=False):
        async with self.lock:
            return self._generate_artifact(run_id, "report", recover)

    def _export_artifact(self, run_id, kind):
        result = self.get(run_id)
        _, _, status, data = self._artifact_info(result, kind)
        decision, event = audit_operation(
            "export_document" if kind == "document" else "export_verification_report",
            run_id=run_id,
            result=result,
            artifact_bytes=data if kind == "document" else None,
            report_bytes=data if kind == "report" else None,
        )
        self.store.save(self._with_event(result, event))
        if not decision.allowed:
            raise ServiceError(
                status.code
                or ("export_denied" if kind == "document" else "report_export_denied"),
                "DOCX недоступен. Сначала сформируйте документ; при незавершённой записи или повреждении используйте явное восстановление DOCX.",
                409,
            )
        return getattr(result, FIELDS[kind]).filename, data

    @data_operation
    async def export(self, run_id):
        async with self.lock:
            return self._export_artifact(run_id, "document")

    @data_operation
    async def export_report(self, run_id):
        async with self.lock:
            return self._export_artifact(run_id, "report")
