"""Local application boundary. Only registered uploads reach the pipeline."""

import asyncio
import base64
import binascii
import os
import re
from pathlib import Path
from uuid import uuid4

from document_automation.adapters.openai_extractor import (
    DEFAULT_BASE_URL,
    OpenAIExtractor,
)
from document_automation.extraction import (
    DemoExtractor,
    ExtractionLimits,
    process_paths,
)
from document_automation.generation import (
    OperationDenied,
    generate_document,
    generate_verification_report,
)
from document_automation.operations import audit_operation
from document_automation.schemas import ProcessingResult
from document_automation.storage import ResultStore


class ServiceError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def configured_extractor():
    mode = os.environ.get("DAA_MODE", "demo")
    if mode == "demo":
        return DemoExtractor()
    if mode != "openai":
        raise ValueError("DAA_MODE must be demo or openai")
    return OpenAIExtractor(
        os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        allow_paid=os.environ.get("DAA_ALLOW_PAID", "").lower() == "true",
        base_url=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
    )


class DocumentService:
    def __init__(self, root: Path, extractor=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.extractor = extractor or configured_extractor()
        self.limits = ExtractionLimits.model_validate_json(
            os.environ.get("DAA_EXTRACTION_LIMITS", "{}")
        )
        self.store = ResultStore(self.root)
        self.lock = asyncio.Lock()

    def get(self, run_id):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ServiceError("run_not_found", "Запуск не найден.", 404)
        try:
            return self.store.get(run_id)
        except KeyError:
            raise ServiceError("run_not_found", "Запуск не найден.", 404) from None

    async def process(self, uploads):
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
            decoded.append((f"{index}-{name}", content))
        async with self.lock:
            run_id = uuid4().hex
            directory = self.root / run_id / "inputs"
            directory.mkdir(parents=True)
            paths = []
            for name, content in decoded:
                path = directory / name
                path.write_bytes(content)
                paths.append(path)
            self.store.begin(run_id, paths)
            result = await process_paths(
                tuple(paths), run_id, self.extractor, self.limits
            )
            self.store.save(result)
            return result

    async def generate(self, run_id):
        async with self.lock:
            result = self.get(run_id)
            self.store.verify_inputs(run_id)
            try:
                result, data = generate_document(result)
            except OperationDenied as exc:
                self.store.save(
                    ProcessingResult.model_validate(
                        {**result.model_dump(), "audit": result.audit + (exc.event,)}
                    )
                )
                raise ServiceError(
                    "generation_denied",
                    (
                        "Генерация недоступна: нет подтверждённых содержательных данных. "
                        "Нужны заказчик, название заявки, описание или наименование работы; одного email недостаточно."
                        if exc.event.decision.reason == "no_substantive_content"
                        else "Генерация недоступна: обработка завершилась ошибкой."
                    ),
                    409,
                ) from None
            output = self.root / run_id / result.generated_document.filename
            temporary = output.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(output)
            self.store.save(result)
            return result

    async def export(self, run_id):
        async with self.lock:
            result = self.get(run_id)
            data = b""
            if result.generated_document:
                path = self.root / run_id / result.generated_document.filename
                if path.is_file() and path.stat().st_size <= 10 * 1024 * 1024:
                    data = path.read_bytes()
            decision, event = audit_operation(
                "export_document", run_id=run_id, result=result, artifact_bytes=data
            )
            self.store.save(
                ProcessingResult.model_validate(
                    {**result.model_dump(), "audit": result.audit + (event,)}
                )
            )
            if not decision.allowed:
                raise ServiceError(
                    "export_denied",
                    "Сначала сформируйте DOCX. Повреждённый файл необходимо сформировать повторно.",
                    409,
                )
            return result.generated_document.filename, data

    async def generate_report(self, run_id):
        async with self.lock:
            result = self.get(run_id)
            self.store.verify_inputs(run_id)
            try:
                result, data = generate_verification_report(result)
            except OperationDenied as exc:
                self.store.save(
                    ProcessingResult.model_validate(
                        {**result.model_dump(), "audit": result.audit + (exc.event,)}
                    )
                )
                raise ServiceError(
                    "report_denied",
                    "Отчёт проверки недоступен: обработка не завершена или нет диагностических данных.",
                    409,
                ) from None
            output = self.root / run_id / result.generated_report.filename
            temporary = output.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(output)
            self.store.save(result)
            return result

    async def export_report(self, run_id):
        async with self.lock:
            result = self.get(run_id)
            data = b""
            if result.generated_report:
                path = self.root / run_id / result.generated_report.filename
                if path.is_file() and path.stat().st_size <= 10 * 1024 * 1024:
                    data = path.read_bytes()
            decision, event = audit_operation(
                "export_verification_report",
                run_id=run_id,
                result=result,
                report_bytes=data,
            )
            self.store.save(
                ProcessingResult.model_validate(
                    {**result.model_dump(), "audit": result.audit + (event,)}
                )
            )
            if not decision.allowed:
                raise ServiceError(
                    "report_export_denied",
                    "Сначала сформируйте отчёт проверки или повторите его формирование.",
                    409,
                )
            return result.generated_report.filename, data
