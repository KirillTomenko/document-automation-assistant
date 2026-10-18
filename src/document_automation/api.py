"""Local-only FastAPI surface; no arbitrary paths, URLs, policies or result edits."""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, ValidationError

from document_automation.contracts import ContractModel
from document_automation.service import DocumentService, ServiceError
from document_automation.storage import StorageIntegrityError


class Upload(ContractModel):
    filename: str = Field(min_length=1, max_length=120)
    content_base64: str = Field(max_length=14 * 1024 * 1024)


class UploadBatch(ContractModel):
    files: tuple[Upload, ...] = Field(min_length=1, max_length=10)


def create_app(root=None, extractor=None):
    app = FastAPI(title="Document Automation Assistant", version="0.1.0")
    service = DocumentService(
        Path(root or os.environ.get("DAA_DATA_DIR", "data")), extractor
    )
    app.state.service = service

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse(
            {"code": exc.code, "detail": str(exc)}, status_code=exc.status
        )

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        return JSONResponse(
            {
                "code": "storage_error",
                "detail": "Не удалось прочитать или сохранить локальные данные.",
            },
            status_code=503,
        )

    @app.exception_handler(StorageIntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse(
            {
                "code": "storage_integrity",
                "detail": "Нарушена целостность локальных данных. Создайте новый запуск из исходных файлов.",
            },
            status_code=409,
        )

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": service.extractor.name}

    @app.post("/runs", status_code=201)
    async def process(request: Request):
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 42 * 1024 * 1024:
                raise ServiceError("upload_limit", "Превышен размер запроса.", 413)
        try:
            batch = UploadBatch.model_validate_json(body)
        except ValidationError:
            raise ServiceError(
                "invalid_request",
                "Ожидается JSON с файлами: filename и content_base64.",
                422,
            ) from None
        return await service.process([f.model_dump() for f in batch.files])

    @app.get("/runs/{run_id}")
    async def get(run_id: str):
        return service.get(run_id)

    @app.post("/runs/{run_id}/document")
    async def generate(run_id: str):
        return await service.generate(run_id)

    @app.get("/runs/{run_id}/document")
    async def export(run_id: str):
        filename, data = await service.export(run_id)
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/runs/{run_id}/verification-report")
    async def generate_report(run_id: str):
        return await service.generate_report(run_id)

    @app.get("/runs/{run_id}/verification-report")
    async def export_report(run_id: str):
        filename, data = await service.export_report(run_id)
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    return app
