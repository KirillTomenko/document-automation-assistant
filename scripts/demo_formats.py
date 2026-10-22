"""Persist synthetic v2 runs and verify both downloads, with no network/provider."""

import argparse
import asyncio
import base64
import json
from pathlib import Path

from document_automation.extraction_v2 import DemoExtractorV2
from document_automation.generation import verify_document, verify_verification_report
from document_automation.service import DocumentService

ROOT = Path(__file__).resolve().parents[1]


async def demonstrate(output, cases):
    service = DocumentService(output, DemoExtractorV2())
    formats = {"P": "pdf", "D": "docx", "X": "xlsx"}
    results = []
    for case in cases:
        paths = [
            ROOT / "tests/fixtures/formats" / f"{role}.{formats[letter]}"
            for role, letter in zip(("customer", "description", "items"), case)
        ]
        result = await service.process(
            [
                dict(
                    filename=p.name,
                    content_base64=base64.b64encode(p.read_bytes()).decode(),
                )
                for p in paths
            ]
        )
        if result.processing_status != "completed" or result.needs_review:
            raise RuntimeError(f"Synthetic case {case} failed validation")
        result = await service.generate(result.run_id)
        _, work = await service.export(result.run_id)
        result = await service.generate_report(result.run_id)
        _, report = await service.export_report(result.run_id)
        if not verify_document(result, work) or not verify_verification_report(
            result, report
        ):
            raise RuntimeError("Artifact verification failed")
        results.append(
            dict(
                case=case,
                run_id=result.run_id,
                contract_version=result.contract_version,
                completed=True,
                needs_review=result.needs_review,
                items=len(result.data.items),
                artifacts=2,
            )
        )
    print(
        json.dumps(
            dict(
                data_origin="synthetic",
                extraction="offline deterministic rules; no paid calls",
                external_llm_calls=0,
                cases=results,
                completed=sum(r["completed"] for r in results),
                needs_review=sum(r["needs_review"] for r in results),
                artifacts=sum(r["artifacts"] for r in results),
                output=str(output.resolve()),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/v2-format-demo")
    parser.add_argument(
        "--cases", nargs="+", default=["PPP", "DDD", "XXX", "DXD", "XPX"]
    )
    args = parser.parse_args()
    if any(len(case) != 3 or set(case) - set("PDX") for case in args.cases):
        parser.error("Each case must contain three letters P/D/X")
    asyncio.run(demonstrate(args.output, args.cases))
