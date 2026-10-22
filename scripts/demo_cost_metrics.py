"""Synthetic main-service cost demo; every HTTP request is handled in memory."""

import argparse
import asyncio
import base64
import json
from pathlib import Path
from uuid import uuid4

import httpx

from document_automation.adapters.openai_extractor import OpenAIExtractor
from document_automation.extraction_v2 import DemoExtractorV2
from document_automation.llm_usage import TariffCatalog
from document_automation.service import DocumentService

ROOT = Path(__file__).resolve().parents[1]


async def demonstrate(root):
    catalog = TariffCatalog.model_validate_json(
        (ROOT / "config/llm_tariffs.example.json").read_text(encoding="utf-8")
    )
    document = ROOT / "tests/fixtures/formats/all.docx"
    uploads = [
        dict(
            filename=document.name,
            content_base64=base64.b64encode(document.read_bytes()).decode(),
        )
    ]
    summaries = []
    failed_parent = None
    for case in (
        "offline",
        "mock_success",
        "mock_failure",
        "mock_explicit_retry",
        "mock_timeout",
    ):
        calls = []

        def response(request):
            calls.append(request)
            if case == "mock_timeout":
                raise httpx.ReadTimeout("Synthetic timeout")
            body = json.loads(request.content)
            payload = json.loads(body["input"][0]["content"][0]["text"])
            text = (
                "{bad"
                if case == "mock_failure"
                else json.dumps(payload["supported_claims"])
            )
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "total_tokens": 1500,
                        "input_tokens_details": {"cached_tokens": 200},
                    },
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                },
            )

        extractor = (
            DemoExtractorV2()
            if case == "offline"
            else OpenAIExtractor(
                "synthetic-test-key",
                "test-model",
                allow_paid=True,
                transport=httpx.MockTransport(response),
                contract_version="2.0",
            )
        )
        service = DocumentService(root, extractor, tariffs=catalog)
        retry_of = failed_parent if case == "mock_explicit_retry" else None
        action_key = "cost-demo:" + uuid4().hex
        result = await service.process(
            uploads, idempotency_key=action_key, retry_of=retry_of
        )
        if case == "mock_failure":
            failed_parent = result.run_id
        # Replaying the same action must neither call the mock nor alter usage.
        replay = await service.submit(uploads, action_key, retry_of)
        if replay.status_code != 200 or replay.body.run_id != result.run_id:
            raise RuntimeError("Synthetic idempotent replay mismatch")
        summary = service.usage(result.run_id).model_dump(mode="json")
        if summary["attempts"] != len(calls):
            raise RuntimeError("Synthetic request count mismatch")
        summaries.append(
            dict(
                case=case,
                processing_status=result.processing_status,
                retry_of=retry_of,
                replay_without_new_call=True,
                **summary,
            )
        )
    report = dict(
        data_origin="synthetic fixtures, mock usage and illustrative tariffs",
        actual_external_api_calls=0,
        runs=summaries,
    )
    text = json.dumps(report, indent=2, ensure_ascii=False)
    (root / "summary.json").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/cost-metrics-demo")
    args = parser.parse_args()
    asyncio.run(demonstrate(args.output))
