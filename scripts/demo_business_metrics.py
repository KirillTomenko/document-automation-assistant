"""Synthetic operator acceptance, mock costs and real local HTTP restore checks."""

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
from demo_backup_restore import ROOT, cli, observe, server, uploads

from document_automation.adapters.openai_extractor import OpenAIExtractor
from document_automation.backup import BackupManifest
from document_automation.backup_paths import checksum
from document_automation.extraction import DemoExtractor
from document_automation.llm_usage import TariffCatalog
from document_automation.service import DocumentService, ServiceError


async def seed(data):
    infos, calls = [], []
    files = uploads(
        sorted(
            p
            for p in (ROOT / "demo/B").iterdir()
            if p.suffix in (".pdf", ".docx", ".xlsx")
        )
    )
    service = DocumentService(data, DemoExtractor(), contract_version="1.0")
    v1 = await service.process(files, "business-v1")
    assert v1.needs_review
    await service.generate(v1.run_id)
    await service.generate_report(v1.run_id)
    try:
        await service.accept(v1.run_id)
    except ServiceError as error:
        assert error.code == "acceptance_review_required"
    else:
        raise AssertionError("Review acknowledgement was not required")
    await service.accept(v1.run_id, True)
    infos.append(dict(run_id=v1.run_id, key="business-v1", files=files, retry_of=None))

    def response(request):
        calls.append(request)
        if len(calls) == 3:
            raise httpx.ReadTimeout("Synthetic timeout without usage")
        claims = json.loads(
            json.loads(request.content)["input"][0]["content"][0]["text"]
        )["supported_claims"]
        text = "{bad" if len(calls) == 1 else json.dumps(claims)
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

    adapter = OpenAIExtractor(
        "synthetic-test-key",
        "test-model",
        allow_paid=True,
        transport=httpx.MockTransport(response),
        contract_version="2.0",
    )
    catalog = TariffCatalog.model_validate_json(
        (ROOT / "config/llm_tariffs.example.json").read_bytes()
    )
    service = DocumentService(data, adapter, tariffs=catalog)
    files = uploads([ROOT / "tests/fixtures/formats/all.docx"])
    parent = await service.process(files, "business-failed")
    child = await service.process(files, "business-child", retry_of=parent.run_id)
    await service.generate(child.run_id)
    await service.generate_report(child.run_id)
    await service.accept(child.run_id)
    known = service.business_metrics().model_dump(mode="json")
    assert known["known_estimated_costs"] == {"RUB": "0.0368"}
    assert known["average_cost_per_accepted"] == "0.0184"
    assert known["accepted_results"] == 2 and known["retry_calls"] == 1
    await service.revoke_acceptance(child.run_id)
    assert service.business_metrics().average_cost_per_accepted == Decimal("0.0368")
    await service.accept(child.run_id)
    unknown = await service.process(files, "business-timeout")
    assert (
        unknown.processing_status == parent.processing_status == "failed"
        and len(calls) == 3
    )
    partial = service.business_metrics().model_dump(mode="json")
    assert (
        partial["average_cost_per_accepted"] is None
        and partial["unknown_cost_attempts"] == 1
    )
    for result, key, retry in (
        (parent, "business-failed", None),
        (child, "business-child", parent.run_id),
        (unknown, "business-timeout", None),
    ):
        infos.append(dict(run_id=result.run_id, key=key, files=files, retry_of=retry))
    return infos, known, partial, len(calls)


def demonstrate(output):
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    data, copy, restored = (output / n for n in ("data", "backup", "restored"))
    infos, known, partial, calls = asyncio.run(seed(data))
    with server(data) as client:
        before_results, before_usage, before_operations, before_artifacts = observe(
            client, infos
        )
        histories = {}
        for info in infos:
            run_id = info["run_id"]
            status = client.get(f"/runs/{run_id}/acceptance").json()
            if status["accepted"]:
                response = client.post(
                    f"/runs/{run_id}/acceptance", json={"remarks_reviewed": True}
                )
                assert response.status_code == 200 and response.json() == status
            histories[run_id] = status
        assert client.get("/business-metrics").json() == partial
    created = cli("backup", "--data", data, "--output", copy)
    verified = cli("verify-backup", "--backup", copy)
    recovered = cli("restore", "--backup", copy, "--target", restored)
    assert {**verified, "command": "backup"} == created
    assert {**recovered, "command": "backup"} == created
    manifest = BackupManifest.model_validate_json((copy / "manifest.json").read_bytes())
    file_count = 0
    for entry in manifest.files:
        if entry.role != "database":
            assert (
                checksum(data / entry.path)
                == checksum(restored / entry.path)
                == (entry.size, entry.sha256)
            )
            file_count += 1
    with server(restored) as client:
        assert client.get("/business-metrics").json() == partial
        for run_id, result in before_results.items():
            assert client.get(f"/runs/{run_id}").json() == result
            assert client.get(f"/runs/{run_id}/acceptance").json() == histories[run_id]
        _, usage, operations, artifacts = observe(client, infos)
        assert (
            usage == before_usage
            and operations == before_operations
            and artifacts == before_artifacts
        )
        assert client.get("/business-metrics").json() == partial
    report = dict(
        data_origin="synthetic_documents_and_mock_http",
        tariff="conditional demonstration prices, not a provider quote; exact version is in each metrics.tariffs entry",
        real_paid_llm_calls=0,
        mock_http_calls=calls,
        known_cost_example=known,
        unknown_cost_example=partial,
        acceptance_events_preserved=sum(len(s["history"]) for s in histories.values()),
        backup=created,
        verify=verified,
        restore=recovered,
        restored_http_checks_passed=True,
        unchanged_source_and_docx_sha256=file_count,
        contracts=["1.0", "2.0"],
        idempotency_and_retry_of_preserved=True,
    )
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    demonstrate(parser.parse_args().output)
