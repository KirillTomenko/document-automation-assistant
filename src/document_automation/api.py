"""Local-only FastAPI surface; no arbitrary paths, URLs, policies or result edits."""

import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, ValidationError

from document_automation.acceptance import AcceptanceAction
from document_automation.artifact_files import ArtifactProblem
from document_automation.contracts import ContractModel
from document_automation.data_lease import DataBusyError, DataLease
from document_automation.service import DocumentService, ServiceError
from document_automation.storage import StorageIntegrityError


class Upload(ContractModel):
    filename: str = Field(min_length=1, max_length=120)
    content_base64: str = Field(max_length=14 * 1024 * 1024)


class UploadBatch(ContractModel):
    files: tuple[Upload, ...] = Field(min_length=1, max_length=10)
    retry_of: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


def create_app(root=None, extractor=None, contract_version="2.0", tariffs=None):
    @asynccontextmanager
    async def lifespan(app):
        with DataLease(service.root, "backend"):
            yield

    app = FastAPI(
        title="Document Automation Assistant", version="2.0.0", lifespan=lifespan
    )
    service = DocumentService(
        Path(root or os.environ.get("DAA_DATA_DIR", "data")),
        extractor,
        contract_version,
        tariffs,
    )
    app.state.service = service

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse(
            {"code": exc.code, "detail": str(exc)}, status_code=exc.status
        )

    @app.exception_handler(DataBusyError)
    async def data_busy(request, exc):
        return JSONResponse(
            {
                "code": "data_busy",
                "detail": "Каталог занят обслуживанием. Операция не запущена.",
            },
            status_code=503,
        )

    @app.exception_handler(ArtifactProblem)
    async def artifact_error(request, exc):
        return JSONResponse({"code": exc.code, "detail": str(exc)}, status_code=409)

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        return JSONResponse(
            {
                "code": "storage_error",
                "detail": "Не удалось прочитать или сохранить локальные данные.",
            },
            status_code=503,
        )

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        return JSONResponse(
            {
                "code": "storage_unavailable",
                "detail": "Локальное хранилище недоступно. Обработка остановлена; повторяйте исходный запрос с тем же ключом.",
            },
            status_code=503,
        )

    def submission_response(submission):
        headers = {
            "Cache-Control": "no-store",
            "Location": f"/runs/{submission.body.run_id}",
        }
        if submission.replayed:
            headers["Idempotency-Replayed"] = "true"
        return JSONResponse(
            submission.body.model_dump(mode="json"),
            status_code=submission.status_code,
            headers=headers,
        )

    @app.exception_handler(StorageIntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse(
            {
                "code": "storage_integrity",
                "detail": "Нарушена целостность локальных данных. Операция остановлена; повторная отправка провайдеру не выполняется.",
            },
            status_code=409,
        )

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "mode": service.extractor.name,
            "contract_version": service.contract_version,
        }

    @app.post("/runs", status_code=201)
    async def process(request: Request):
        keys = request.headers.getlist("Idempotency-Key")
        if len(keys) > 1:
            raise ServiceError(
                "idempotency_key_invalid", "Передайте только один Idempotency-Key.", 422
            )
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
        return submission_response(
            await service.submit(
                [f.model_dump() for f in batch.files],
                keys[0] if keys else None,
                batch.retry_of,
            )
        )

    @app.get("/business-metrics")
    async def business_metrics(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return service.business_metrics()

    @app.get("/runs/{run_id}/acceptance")
    async def acceptance(run_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return service.acceptance(run_id)

    @app.post("/runs/{run_id}/acceptance")
    async def accept(run_id: str, body: AcceptanceAction, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await service.accept(run_id, body.remarks_reviewed)

    @app.post("/runs/{run_id}/acceptance/revoke")
    async def revoke_acceptance(run_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await service.revoke_acceptance(run_id)

    @app.get("/runs/{run_id}")
    async def get(run_id: str):
        return submission_response(service.lookup(run_id))

    @app.get("/runs/{run_id}/operation")
    async def operation(run_id: str):
        return service.operation(run_id)

    @app.get("/runs/{run_id}/usage")
    async def usage(run_id: str):
        return service.usage(run_id)

    @app.get("/runs/{run_id}/artifacts")
    async def artifacts(run_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return service.artifact_status(run_id)

    @app.post("/runs/{run_id}/document/recover")
    async def recover_document(run_id: str):
        return await service.generate(run_id, recover=True)

    @app.post("/runs/{run_id}/verification-report/recover")
    async def recover_report(run_id: str):
        return await service.generate_report(run_id, recover=True)

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
