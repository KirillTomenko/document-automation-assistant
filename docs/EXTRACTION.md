# Extraction boundary

The description below retains the historical v1 route. New backend runs use
[work_request/2.0](DEMO_CONTRACT_V2.md): DemoExtractorV2 and ExtractionV2 share
application-detected labeled fields and finite tables in every supported format.
Classification may include several roles per file. Item claims cite an application
row_id and real fragment coordinates, rather than requiring an Excel row number.
Incomplete coverage of detected candidates fails with extraction_incomplete_candidates;
an omitted conflicting source cannot silently produce a clean result. v2 does not
fall back from a provider failure to offline rules. Stage 7A limits every LLM run
to one automatic attempt. Timeout/disconnect/uncertain server outcomes stop safely.
Direct process_paths callers keep v1 unless contract_version="2.0" is supplied.
DocumentService supplies an attempt journal to both versions; standalone process_paths
and direct adapter calls do not create persistent metering by themselves. The shared
observation boundary persists real usage even on schema failures. Explicit retries
use a new run and key with retry_of, subject to outcome checks. See COST_METRICS.md
and SAFE_RETRIES_AND_IDEMPOTENCY.md.

`process_paths` registers local inputs, parses them, obtains one structured batch
response and passes prepared candidates to deterministic validation. Content, not filenames,
determines types. `DemoExtractor` is offline deterministic extraction for the explicit
layouts in [VARIANT_B.md](VARIANT_B.md); it is not a simulated paid model and does not read expected results.

OpenAI uses the Responses API, strict JSON Schema, no tools and `store=false`.
Reference: https://developers.openai.com/api/docs/guides/structured-outputs
Application settings choose model and explicitly enable paid mode. Transport tests
use httpx.MockTransport. Separate real ProxyAPI experiments completed A successfully
and rejected V at Extraction row_scope validation before provenance/3A. The direct
OpenAI endpoint has not been tested. See [PROXYAPI.md](PROXYAPI.md) for the evidence
and the distinction between offline, mock and paid results.

Default limits: 150 KB input, 200 KB extracted output, 8,000 output tokens,
30 seconds per attempt, 65 seconds total. The configured attempts limit (default two,
maximum three) applies only to explicitly offline extractors. LLM and unknown custom
extractors receive one automatic attempt, including schema failures and HTTP 429/500.
No silent truncation. A known failed response may be retried as an explicit new paid
action; uncertain remote outcomes cannot. Provider exactly-once execution is not promised.
Error messages and audit never contain provider response bodies or API keys.

Document text stays in the untrusted user payload, separate from application
instructions and schema. Returned IDs resolve only against this run's fragments.
Models cannot set verified, final values, policies, permissions or initiate actions.
This limits effects of prompt injection; semantic extraction completeness still needs
human review and cannot be proven solely by structured output or provenance.

Missing candidates become missing fields. Invalid candidate references produce
unverified_source in 3A. Invalid classification references and incomplete batch
classifications cause technical failure. Unknown classifications produce needs_review.

# Deployment configuration

`DAA_EXTRACTION_LIMITS` accepts a JSON object validated as ExtractionLimits.
Example: `{"attempts":1,"timeout_seconds":20.0,"total_seconds":25.0,"max_output_tokens":4000}`.
Invalid or excessive limits fail at startup. No policy parameters come from documents.
# Настраиваемый Responses endpoint

`OPENAI_BASE_URL` допускает только `https://api.openai.com/v1` (по умолчанию)
и `https://api.proxyapi.ru/v1`. Перенаправления и прокси из окружения отключены.
Бизнес-правила не меняются. Отдельный A/V запускатель, локальный бюджет и
безопасная настройка описаны в [PROXYAPI.md](PROXYAPI.md).
Обычный backend сам по себе не обеспечивает общий денежный лимит.
