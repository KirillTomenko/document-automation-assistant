# Local application interface

Since 2026-10-20 new API runs use work_request/2.0. Files may share formats;
one file may contain multiple supported groups. See [FORMATS_V2.md](FORMATS_V2.md)
for structures and the 27-route matrix. The result carries contract_version=2.0;
historical results without that field retain v1 serialization and validation.
GET /health includes the active contract_version. Stage 7A adds an optional
Idempotency-Key header. Without it, each POST /runs still creates a new run.
With it, an identical request returns the existing run (200), or a pending envelope
(202) while processing; a different request with the same key returns 409.
See [SAFE_RETRIES_AND_IDEMPOTENCY.md](SAFE_RETRIES_AND_IDEMPOTENCY.md).

Run backend with `python -m uvicorn document_automation.api:create_app --factory`.
Run UI with `python -m streamlit run src/document_automation/ui.py`.
Both bind localhost by default. Streamlit calls FastAPI; it does not call business
logic or the LLM provider directly. DAA_API_URL can configure the internal API URL.
Stage 8 holds a local data-directory lease for the backend lifespan. Keep lifespan
enabled (uvicorn default, or --lifespan on). Offline backup requires stopping this
backend; maintenance blocks new application startup/operations on the same data.
CLI instructions: [BACKUP_AND_RECOVERY.md](BACKUP_AND_RECOVERY.md).

POST /runs accepts JSON: files = [{filename, content_base64}]. Request bytes are
bounded before JSON validation (42 MiB); maximum 10 files, 10 MiB each and 30 MiB
decoded total. Upload names cannot contain paths. The application assigns UUID run
directories and checks read_source before parsing registered files.
An explicit retry of a failed run additionally supplies retry_of and a new
Idempotency-Key. Unknown provider outcomes block this action with 409.

GET /runs/{id} returns the validated result, audit and independent artifact metadata.
An active run returns 202 with run_id, operation_status and code=run_in_progress;
it is not a successful business result. GET /runs/{id}/operation returns lifecycle,
retry_of, can_retry and retry_block_reason separately from immutable business data.
GET /runs/{id}/usage returns persistent usage/cost summary separately from that result;
see COST_METRICS.md. Historical runs without metering report unknown, not zero cost.
POST/GET /runs/{id}/document generate and export the work request.
POST/GET /runs/{id}/verification-report generate and export the verification report.
Each export verifies its own artifact and returns bytes with no-store. Both artifacts
use the same saved result and run ID; generation does not rerun extraction. A completed
diagnostic run can produce a report even when it has no substantive work-request data.
Failed runs and unavailable/corrupt outputs return 409.
Stage 7B reuses verified DOCX without rewriting. Interrupted writes and damaged
files require an explicit recovery action; they are never silently replaced.
GET /runs/{id}/artifacts returns independent availability states for document/report.
POST /runs/{id}/document/recover and /verification-report/recover isolate incomplete
or damaged bytes and rebuild the selected artifact from the saved result, without
LLM calls. Permission and source-integrity checks still apply. Existing successful
response shapes remain unchanged. See [ARTIFACT_RECOVERY.md](ARTIFACT_RECOVERY.md).
Unknown run IDs return 404. No edit, arbitrary-path, external-action or publish routes.

This is a single-user local MVP, not an authenticated public service. Do not expose
the API port to an untrusted network. SQLite persistence is described in STORAGE.md.

UI includes upload, automatic-check status (never human approval), values, issues,
candidate evidence, and separate generation and download buttons for each artifact. Bundled A/B/C/V inputs are
available through the demo selector without an API key.

UI labels, field states, issue explanations/actions and source coordinates are
localized in ui_text.py. Technical codes and run identifiers are shown only in
expandable technical details. The verification report carries detailed provenance and
audit; the business-facing request omits those technical details.
Demo options explain A/B/C and the limitation to the documented input structure.

## Этап 9: принятие и показатели

Добавочные маршруты, не меняющие старые ответы `/runs`:

- `GET /business-metrics` — all_time сводка из SQLite; денежные Decimal передаются
  строками, доли от 0 до 1, отсутствующая доля/средняя — null. Известные суммы
  разделены по валютам, тарифы возвращаются с источниками. Mock помечается simulated_runs.
- `GET /runs/{run_id}/acceptance` — accepted, eligible, block_code, history.
- `POST /runs/{run_id}/acceptance` с JSON `{}` или `{"remarks_reviewed": true}` —
  явное решение оператора. 409 `acceptance_not_completed`, `acceptance_artifacts_required`
  или `acceptance_review_required` при невыполненных условиях. Bool строгий.
- `POST /runs/{run_id}/acceptance/revoke` — отдельный явный отзыв без удаления истории.

Повторы одного текущего решения не добавляют событий, возвращают существующее состояние.
При нарушении целостности — безопасный 409 storage_integrity. Ошибка SQLite — 503;
повтор решения безопасен. Данные принимаются вне work_request v1/v2; fingerprints,
provenance, needs_review, разрешения генерации/экспорта не меняются. Новые ответы
имеют Cache-Control: no-store. Формулы: [BUSINESS_METRICS.md](BUSINESS_METRICS.md).
