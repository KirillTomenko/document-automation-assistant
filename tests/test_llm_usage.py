"""Main service metering with mock HTTP, synthetic rates and durable SQLite."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_formats_v2 import batch

from document_automation.adapters.openai_extractor import (
    PROXY_BASE_URL,
    OpenAIExtractor,
)
from document_automation.api import create_app
from document_automation.extraction import ExtractionLimits
from document_automation.llm_usage import (
    AttemptRecord,
    RunMeter,
    TariffCatalog,
    checked_usage,
    estimated_cost,
)
from document_automation.metering import AttemptJournal
from document_automation.schemas import AuditEvent, ProcessingResult, result_fingerprint
from document_automation.service import DocumentService
from document_automation.storage import ResultStore, StorageIntegrityError

FIXTURES = Path(__file__).parent / "fixtures/formats"
USAGE = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "total_tokens": 1500,
    "input_tokens_details": {"cached_tokens": 200},
}


def tariffs(provider="openai", **updates):
    value = dict(
        provider=provider,
        model="test-model",
        version="synthetic-v1",
        currency="RUB",
        source="Synthetic test tariff, not a provider quote",
        as_of="2026-10-20",
        input_per_million="10",
        output_per_million="20",
        cached_input_per_million="2",
    )
    value.update(updates)
    return TariffCatalog.model_validate_json(json.dumps({"tariffs": [value]}))


def adapter_for(cases=("ok",), usages=None, base_url=None):
    calls = []

    def response(request):
        index = len(calls)
        calls.append(request)
        case = cases[min(index, len(cases) - 1)]
        if case == "timeout":
            raise httpx.ReadTimeout("PRIVATE-NETWORK-DETAIL")
        if case in ("429", "500"):
            return httpx.Response(int(case), text="PRIVATE-HTTP-DETAIL")
        body = json.loads(request.content)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        claims = payload["supported_claims"]
        if case == "references":
            claims["candidates"].pop()
        output = "{bad" if case == "schema" else json.dumps(claims)
        usage = usages[min(index, len(usages) - 1)] if usages is not None else USAGE
        envelope = {
            "status": case if case in ("incomplete", "in_progress") else "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": output}],
                }
            ],
        }
        if usage != "ABSENT":
            envelope["usage"] = usage
        return httpx.Response(200, json=envelope)

    kwargs = {"base_url": base_url} if base_url else {}
    adapter = OpenAIExtractor(
        "synthetic-test-key",
        "test-model",
        allow_paid=True,
        transport=httpx.MockTransport(response),
        contract_version="2.0",
        **kwargs,
    )
    return adapter, calls


def run(tmp_path, cases=("ok",), usages=None, catalog=None, **kwargs):
    adapter, calls = adapter_for(cases, usages, **kwargs)
    app = create_app(
        tmp_path, adapter, tariffs=catalog if catalog is not None else tariffs()
    )
    with TestClient(app) as client:
        response = client.post("/runs", json=batch([FIXTURES / "all.docx"]))
        assert response.status_code == 201
        result = response.json()
        summary = client.get(f"/runs/{result['run_id']}/usage")
        assert summary.status_code == 200
    records = app.state.service.store.get_attempts(result["run_id"])
    return result, summary.json(), records, calls


def test_usage_success_cached_price_and_snapshot(tmp_path):
    result, summary, records, calls = run(tmp_path)
    assert result["processing_status"] == "completed"
    assert len(calls) == summary["attempts"] == 1
    assert summary["retry_calls"] == 0
    assert summary["confirmed_input_tokens"] == 1000
    assert summary["confirmed_output_tokens"] == 500
    assert summary["confirmed_cached_tokens"] == 200
    assert summary["known_estimated_costs"] == {"RUB": "0.0184"}
    assert not summary["has_unknown_cost"]
    assert summary["data_origin"] == "simulated"
    assert records[0].status == "succeeded"
    assert records[0].finished_at is not None
    assert records[0].tariff.version == "synthetic-v1"
    assert records[0].tariff.source == "Synthetic test tariff, not a provider quote"


def explicit_pair(tmp_path, cases):
    adapter, calls = adapter_for(cases)
    app = create_app(tmp_path, adapter, tariffs=tariffs())
    payload = batch([FIXTURES / "all.docx"])
    with TestClient(app) as client:
        first = client.post(
            "/runs", json=payload, headers={"Idempotency-Key": "first"}
        ).json()
        second = client.post(
            "/runs",
            json={**payload, "retry_of": first["run_id"]},
            headers={"Idempotency-Key": "explicit-retry"},
        ).json()
        summaries = [
            client.get(f"/runs/{r['run_id']}/usage").json() for r in (first, second)
        ]
    return first, second, summaries, calls, app.state.service.store


def test_explicit_retry_counts_both_paid_runs(tmp_path):
    first, second, summaries, calls, store = explicit_pair(tmp_path, ("schema", "ok"))
    assert (
        first["processing_status"] == "failed"
        and second["processing_status"] == "completed"
    )
    assert first["run_id"] != second["run_id"] and len(calls) == 2
    assert all(s["attempts"] == 1 and s["retry_calls"] == 0 for s in summaries)
    assert sum(Decimal(s["estimated_llm_api_cost"]) for s in summaries) == Decimal(
        "0.0368"
    )
    assert store.get_request(second["run_id"]).retry_of == first["run_id"]


@pytest.mark.parametrize("failure", ("schema", "incomplete", "references"))
def test_failed_after_response_retains_usage_and_cost(tmp_path, failure):
    result, summary, records, calls = run(tmp_path, (failure,))
    assert result["processing_status"] == "failed"
    assert all(r.status == "failed" for r in records)
    assert all(r.usage_status == "valid" for r in records)
    assert summary["attempts"] == len(calls)
    assert Decimal(summary["estimated_llm_api_cost"]) == Decimal("0.0184") * len(calls)
    assert not summary["has_unknown_cost"]


@pytest.mark.parametrize("failure", ("timeout", "429", "500"))
def test_unknown_error_cost_is_not_zero(tmp_path, failure):
    result, summary, records, calls = run(tmp_path, (failure,))
    assert result["processing_status"] == "failed"
    assert len(calls) == 1
    assert summary["attempts"] == 1 and summary["retry_calls"] == 0
    assert summary["unknown_cost_attempts"] == 1
    assert summary["has_unknown_cost"]
    assert summary["estimated_llm_api_cost"] is None
    assert summary["known_estimated_costs"] == {}
    assert all(r.estimated_cost is None for r in records)
    assert "PRIVATE" not in json.dumps(result) + json.dumps(summary)


def test_known_subtotal_is_not_full_total_and_no_stale_usage(tmp_path):
    first, second, summaries, calls, store = explicit_pair(
        tmp_path, ("schema", "timeout")
    )
    assert second["processing_status"] == "failed"
    assert len(calls) == 2
    record = store.get_attempts(second["run_id"])[0]
    assert record.input_tokens is None and record.usage_status == "missing"
    assert summaries[0]["known_estimated_costs"] == {"RUB": "0.0184"}
    assert (
        summaries[1]["has_unknown_cost"]
        and summaries[1]["estimated_llm_api_cost"] is None
    )


@pytest.mark.parametrize(
    "usage,status",
    [
        ("ABSENT", "missing"),
        (None, "missing"),
        ({}, "invalid"),
        ({"input_tokens": True, "output_tokens": 5}, "invalid"),
        ({"input_tokens": "100", "output_tokens": 5}, "invalid"),
        ({"input_tokens": -1, "output_tokens": 5}, "invalid"),
        ({"input_tokens": 100, "output_tokens": 5, "total_tokens": 106}, "invalid"),
        (
            {
                "input_tokens": 100,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 101},
            },
            "invalid",
        ),
    ],
)
def test_absent_and_invalid_usage(tmp_path, usage, status):
    result, summary, records, _ = run(tmp_path, usages=[usage])
    assert result["processing_status"] == "completed"
    assert records[0].usage_status == status
    assert records[0].input_tokens is None
    assert records[0].estimated_cost is None
    assert summary["has_unknown_cost"]


def test_missing_tariff_does_not_guess_a_price(tmp_path):
    _, summary, records, _ = run(tmp_path, catalog=TariffCatalog())
    assert records[0].usage_status == "valid" and records[0].tariff is None
    assert summary["confirmed_input_tokens"] == 1000
    assert summary["estimated_llm_api_cost"] is None and summary["has_unknown_cost"]


def test_cache_breakdown_missing_and_uniform_tariff():
    state, usage = checked_usage({"input_tokens": 1000, "output_tokens": 500})
    assert state == "valid"
    assert estimated_cost(usage, tariffs().tariffs[0]) is None
    assert estimated_cost(
        usage, tariffs(cached_input_per_million=None).tariffs[0]
    ) == Decimal("0.02")
    assert estimated_cost(
        usage, tariffs(cached_input_per_million="10").tariffs[0]
    ) == Decimal("0.02")


def test_proxyapi_identity_and_tariff_matching(tmp_path):
    _, summary, records, _ = run(
        tmp_path, catalog=tariffs("proxyapi"), base_url=PROXY_BASE_URL
    )
    assert summary["provider"] == "proxyapi"
    assert summary["known_estimated_costs"] == {"RUB": "0.0184"}
    assert records[0].tariff.provider == "proxyapi"


def test_offline_demo_zero_api_calls_and_no_attempt_rows(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        result = client.post("/runs", json=batch([FIXTURES / "all.pdf"])).json()
        summary = client.get(f"/runs/{result['run_id']}/usage").json()
    assert summary["mode"] == "offline" and summary["data_origin"] == "offline"
    assert summary["attempts"] == summary["retry_calls"] == 0
    assert summary["estimated_llm_api_cost"] == "0"
    assert not summary["has_unknown_cost"]
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM llm_attempts").fetchone() == (0,)


def test_restart_no_double_count_no_repricing_no_result_changes(tmp_path):
    result, summary, records, calls = run(tmp_path)
    run_id = result["run_id"]
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        before = db.execute(
            "SELECT snapshot,digest FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    store = ResultStore(tmp_path)
    store.finish_attempt(records[0])
    with pytest.raises(StorageIntegrityError):
        store.finish_attempt(
            records[0].model_copy(update={"estimated_cost": Decimal("999")})
        )
    with pytest.raises(StorageIntegrityError):
        store.start_attempt(
            records[0].model_copy(update={"status": "started", "finished_at": None})
        )
    with TestClient(
        create_app(
            tmp_path, tariffs=tariffs(version="synthetic-v2", input_per_million="999")
        )
    ) as client:
        assert client.get(f"/runs/{run_id}/usage").json() == summary
        saved = client.get(f"/runs/{run_id}").json()
        assert result_fingerprint(
            ProcessingResult.model_validate_json(json.dumps(saved))
        ) == result_fingerprint(
            ProcessingResult.model_validate_json(json.dumps(result))
        )
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        assert (
            db.execute(
                "SELECT snapshot,digest FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            == before
        )
        assert db.execute("SELECT count(*) FROM llm_attempts").fetchone() == (1,)
    assert len(calls) == 1


def test_interrupted_attempt_survives_and_is_unknown(tmp_path):
    run_id = "b" * 32
    store = ResultStore(tmp_path)
    adapter, calls = adapter_for()
    meter = RunMeter(
        run_id=run_id,
        mode="llm",
        provider="openai",
        model="test-model",
        data_origin="simulated",
    )
    store.begin(run_id, [], meter)
    journal = AttemptJournal(store, meter, adapter, tariffs())
    journal.begin(1)
    restarted = ResultStore(tmp_path)
    summary = restarted.usage_summary(run_id)
    assert restarted.get(run_id).processing_status == "failed"
    assert restarted.get_attempts(run_id)[0].status == "interrupted"
    assert summary.attempts == 1 and summary.has_unknown_cost
    assert summary.estimated_llm_api_cost is None
    assert not calls


def test_usage_saved_before_business_result(tmp_path, monkeypatch):
    adapter, calls = adapter_for()
    service = DocumentService(tmp_path, adapter, tariffs=tariffs())

    def fail_save(result):
        raise OSError("synthetic interruption before business persistence")

    monkeypatch.setattr(service.store, "save", fail_save)
    with pytest.raises(OSError):
        asyncio.run(service.process(batch([FIXTURES / "all.docx"])["files"]))
    store = ResultStore(tmp_path)
    with store.connection() as db:
        run_id = db.execute("SELECT run_id FROM runs").fetchone()[0]
    assert store.get(run_id).processing_status == "failed"
    assert store.usage_summary(run_id).estimated_llm_api_cost == Decimal("0.0184")
    assert len(calls) == 1


def test_legacy_run_without_ledger_is_not_free(tmp_path):
    run_id = "a" * 32
    store = ResultStore(tmp_path)
    store.begin(run_id, [])
    event = AuditEvent(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc),
        stage="extraction",
        event="failed",
        message="synthetic legacy error",
        error_code="extraction_timeout",
    )
    store.save(
        ProcessingResult(
            run_id=run_id,
            processing_status="failed",
            documents=(),
            classifications=(),
            data=None,
            issues=(),
            needs_review=False,
            audit=(event,),
        )
    )
    with TestClient(create_app(tmp_path)) as client:
        summary = client.get(f"/runs/{run_id}/usage").json()
    assert summary["mode"] == "legacy_untracked"
    assert summary["attempts"] is None
    assert summary["has_unknown_cost"] and summary["estimated_llm_api_cost"] is None


def test_ledger_contains_no_prompts_responses_or_secret(tmp_path):
    usage = {**USAGE, "debug": "PRIVATE-RAW-RESPONSE", "prompt": "PRIVATE-PROMPT"}
    run(tmp_path, usages=[usage])
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        text = " ".join(
            r[0]
            for table in ("llm_runs", "llm_attempts")
            for r in db.execute(f"SELECT record FROM {table}")
        )
    for forbidden in (
        "PRIVATE",
        "synthetic-test-key",
        "Карточка заказчика",
        "Сборка офисного стола",
        "supported_claims",
        "content_base64",
    ):
        assert forbidden not in text


def test_tariffs_fail_closed_on_duplicate_configuration():
    value = json.loads(tariffs().model_dump_json())["tariffs"][0]
    with pytest.raises(ValueError):
        TariffCatalog.model_validate_json(
            json.dumps({"tariffs": [value, {**value, "version": "other"}]})
        )


def test_configured_limit_cannot_enable_automatic_paid_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("DAA_EXTRACTION_LIMITS", '{"attempts":3}')
    result, summary, records, calls = run(tmp_path, ("schema", "schema", "ok"))
    assert result["processing_status"] == "failed"
    assert len(records) == len(calls) == summary["attempts"] == 1
    assert summary["retry_calls"] == 0
    assert Decimal(summary["estimated_llm_api_cost"]) == Decimal("0.0184")


def test_preflight_disabled_does_not_claim_a_paid_call(tmp_path):
    adapter, calls = adapter_for()
    adapter.allow_paid = False
    with TestClient(create_app(tmp_path, adapter)) as client:
        result = client.post("/runs", json=batch([FIXTURES / "all.docx"])).json()
        summary = client.get(f"/runs/{result['run_id']}/usage").json()
    assert result["processing_status"] == "failed" and not calls
    assert summary["attempts"] == 1 and summary["response_received_calls"] == 0
    assert summary["uncertain_calls"] == 0
    assert summary["estimated_llm_api_cost"] == "0"
    assert not summary["has_unknown_cost"]


def test_attempt_committed_before_http_and_no_sensitive_values(tmp_path):
    calls = []

    def response(request):
        with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
            raw = db.execute("SELECT record FROM llm_attempts").fetchone()[0]
        record = AttemptRecord.model_validate_json(raw)
        assert record.status == "started" and record.estimated_cost is None
        assert record.request_state == "unknown"
        calls.append(request)
        return httpx.Response(500)

    adapter = OpenAIExtractor(
        "synthetic-test-key",
        "test-model",
        allow_paid=True,
        transport=httpx.MockTransport(response),
        contract_version="2.0",
    )
    app = create_app(tmp_path, adapter)
    app.state.service.limits = ExtractionLimits(attempts=1)
    with TestClient(app) as client:
        assert (
            client.post("/runs", json=batch([FIXTURES / "all.docx"])).status_code == 201
        )
    assert len(calls) == 1


def test_cost_storage_failure_recovers_unknown(tmp_path, monkeypatch):
    adapter, calls = adapter_for()
    service = DocumentService(tmp_path, adapter, tariffs=tariffs())

    def interrupted(record):
        raise OSError("synthetic interruption while persisting usage")

    monkeypatch.setattr(service.store, "finish_attempt", interrupted)
    with pytest.raises(OSError):
        asyncio.run(service.process(batch([FIXTURES / "all.docx"])["files"]))
    store = ResultStore(tmp_path)
    with store.connection() as db:
        run_id = db.execute("SELECT run_id FROM runs").fetchone()[0]
    assert store.get_attempts(run_id)[0].status == "interrupted"
    assert store.usage_summary(run_id).has_unknown_cost
    assert store.usage_summary(run_id).estimated_llm_api_cost is None
    assert len(calls) == 1


def test_usage_digest_corruption_returns_safe_error(tmp_path):
    result, _, _, _ = run(tmp_path)
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        db.execute("UPDATE llm_attempts SET digest='corrupt'")
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/runs/{result['run_id']}/usage")
        assert response.status_code == 409
        assert response.json()["code"] == "storage_integrity"
        assert client.get(f"/runs/{result['run_id']}").status_code == 200


def test_v1_pipeline_is_metered_without_changing_its_result_schema(tmp_path):
    from document_automation.extraction import DemoExtractor

    async def response(request):
        body = json.loads(request.content)
        payload = body["input"][0]["content"][0]["text"]
        output = await DemoExtractor().extract(payload, ExtractionLimits())
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": USAGE,
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": output}],
                    }
                ],
            },
        )

    adapter = OpenAIExtractor(
        "synthetic-test-key",
        "test-model",
        allow_paid=True,
        transport=httpx.MockTransport(response),
    )
    paths = [
        Path(__file__).parents[1] / "demo/A" / name
        for name in ("customer.pdf", "description.docx", "items.xlsx")
    ]
    with TestClient(
        create_app(tmp_path, adapter, contract_version="1.0", tariffs=tariffs())
    ) as client:
        result = client.post("/runs", json=batch(paths)).json()
        assert (
            result["processing_status"] == "completed"
            and "contract_version" not in result
        )
        summary = client.get(f"/runs/{result['run_id']}/usage").json()
    assert summary["attempts"] == 1 and summary["known_estimated_costs"] == {
        "RUB": "0.0184"
    }


def test_no_call_before_input_limit_and_safe_usage_not_found(tmp_path):
    adapter, calls = adapter_for()
    app = create_app(tmp_path, adapter, tariffs=tariffs())
    app.state.service.limits = ExtractionLimits(max_input_bytes=1)
    with TestClient(app) as client:
        result = client.post("/runs", json=batch([FIXTURES / "all.docx"])).json()
        summary = client.get(f"/runs/{result['run_id']}/usage").json()
        assert client.get("/runs/invalid-id/usage").status_code == 404
    assert result["processing_status"] == "failed" and not calls
    assert summary["attempts"] == 0 and summary["estimated_llm_api_cost"] == "0"


def test_multiple_currencies_not_added_together():
    from document_automation.llm_usage import summarize_usage

    now = datetime.now(timezone.utc)
    meter = RunMeter(
        run_id="a" * 32,
        mode="llm",
        provider="openai",
        model="test-model",
        data_origin="simulated",
    )
    records = tuple(
        AttemptRecord(
            run_id=meter.run_id,
            provider=meter.provider,
            model=meter.model,
            attempt_no=i + 1,
            started_at=now,
            finished_at=now,
            status="succeeded",
            request_state="response_received",
            usage_status="valid",
            input_tokens=1000,
            output_tokens=500,
            cached_tokens=200,
            tariff=tariffs(currency=currency).tariffs[0],
            estimated_cost=Decimal("0.0184"),
        )
        for i, currency in enumerate(("RUB", "USD"))
    )
    summary = summarize_usage(meter.run_id, meter, records)
    assert summary.known_estimated_costs == {
        "RUB": Decimal("0.0184"),
        "USD": Decimal("0.0184"),
    }
    assert summary.currency is None and summary.estimated_llm_api_cost is None
    assert not summary.has_unknown_cost
