"""Synthetic SQL failure after DOCX publication, restart and explicit recovery."""

import argparse
import asyncio
import base64
import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from document_automation.extraction_v2 import DemoExtractorV2
from document_automation.schemas import result_fingerprint
from document_automation.service import DocumentService, ServiceError

ROOT = Path(__file__).resolve().parents[1]


async def demonstrate(root):
    source = ROOT / "tests/fixtures/formats/all.docx"
    service = DocumentService(root, DemoExtractorV2())
    result = await service.process(
        [
            dict(
                filename=source.name,
                content_base64=base64.b64encode(source.read_bytes()).decode(),
            )
        ]
    )
    run_id = result.run_id
    await service.generate(run_id)
    _, request_bytes = await service.export(run_id)
    usage = service.usage(run_id)
    finish = service.store._finish_artifact

    def fail_sql(db, write, state):
        finish(db, write, state)
        if write.kind == "report" and state == "committed":
            raise sqlite3.OperationalError(
                "Synthetic rollback after publication and SQL updates"
            )

    with patch.object(service.store, "_finish_artifact", fail_sql):
        try:
            await service.generate_report(run_id)
        except sqlite3.OperationalError:
            pass
        else:
            raise RuntimeError("Expected injected SQL failure")
    assert service.get(run_id).generated_report is None
    restarted = DocumentService(root, DemoExtractorV2())
    before = restarted.artifact_status(run_id)["report"]
    assert before.state == "recovery_required" and not before.verified
    try:
        await restarted.export_report(run_id)
    except ServiceError as exc:
        assert exc.status == 409
    else:
        raise RuntimeError("Unconfirmed report was exported")
    recovered = await restarted.generate_report(run_id, recover=True)
    _, report_bytes = await restarted.export_report(run_id)
    _, retained_bytes = await restarted.export(run_id)
    assert retained_bytes == request_bytes
    assert result_fingerprint(result) == result_fingerprint(recovered)
    assert restarted.usage(run_id) == usage
    assert (
        await restarted.generate_report(run_id)
    ).generated_report == recovered.generated_report
    summary = dict(
        data_origin="synthetic documents, actual local fault injection",
        run_id=run_id,
        injected_failure="SQL rollback after final file publication",
        after_restart=before.model_dump(mode="json"),
        unconfirmed_export_blocked=True,
        after_explicit_recovery=restarted.artifact_status(run_id)["report"].model_dump(
            mode="json"
        ),
        report_write_states=[
            w.state for w in restarted.store.artifact_writes(run_id, "report")
        ],
        original_request_sha256=sha256(request_bytes).hexdigest(),
        retained_request_sha256=sha256(retained_bytes).hexdigest(),
        report_sha256=sha256(report_bytes).hexdigest(),
        prior_confirmed_request_unchanged=True,
        business_fingerprint_unchanged=True,
        usage_unchanged=True,
        external_llm_calls=0,
        llm_api_cost="0 (offline; total processing cost not measured)",
    )
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    (root / "summary.json").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "tmp/artifact-recovery-demo"
    )
    args = parser.parse_args()
    asyncio.run(demonstrate(args.output))
