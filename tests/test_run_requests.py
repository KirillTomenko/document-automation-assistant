"""Idempotent request reservation, cancellation, restart and accounting boundaries."""

import asyncio
import base64
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_formats_v2 import FIXTURES, batch
from test_llm_usage import USAGE, adapter_for, tariffs

from document_automation.adapters.openai_extractor import OpenAIExtractor
from document_automation.api import create_app
from document_automation.extraction import ExtractionLimits
from document_automation.llm_usage import RunMeter
from document_automation.metering import AttemptJournal
from document_automation.run_requests import (
    RunRequest,
    content_fingerprint,
    key_hash,
    request_fingerprint,
    streamlit_action,
)
from document_automation.schemas import ProcessingResult, result_fingerprint
from document_automation.service import DocumentService

PAYLOAD = batch([FIXTURES / "all.docx"])
HEADERS = {"Idempotency-Key": "one-action"}


def test_replay_completed_preserves_result_usage_and_artifacts(tmp_path):
    adapter, calls = adapter_for()
    app = create_app(tmp_path, adapter, tariffs=tariffs())
    with TestClient(app) as client:
        first = client.post("/runs", json=PAYLOAD, headers=HEADERS)
        assert first.status_code == 201
        result = first.json()
        run_id = result["run_id"]
        summary = client.get(f"/runs/{run_id}/usage").json()
        for _ in range(3):
            replay = client.post("/runs", json=PAYLOAD, headers=HEADERS)
            assert replay.status_code == 200
            assert replay.headers["Idempotency-Replayed"] == "true"
            assert replay.json() == result
        assert client.post(f"/runs/{run_id}/document").status_code == 200
        assert client.post(f"/runs/{run_id}/verification-report").status_code == 200
        work = client.get(f"/runs/{run_id}/document").content
        report = client.get(f"/runs/{run_id}/verification-report").content
        updated = client.post("/runs", json=PAYLOAD, headers=HEADERS).json()
        assert result_fingerprint(
            ProcessingResult.model_validate_json(json.dumps(updated))
        ) == result_fingerprint(
            ProcessingResult.model_validate_json(json.dumps(result))
        )
        assert client.get(f"/runs/{run_id}/usage").json() == summary
    new_adapter, new_calls = adapter_for()
    with TestClient(create_app(tmp_path, new_adapter)) as client:
        assert client.post("/runs", json=PAYLOAD, headers=HEADERS).json() == updated
        assert client.get(f"/runs/{run_id}/document").content == work
        assert client.get(f"/runs/{run_id}/verification-report").content == report
        assert client.get(f"/runs/{run_id}/usage").json() == summary
    assert len(calls) == 1 and not new_calls
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM llm_attempts").fetchone() == (1,)


@pytest.mark.parametrize("change", ("name", "content", "multiplicity", "retry_of"))
def test_same_key_changed_request_conflicts_without_call(tmp_path, change):
    adapter, calls = adapter_for()
    with TestClient(create_app(tmp_path, adapter)) as client:
        client.post("/runs", json=PAYLOAD, headers=HEADERS)
        altered = json.loads(json.dumps(PAYLOAD))
        if change == "name":
            altered["files"][0]["filename"] = "renamed.docx"
        elif change == "content":
            altered["files"][0]["content_base64"] = base64.b64encode(
                b"different content"
            ).decode()
        elif change == "multiplicity":
            altered["files"].append(altered["files"][0])
        else:
            altered["retry_of"] = "a" * 32
        response = client.post("/runs", json=altered, headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_conflict"
    assert len(calls) == 1


def test_file_order_json_order_does_not_change_identity(tmp_path):
    payload = batch(
        [
            FIXTURES / "customer.pdf",
            FIXTURES / "description.docx",
            FIXTURES / "items.xlsx",
        ]
    )
    adapter, calls = adapter_for()
    with TestClient(create_app(tmp_path, adapter)) as client:
        first = client.post("/runs", json=payload, headers=HEADERS).json()
        reversed_payload = {"files": payload["files"][::-1], "retry_of": None}
        response = client.post(
            "/runs",
            content=json.dumps(reversed_payload, indent=3),
            headers={**HEADERS, "Content-Type": "application/json"},
        )
    assert response.status_code == 200 and response.json() == first
    assert len(calls) == 1


def test_keyless_clients_still_create_new_runs(tmp_path):
    adapter, calls = adapter_for()
    with TestClient(create_app(tmp_path, adapter)) as client:
        first = client.post("/runs", json=PAYLOAD)
        second = client.post("/runs", json=PAYLOAD)
        assert first.status_code == second.status_code == 201
        assert first.json()["run_id"] != second.json()["run_id"]
        invalid_retry = client.post(
            "/runs", json={**PAYLOAD, "retry_of": first.json()["run_id"]}
        )
        assert invalid_retry.status_code == 422
    assert len(calls) == 2


@pytest.mark.parametrize(
    "key", ("", "spaces forbidden", "x" * 129, "slash/not/allowed")
)
def test_invalid_key_has_no_side_effects(tmp_path, key):
    adapter, calls = adapter_for()
    with TestClient(create_app(tmp_path, adapter)) as client:
        response = client.post("/runs", json=PAYLOAD, headers={"Idempotency-Key": key})
        assert response.status_code == 422
        assert response.json()["code"] == "idempotency_key_invalid"
    assert not calls
    assert not list(tmp_path.glob("*/inputs"))


def test_duplicate_key_headers_rejected(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post(
            "/runs",
            json=PAYLOAD,
            headers=[("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
        )
    assert response.status_code == 422


def blocking_adapter(started, release, calls):
    async def response(request):
        calls.append(request)
        started.set()
        await release.wait()
        payload = json.loads(
            json.loads(request.content)["input"][0]["content"][0]["text"]
        )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": USAGE,
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(payload["supported_claims"]),
                            }
                        ],
                    }
                ],
            },
        )

    return OpenAIExtractor(
        "synthetic-test-key",
        "test-model",
        allow_paid=True,
        transport=httpx.MockTransport(response),
        contract_version="2.0",
    )


def test_duplicate_during_inflight_processing_returns_202(tmp_path):
    async def scenario():
        started, release, calls = asyncio.Event(), asyncio.Event(), []
        app = create_app(
            tmp_path, blocking_adapter(started, release, calls), tariffs=tariffs()
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            task = asyncio.create_task(
                client.post("/runs", json=PAYLOAD, headers=HEADERS)
            )
            await asyncio.wait_for(started.wait(), 3)
            try:
                replay = await asyncio.wait_for(
                    client.post("/runs", json=PAYLOAD, headers=HEADERS), 2
                )
                assert replay.status_code == 202
                assert "processing_status" not in replay.json()
                run_id = replay.json()["run_id"]
                assert (await client.get(f"/runs/{run_id}")).status_code == 202
                assert (await client.get(f"/runs/{run_id}/operation")).json()[
                    "state"
                ] == "processing"
                assert len(calls) == 1
            finally:
                release.set()
                first = await task
            assert first.status_code == 201 and first.json()["run_id"] == run_id
            assert (
                await client.post("/runs", json=PAYLOAD, headers=HEADERS)
            ).status_code == 200
            summary = (await client.get(f"/runs/{run_id}/usage")).json()
            assert (
                summary["attempts"] == 1 and summary["confirmed_input_tokens"] == 1000
            )
            assert len(calls) == 1

    asyncio.run(scenario())


def test_cancelled_inflight_and_restart_never_resends(tmp_path):
    async def scenario():
        started, release, calls = asyncio.Event(), asyncio.Event(), []
        app = create_app(
            tmp_path, blocking_adapter(started, release, calls), tariffs=tariffs()
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            task = asyncio.create_task(
                client.post("/runs", json=PAYLOAD, headers=HEADERS)
            )
            await asyncio.wait_for(started.wait(), 3)
            pending = (await client.post("/runs", json=PAYLOAD, headers=HEADERS)).json()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return pending["run_id"], calls

    run_id, calls = asyncio.run(scenario())
    adapter, restarted_calls = adapter_for()
    with TestClient(create_app(tmp_path, adapter)) as client:
        replay = client.post("/runs", json=PAYLOAD, headers=HEADERS)
        assert replay.status_code == 200 and replay.json()["run_id"] == run_id
        assert replay.json()["processing_status"] == "failed"
        usage = client.get(f"/runs/{run_id}/usage").json()
        assert usage["attempts"] == usage["unknown_outcome_attempts"] == 1
        assert usage["estimated_llm_api_cost"] is None
        blocked = client.post(
            "/runs",
            json={**PAYLOAD, "retry_of": run_id},
            headers={"Idempotency-Key": "new-retry"},
        )
        assert (
            blocked.status_code == 409
            and blocked.json()["code"] == "retry_outcome_unknown"
        )
    assert len(calls) == 1 and not restarted_calls


@pytest.mark.parametrize("failure", ("timeout", "500"))
def test_uncertain_provider_outcome_one_call_replay_and_new_retry_blocked(
    tmp_path, failure
):
    adapter, calls = adapter_for((failure,))
    app = create_app(tmp_path, adapter, tariffs=tariffs())
    app.state.service.limits = ExtractionLimits(attempts=3)
    with TestClient(app) as client:
        first = client.post("/runs", json=PAYLOAD, headers=HEADERS).json()
        run_id = first["run_id"]
        assert first["processing_status"] == "failed"
        assert client.post("/runs", json=PAYLOAD, headers=HEADERS).json() == first
        summary = client.get(f"/runs/{run_id}/usage").json()
        assert (
            summary["unknown_outcome_attempts"] == summary["unknown_cost_attempts"] == 1
        )
        assert summary["attempts"] == 1 and summary["retry_calls"] == 0
        assert not client.get(f"/runs/{run_id}/operation").json()["can_retry"]
        blocked = client.post(
            "/runs",
            json={**PAYLOAD, "retry_of": run_id},
            headers={"Idempotency-Key": "explicit-unknown"},
        )
        assert blocked.status_code == 409
        assert client.post(f"/runs/{run_id}/document").status_code == 409
    assert len(calls) == 1


def test_explicit_new_retry_has_link_and_separate_charge(tmp_path):
    adapter, calls = adapter_for(("schema", "ok"))
    with TestClient(create_app(tmp_path, adapter, tariffs=tariffs())) as client:
        first = client.post("/runs", json=PAYLOAD, headers=HEADERS).json()
        assert client.get(f"/runs/{first['run_id']}/operation").json()["can_retry"]
        retry_payload = {**PAYLOAD, "retry_of": first["run_id"]}
        second_response = client.post(
            "/runs", json=retry_payload, headers={"Idempotency-Key": "new-action"}
        )
        second = second_response.json()
        assert second_response.status_code == 201
        assert (
            second["processing_status"] == "completed"
            and second["run_id"] != first["run_id"]
        )
        assert (
            client.get(f"/runs/{second['run_id']}/operation").json()["retry_of"]
            == first["run_id"]
        )
        assert (
            client.post(
                "/runs", json=retry_payload, headers={"Idempotency-Key": "new-action"}
            ).json()
            == second
        )
        total = sum(
            Decimal(
                client.get(f"/runs/{r['run_id']}/usage").json()[
                    "estimated_llm_api_cost"
                ]
            )
            for r in (first, second)
        )
        assert total == Decimal("0.0368")
        assert (
            client.post(
                "/runs",
                json={**PAYLOAD, "retry_of": second["run_id"]},
                headers={"Idempotency-Key": "not-a-failure"},
            ).status_code
            == 409
        )
    assert len(calls) == 2


def test_abrupt_exit_with_started_ledger_recovers_same_request(tmp_path):
    service = DocumentService(tmp_path)
    adapter, calls = adapter_for()
    run_id = "c" * 32
    content = content_fingerprint([("all.docx", (FIXTURES / "all.docx").read_bytes())])
    operation = RunRequest(
        run_id=run_id,
        key_sha256=key_hash("one-action"),
        content_sha256=content,
        request_sha256=request_fingerprint(content),
        contract_version="2.0",
        created_at=datetime.now(timezone.utc),
    )
    meter = RunMeter(
        run_id=run_id,
        mode="llm",
        provider="openai",
        model="test-model",
        data_origin="simulated",
    )
    service.store.reserve_request(operation, meter)
    AttemptJournal(service.store, meter, adapter, tariffs()).begin(1)
    with TestClient(create_app(tmp_path, adapter)) as client:
        replay = client.post("/runs", json=PAYLOAD, headers=HEADERS).json()
        assert replay["run_id"] == run_id and replay["processing_status"] == "failed"
        assert replay["contract_version"] == "2.0"
        status = client.get(f"/runs/{run_id}/operation").json()
        assert status["state"] == "interrupted" and not status["can_retry"]
        assert (
            client.get(f"/runs/{run_id}/usage").json()["estimated_llm_api_cost"] is None
        )
    assert not calls


def test_client_key_survives_reordering_and_fresh_streamlit_session():
    files = [("a.pdf", b"PDF"), ("b.docx", b"DOCX")]
    content = content_fingerprint(files)
    first = streamlit_action(content)
    assert streamlit_action(content_fingerprint(files[::-1])) == first
    assert streamlit_action(content, first) == first
    retry = streamlit_action(content, retry_of="a" * 32)
    assert retry["key"] != first["key"]
    assert streamlit_action(content, retry) == retry
    assert streamlit_action(content, retry_of="a" * 32) == retry


@pytest.mark.parametrize("provider_status", ("incomplete", "in_progress"))
def test_unconfirmed_provider_completion_blocks_explicit_retry(
    tmp_path, provider_status
):
    adapter, calls = adapter_for((provider_status,))
    with TestClient(create_app(tmp_path, adapter, tariffs=tariffs())) as client:
        result = client.post("/runs", json=PAYLOAD, headers=HEADERS).json()
        assert result["processing_status"] == "failed"
        run_id = result["run_id"]
        summary = client.get(f"/runs/{run_id}/usage").json()
        assert summary["unknown_outcome_attempts"] == 1
        # Received usage remains usable even when completion is unconfirmed.
        assert summary["estimated_llm_api_cost"] == "0.0184"
        assert not client.get(f"/runs/{run_id}/operation").json()["can_retry"]
        response = client.post(
            "/runs",
            json={**PAYLOAD, "retry_of": run_id},
            headers={"Idempotency-Key": "must-not-send"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "retry_outcome_unknown"
    assert len(calls) == 1


def test_keyed_v1_result_and_fingerprint_survive_v2_service_restart(tmp_path):
    from test_extraction import paths

    payload = batch(paths(tmp_path))
    root = tmp_path / "store"
    with TestClient(create_app(root, contract_version="1.0")) as client:
        response = client.post("/runs", json=payload, headers=HEADERS)
        assert response.status_code == 201
        result = response.json()
        assert result["processing_status"] == "completed" and not result["needs_review"]
        assert "contract_version" not in result
        run_id = result["run_id"]
        fingerprint = result_fingerprint(
            ProcessingResult.model_validate_json(response.content)
        )
        for artifact in ("document", "verification-report"):
            assert client.post(f"/runs/{run_id}/{artifact}").status_code == 200
        artifacts = {
            name: client.get(f"/runs/{run_id}/{name}").content
            for name in ("document", "verification-report")
        }
        # Downloads append audit, so capture the current snapshot afterwards.
        saved = client.get(f"/runs/{run_id}").content
    adapter, calls = adapter_for()
    with TestClient(create_app(root, adapter, contract_version="2.0")) as client:
        replay = client.post("/runs", json=payload, headers=HEADERS)
        assert replay.status_code == 200 and replay.content == saved
        assert (
            result_fingerprint(ProcessingResult.model_validate_json(replay.content))
            == fingerprint
        )
        for name, data in artifacts.items():
            assert client.get(f"/runs/{run_id}/{name}").content == data
        assert client.get(f"/runs/{run_id}/usage").json()["attempts"] == 0
    assert not calls


def test_connection_break_after_headers_before_usage_is_unknown(tmp_path):
    calls = []

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"usage":'
            raise httpx.ReadError("PRIVATE-STREAM-ERROR")

    def response(request):
        calls.append(request)
        return httpx.Response(200, stream=BrokenBody())

    adapter = OpenAIExtractor(
        "synthetic-test-key",
        "test-model",
        allow_paid=True,
        transport=httpx.MockTransport(response),
        contract_version="2.0",
    )
    with TestClient(create_app(tmp_path, adapter, tariffs=tariffs())) as client:
        first = client.post("/runs", json=PAYLOAD, headers=HEADERS).json()
        run_id = first["run_id"]
        assert first["processing_status"] == "failed"
        assert client.post("/runs", json=PAYLOAD, headers=HEADERS).json() == first
        summary = client.get(f"/runs/{run_id}/usage").json()
        assert (
            summary["attempts"]
            == summary["unknown_cost_attempts"]
            == summary["unknown_outcome_attempts"]
            == 1
        )
        assert summary["estimated_llm_api_cost"] is None
        blocked = client.post(
            "/runs",
            json={**PAYLOAD, "retry_of": run_id},
            headers={"Idempotency-Key": "after-broken-stream"},
        )
        assert blocked.status_code == 409
    assert len(calls) == 1
    assert "PRIVATE" not in json.dumps(first) + json.dumps(summary)


def test_request_state_and_snapshot_commit_atomically(tmp_path, monkeypatch):
    adapter, calls = adapter_for()
    app = create_app(tmp_path, adapter, tariffs=tariffs())
    store = app.state.service.store
    original = store._update_request

    def fail_final_state(db, old, new):
        if new.state == "finished":
            raise OSError("synthetic boundary interruption")
        return original(db, old, new)

    monkeypatch.setattr(store, "_update_request", fail_final_state)
    with TestClient(app) as client:
        first = client.post("/runs", json=PAYLOAD, headers=HEADERS)
        assert first.status_code == 503
        replay = client.post("/runs", json=PAYLOAD, headers=HEADERS)
        assert (
            replay.status_code == 200 and replay.json()["processing_status"] == "failed"
        )
        run_id = replay.json()["run_id"]
        summary = client.get(f"/runs/{run_id}/usage").json()
        assert summary["attempts"] == 1 and summary["known_estimated_costs"] == {
            "RUB": "0.0184"
        }
        assert store.get_request(run_id).state == "interrupted"
    assert len(calls) == 1


def test_corrupt_request_or_ledger_blocks_new_call(tmp_path):
    adapter, calls = adapter_for(("schema", "ok"))
    with TestClient(create_app(tmp_path, adapter)) as client:
        first = client.post("/runs", json=PAYLOAD, headers=HEADERS).json()
        with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
            db.execute("UPDATE llm_attempts SET digest='corrupt'")
        retry = client.post(
            "/runs",
            json={**PAYLOAD, "retry_of": first["run_id"]},
            headers={"Idempotency-Key": "new-action"},
        )
        assert retry.status_code == 409 and retry.json()["code"] == "storage_integrity"
        with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
            db.execute("UPDATE run_requests SET digest='corrupt'")
        replay = client.post("/runs", json=PAYLOAD, headers=HEADERS)
        assert (
            replay.status_code == 409 and replay.json()["code"] == "storage_integrity"
        )
        assert client.get(f"/runs/{first['run_id']}/usage").status_code == 409
    assert len(calls) == 1


def test_confirmed_429_can_only_retry_as_linked_new_action(tmp_path):
    adapter, calls = adapter_for(("429", "ok"))
    with TestClient(create_app(tmp_path, adapter, tariffs=tariffs())) as client:
        failed = client.post("/runs", json=PAYLOAD, headers=HEADERS).json()
        run_id = failed["run_id"]
        assert client.get(f"/runs/{run_id}/operation").json()["can_retry"]
        summary = client.get(f"/runs/{run_id}/usage").json()
        assert summary["has_unknown_cost"] and summary["unknown_outcome_attempts"] == 0
        assert client.post("/runs", json=PAYLOAD, headers=HEADERS).json() == failed
        second = client.post(
            "/runs",
            json={**PAYLOAD, "retry_of": run_id},
            headers={"Idempotency-Key": "operator-retry"},
        ).json()
        assert second["processing_status"] == "completed" and second["run_id"] != run_id
    assert len(calls) == 2


def test_streamlit_reuses_key_when_response_is_lost_and_after_restart(
    tmp_path, monkeypatch
):
    from streamlit.testing.v1 import AppTest

    adapter, calls = adapter_for()
    current = {}
    seen_keys = []
    drop = [True]

    def request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        response = current["client"].request(
            method, url.replace("http://127.0.0.1:8000", ""), **kwargs
        )
        if method == "POST" and url.endswith("/runs"):
            seen_keys.append(kwargs["headers"]["Idempotency-Key"])
            if drop[0]:
                drop[0] = False
                raise httpx.ReadTimeout("Synthetic lost client response")
        return response

    monkeypatch.setattr(httpx, "request", request)
    ui = Path(__file__).parents[1] / "src/document_automation/ui.py"
    with TestClient(create_app(tmp_path, adapter, tariffs=tariffs())) as client:
        current["client"] = client
        app = AppTest.from_file(str(ui), default_timeout=30).run()
        app.selectbox[0].select("A").run()
        app.button[0].click().run()
        assert not app.exception and app.error
        key = app.session_state["submission"]["key"]
        app.button[0].click().run()
        assert (
            not app.exception
            and app.session_state["result"]["processing_status"] == "completed"
        )
        assert app.session_state["submission"]["key"] == key
    fresh_adapter, fresh_calls = adapter_for()
    with TestClient(create_app(tmp_path, fresh_adapter)) as client:
        current["client"] = client
        fresh = AppTest.from_file(str(ui), default_timeout=30).run()
        fresh.selectbox[0].select("A").run()
        fresh.button[0].click().run()
        assert not fresh.exception
        assert fresh.session_state["submission"]["key"] == key
        assert (
            fresh.session_state["result"]["run_id"]
            == app.session_state["result"]["run_id"]
        )
    assert len(calls) == 1 and not fresh_calls
    assert seen_keys == [key, key, key]
