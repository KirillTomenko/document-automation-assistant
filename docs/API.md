# Local application interface

Run backend with `python -m uvicorn document_automation.api:create_app --factory`.
Run UI with `python -m streamlit run src/document_automation/ui.py`.
Both bind localhost by default. Streamlit calls FastAPI; it does not call business
logic or the LLM provider directly. DAA_API_URL can configure the internal API URL.

POST /runs accepts JSON: files = [{filename, content_base64}]. Request bytes are
bounded before JSON validation (42 MiB); maximum 10 files, 10 MiB each and 30 MiB
decoded total. Upload names cannot contain paths. The application assigns UUID run
directories and checks read_source before parsing registered files.

GET /runs/{id} returns the validated result, audit and independent artifact metadata.
POST/GET /runs/{id}/document generate and export the work request.
POST/GET /runs/{id}/verification-report generate and export the verification report.
Each export verifies its own artifact and returns bytes with no-store. Both artifacts
use the same saved result and run ID; generation does not rerun extraction. A completed
diagnostic run can produce a report even when it has no substantive work-request data.
Failed runs and unavailable/corrupt outputs return 409.
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
