"""Operator acceptance and all-time economics: synthetic files and mock HTTP only."""

import asyncio
import base64
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_api import payload
from test_backup import reseal_database
from test_formats_v2 import FIXTURES, batch
from test_llm_usage import adapter_for, tariffs

from document_automation import acceptance
from document_automation.api import create_app
from document_automation.backup import backup, restore, verify_backup
from document_automation.llm_usage import AttemptRecord
from document_automation.schemas import ProcessingResult, result_fingerprint


def process(client, scenario="A", key="one"):
    files = payload("A" if scenario == "T" else scenario)
    if scenario == "T":
        files["files"][0]["content_base64"] = base64.b64encode(
            b"synthetic damaged input"
        ).decode()
    response = client.post("/runs", json=files, headers={"Idempotency-Key": key})
    assert response.status_code == 201
    return response.json()["run_id"]


def documents(client, run_id):
    for route in ("document", "verification-report"):
        assert client.post(f"/runs/{run_id}/{route}").status_code == 200


def accept(client, run_id, reviewed=False):
    return client.post(
        f"/runs/{run_id}/acceptance", json={"remarks_reviewed": reviewed}
    )


def metrics(client):
    response = client.get("/business-metrics")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


@pytest.mark.parametrize("version", ("1.0", "2.0"))
def test_accept_revoke_reaccept_restart_and_immutable_business(tmp_path, version):
    app = create_app(tmp_path, contract_version=version)
    with TestClient(app) as client:
        run_id = process(client)
        assert not client.get(f"/runs/{run_id}/acceptance").json()["accepted"]
        assert accept(client, run_id).json()["code"] == "acceptance_artifacts_required"
        documents(client, run_id)
        files = {
            r: client.get(f"/runs/{run_id}/{r}").content
            for r in ("document", "verification-report")
        }
        before = client.get(f"/runs/{run_id}").json()
        assert metrics(client)["average_unavailable_reason"] == "no_accepted_results"
        status = accept(client, run_id).json()
        assert status["accepted"] and status["eligible"] and len(status["history"]) == 1
        assert accept(client, run_id).json() == status
        assert metrics(client)["accepted_results"] == 1
        assert metrics(client)["average_cost_per_accepted"] == "0"
        assert client.post(f"/runs/{run_id}/acceptance/revoke").status_code == 200
        revoked = client.get(f"/runs/{run_id}/acceptance").json()
        assert not revoked["accepted"] and len(revoked["history"]) == 2
        assert client.post(f"/runs/{run_id}/acceptance/revoke").json() == revoked
        assert metrics(client)["accepted_results"] == 0
        assert accept(client, run_id).json()["history"][:2] == revoked["history"]
        assert client.get(f"/runs/{run_id}").json() == before
        for route, data in files.items():
            assert client.get(f"/runs/{run_id}/{route}").content == data
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/runs/{run_id}/acceptance").json()["accepted"]
        assert metrics(client)["accepted_results"] == 1
        saved = ProcessingResult.model_validate_json(
            json.dumps(client.get(f"/runs/{run_id}").json())
        )
        assert saved.contract_version == version
        assert result_fingerprint(saved) == result_fingerprint(
            ProcessingResult.model_validate_json(json.dumps(before))
        )
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM acceptance_events").fetchone() == (3,)


@pytest.mark.parametrize("scenario", ("B", "C", "V"))
def test_review_requires_explicit_acknowledgement(tmp_path, scenario):
    with TestClient(create_app(tmp_path)) as client:
        run_id = process(client, scenario)
        documents(client, run_id)
        before = client.get(f"/runs/{run_id}").json()
        assert before["needs_review"]
        response = accept(client, run_id)
        assert (
            response.status_code == 409
            and response.json()["code"] == "acceptance_review_required"
        )
        status = accept(client, run_id, True).json()
        assert status["accepted"] and status["history"][0]["remarks_reviewed"]
        assert client.get(f"/runs/{run_id}").json() == before
        assert metrics(client)["needs_review_runs"] == 1


def test_failed_and_partial_artifacts_cannot_be_accepted(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        failed = process(client, "T", "bad")
        assert accept(client, failed).json()["code"] == "acceptance_not_completed"
        run_id = process(client)
        assert client.post(f"/runs/{run_id}/document").status_code == 200
        assert accept(client, run_id).status_code == 409
        documents(client, run_id)
        file = tmp_path / run_id / "verification_report.docx"
        original = file.read_bytes()
        file.write_bytes(b"synthetic damage")
        assert accept(client, run_id).status_code == 409
        file.write_bytes(original)
        assert accept(client, run_id).status_code == 200
        file.write_bytes(b"synthetic damage")
        status = client.get(f"/runs/{run_id}/acceptance").json()
        assert status["accepted"] and not status["eligible"]
        # Historical approval does not assert current file availability; revoke
        # is still possible and never requires regeneration or an LLM.
        assert not client.post(f"/runs/{run_id}/acceptance/revoke").json()["accepted"]


def test_fraction_denominators_and_duplicate_uploads(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        empty = metrics(client)
        assert empty["total_runs"] == 0 and empty["completed_share"] is None
        assert empty["successful_input_share"] is None
        process(client, "A", "a")
        process(client, "B", "b")
        process(client, "T", "t")
        files = batch([FIXTURES / "all.docx"])["files"]
        duplicate = client.post("/runs", json={"files": files + files}).json()
        assert duplicate["processing_status"] == "completed"
        m = metrics(client)
        assert (m["total_runs"], m["completed_runs"], m["failed_runs"]) == (4, 3, 1)
        assert Decimal(m["completed_share"]) == Decimal("0.75")
        assert m["registered_inputs"] == 11 and m["successful_inputs"] == 8
        assert abs(Decimal(m["successful_input_share"]) - Decimal(8) / 11) < Decimal(
            "1e-25"
        )
        assert m["needs_review_runs"] == 2
        assert m["llm_attempts"] == m["retry_calls"] == m["unknown_cost_attempts"] == 0


@pytest.mark.parametrize("unknown", (False, True))
def test_economics_include_failed_linked_retry_all_attempts_and_no_double_count(
    tmp_path, unknown
):
    adapter, calls = adapter_for(("schema", "ok", "timeout"))
    app = create_app(tmp_path, adapter, tariffs=tariffs())
    files = batch([FIXTURES / "all.docx"])
    with TestClient(app) as client:
        parent = client.post(
            "/runs", json=files, headers={"Idempotency-Key": "failed"}
        ).json()["run_id"]
        child_payload = {**files, "retry_of": parent}
        child = client.post(
            "/runs", json=child_payload, headers={"Idempotency-Key": "child"}
        ).json()["run_id"]
        documents(client, child)
        assert accept(client, child).status_code == 200
        if unknown:
            client.post("/runs", json=files, headers={"Idempotency-Key": "timeout"})
        before = metrics(client)
        assert before["known_estimated_costs"] == {"RUB": "0.0368"}
        assert before["llm_attempts"] == (3 if unknown else 2)
        assert before["retry_calls"] == 1 and before["accepted_results"] == 1
        assert before["unknown_cost_attempts"] == int(unknown)
        assert before["average_cost_per_accepted"] == (None if unknown else "0.0368")
        assert before["simulated_runs"] == len(calls)
        assert (
            client.post(
                "/runs", json=child_payload, headers={"Idempotency-Key": "child"}
            ).json()["run_id"]
            == child
        )
        app.state.service.store.finish_attempt(
            app.state.service.store.get_attempts(child)[0]
        )
        assert metrics(client) == before
    with TestClient(create_app(tmp_path)) as client:
        assert metrics(client) == before
    assert len(calls) == (3 if unknown else 2)


def test_historical_multiple_attempts_same_run_and_missing_legacy_meter(tmp_path):
    adapter, calls = adapter_for()
    app = create_app(tmp_path, adapter, tariffs=tariffs())
    with TestClient(app) as client:
        run_id = client.post("/runs", json=batch([FIXTURES / "all.docx"])).json()[
            "run_id"
        ]
        documents(client, run_id)
        assert accept(client, run_id).status_code == 200
        store = app.state.service.store
        first = store.get_attempts(run_id)[0]
        started = AttemptRecord(
            run_id=run_id,
            provider=first.provider,
            model=first.model,
            attempt_no=2,
            started_at=datetime.now(timezone.utc),
            tariff=first.tariff,
        )
        store.start_attempt(started)
        terminal = first.model_copy(
            update={
                "attempt_no": 2,
                "started_at": started.started_at,
                "finished_at": datetime.now(timezone.utc),
            }
        )
        store.finish_attempt(terminal)
        m = metrics(client)
        assert m["llm_attempts"] == 2 and m["retry_calls"] == 1
        assert m["average_cost_per_accepted"] == "0.0368"
        with store.connection() as db:
            db.execute("DELETE FROM llm_attempts")
            db.execute("DELETE FROM llm_runs")
        m = metrics(client)
        assert m["untracked_runs"] == 1 and m["average_cost_per_accepted"] is None
    assert len(calls) == 1  # Extra ledger entry is a synthetic historical fixture.


def test_currencies_are_not_added_or_converted(tmp_path):
    for currency in ("RUB", "USD"):
        adapter, calls = adapter_for()
        with TestClient(
            create_app(tmp_path, adapter, tariffs=tariffs(currency=currency))
        ) as client:
            run_id = client.post("/runs", json=batch([FIXTURES / "all.docx"])).json()[
                "run_id"
            ]
            documents(client, run_id)
            assert accept(client, run_id).status_code == 200
        assert len(calls) == 1
    with TestClient(create_app(tmp_path)) as client:
        m = metrics(client)
        assert m["known_estimated_costs"] == {"RUB": "0.0184", "USD": "0.0184"}
        assert m["accepted_results"] == 2 and m["average_cost_per_accepted"] is None
        assert m["average_unavailable_reason"] == "multiple_currencies"


def test_acceptance_transaction_failure_rolls_back(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    original = acceptance.append_decision
    with TestClient(app) as client:
        run_id = process(client)
        documents(client, run_id)

        def fail(*args):
            original(*args)
            raise sqlite3.OperationalError("synthetic failure after INSERT")

        with monkeypatch.context() as patch:
            patch.setattr(acceptance, "append_decision", fail)
            assert accept(client, run_id).status_code == 503
        assert not client.get(f"/runs/{run_id}/acceptance").json()["history"]
        assert len(accept(client, run_id).json()["history"]) == 1


def test_concurrent_accept_is_one_event(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = process(client)
        documents(client, run_id)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            responses = await asyncio.gather(
                *(client.post(f"/runs/{run_id}/acceptance", json={}) for _ in range(3))
            )
            assert all(
                r.status_code == 200 and len(r.json()["history"]) == 1
                for r in responses
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("version", ("1.0", "2.0"))
def test_backup_restore_preserves_acceptance_history_metrics_and_docx(
    tmp_path, version
):
    data, copy, target = (tmp_path / n for n in ("data", "backup", "restored"))
    with TestClient(create_app(data, contract_version=version)) as client:
        run_id = process(client)
        documents(client, run_id)
        accept(client, run_id)
        client.post(f"/runs/{run_id}/acceptance/revoke")
        state = accept(client, run_id).json()
        before = metrics(client)
        hashes = {
            route: sha256(client.get(f"/runs/{run_id}/{route}").content).hexdigest()
            for route in ("document", "verification-report")
        }
        snapshot = client.get(f"/runs/{run_id}").json()
    assert backup(data, copy) == verify_backup(copy) == restore(copy, target)
    with TestClient(create_app(target)) as client:
        assert client.get(f"/runs/{run_id}/acceptance").json() == state
        assert metrics(client) == before
        assert client.get(f"/runs/{run_id}").json() == snapshot
        for route, digest in hashes.items():
            assert (
                sha256(client.get(f"/runs/{run_id}/{route}").content).hexdigest()
                == digest
            )
        assert (
            client.post(
                "/runs", json=payload(), headers={"Idempotency-Key": "one"}
            ).json()["run_id"]
            == run_id
        )


def test_legacy_stage8_backup_without_acceptance_table(tmp_path):
    data, copy, target = (tmp_path / n for n in ("data", "backup", "restored"))
    with TestClient(create_app(data)) as client:
        run_id = process(client)
        documents(client, run_id)
    with sqlite3.connect(data / "workflow.sqlite3") as db:
        db.execute("DROP TABLE acceptance_events")
    backup(data, copy)
    restore(copy, target)
    with TestClient(create_app(target)) as client:
        assert not client.get(f"/runs/{run_id}/acceptance").json()["accepted"]
        assert accept(client, run_id).status_code == 200


def test_corrupt_history_blocks_acceptance_metrics_and_backup(tmp_path):
    from document_automation.storage import StorageIntegrityError

    data, copy = tmp_path / "data", tmp_path / "backup"
    app = create_app(data)
    with TestClient(app) as client:
        run_id = process(client)
        documents(client, run_id)
        accept(client, run_id)
        with app.state.service.store.connection() as db:
            db.execute("UPDATE acceptance_events SET digest=?", ("0" * 64,))
        assert (
            client.get(f"/runs/{run_id}").status_code == 200
        )  # Business snapshot unaffected.
        assert client.get("/business-metrics").status_code == 409
        assert accept(client, run_id).status_code == 409
    with pytest.raises(StorageIntegrityError):
        backup(data, copy)
    assert not copy.exists()


def test_verify_rejects_resealed_invalid_acceptance_chain(tmp_path):
    from document_automation.storage import StorageIntegrityError

    data, copy = tmp_path / "data", tmp_path / "backup"
    with TestClient(create_app(data)) as client:
        run_id = process(client)
        documents(client, run_id)
        accept(client, run_id)
    backup(data, copy)
    with sqlite3.connect(copy / "workflow.sqlite3") as db:
        text = db.execute("SELECT record FROM acceptance_events").fetchone()[0]
        raw = json.loads(text)
        raw["previous_sha256"] = "1" * 64
        text = json.dumps(raw)
        db.execute(
            "UPDATE acceptance_events SET record=?,digest=?",
            (text, sha256(text.encode()).hexdigest()),
        )
    reseal_database(copy)
    with pytest.raises(StorageIntegrityError):
        verify_backup(copy)


@pytest.mark.parametrize("scenario", ("A", "B"))
def test_streamlit_accept_and_revoke_with_explicit_review(
    tmp_path, monkeypatch, scenario
):
    from streamlit.testing.v1 import AppTest

    with TestClient(create_app(tmp_path)) as client:

        def request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return client.request(
                method, url.replace("http://127.0.0.1:8000", ""), **kwargs
            )

        monkeypatch.setattr(httpx, "request", request)
        ui = AppTest.from_file(
            Path(__file__).resolve().parents[1]
            / "src"
            / "document_automation"
            / "ui.py",
            default_timeout=30,
        ).run()
        assert not ui.exception
        ui.selectbox[0].select(scenario).run()
        ui.button[0].click().run()
        assert any("Запусков: 1; completed: 1" in m.value for m in ui.markdown)
        accept_button = next(
            b
            for b in ui.button
            if b.label == "Проверено и принято для дальнейшей работы"
        )
        assert accept_button.disabled
        ui.button[1].click().run()
        ui.button[2].click().run()
        if scenario == "B":
            assert next(b for b in ui.button if b.label == accept_button.label).disabled
            ui.checkbox[0].check().run()
        next(b for b in ui.button if b.label == accept_button.label).click().run()
        assert not ui.exception
        run_id = ui.session_state["result"]["run_id"]
        assert client.get(f"/runs/{run_id}/acceptance").json()["accepted"]
        assert any("Принято оператором: 1" in m.value for m in ui.markdown)
        next(
            b for b in ui.button if b.label == "Отозвать принятие результата"
        ).click().run()
        assert not ui.exception
        assert not client.get(f"/runs/{run_id}/acceptance").json()["accepted"]


def test_inflight_run_is_distinct_from_completed_and_accepted(tmp_path):
    from test_run_requests import blocking_adapter

    async def exercise():
        started, release, calls = asyncio.Event(), asyncio.Event(), []
        app = create_app(
            tmp_path, blocking_adapter(started, release, calls), tariffs=tariffs()
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            files = batch([FIXTURES / "all.docx"])
            task = asyncio.create_task(
                client.post("/runs", json=files, headers={"Idempotency-Key": "pending"})
            )
            await started.wait()
            replay = await client.post(
                "/runs", json=files, headers={"Idempotency-Key": "pending"}
            )
            run_id = replay.json()["run_id"]
            status = await client.get(f"/runs/{run_id}/acceptance")
            assert status.status_code == 200 and not status.json()["eligible"]
            assert status.json()["block_code"] == "acceptance_not_completed"
            m = (await client.get("/business-metrics")).json()
            assert m["total_runs"] == m["unfinished_runs"] == m["llm_attempts"] == 1
            assert (
                m["completed_runs"]
                == m["successful_inputs"]
                == m["accepted_results"]
                == 0
            )
            assert m["unknown_cost_attempts"] == 1
            release.set()
            assert (await task).status_code == 201
            assert (
                await client.post(f"/runs/{run_id}/acceptance", json={})
            ).status_code == 409
        assert len(calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "usage", ("ABSENT", {}, {"input_tokens": -1, "output_tokens": 5})
)
def test_unknown_usage_never_produces_zero_average(tmp_path, usage):
    adapter, calls = adapter_for(usages=[usage])
    with TestClient(create_app(tmp_path, adapter, tariffs=tariffs())) as client:
        run_id = client.post("/runs", json=batch([FIXTURES / "all.docx"])).json()[
            "run_id"
        ]
        documents(client, run_id)
        assert accept(client, run_id).status_code == 200
        m = metrics(client)
        assert m["unknown_cost_attempts"] == 1 and m["known_estimated_costs"] == {}
        assert (
            m["average_cost_per_accepted"] is None
            and m["average_unavailable_reason"] == "unknown_cost"
        )
    assert len(calls) == 1
