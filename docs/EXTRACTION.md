# Extraction boundary

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
30 seconds per attempt, 65 seconds total, maximum two attempts. No silent truncation.
Only schema/transport/timeout failures retry. Authentication and refusal errors do not.
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
