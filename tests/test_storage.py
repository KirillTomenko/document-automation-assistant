import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_api import payload

from document_automation.api import create_app
from document_automation.storage import ResultStore, StorageIntegrityError


def test_restart_preserves_result_artifact_and_audit(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        run = client.post("/runs", json=payload()).json()["run_id"]
        assert client.post(f"/runs/{run}/document").status_code == 200
        assert client.post(f"/runs/{run}/verification-report").status_code == 200
        original = client.get(f"/runs/{run}/document").content
        original_report = client.get(f"/runs/{run}/verification-report").content
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/runs/{run}/document").content == original
        assert client.get(f"/runs/{run}/verification-report").content == original_report
        result = client.get(f"/runs/{run}").json()
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == len(
            result["audit"]
        )
        assert db.execute("SELECT count(*) FROM inputs").fetchone()[0] == 3


@pytest.mark.parametrize("target", ["snapshot", "audit", "input", "artifact", "report"])
def test_corruption_blocks_use(tmp_path, target):
    with TestClient(create_app(tmp_path)) as client:
        run = client.post("/runs", json=payload()).json()["run_id"]
        client.post(f"/runs/{run}/document")
        client.post(f"/runs/{run}/verification-report")
        if target in ("snapshot", "audit"):
            with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
                db.execute(
                    "UPDATE runs SET digest='wrong'"
                    if target == "snapshot"
                    else "DELETE FROM events WHERE sequence=0"
                )
            assert client.get(f"/runs/{run}").status_code == 409
        elif target == "input":
            next((tmp_path / run / "inputs").glob("*")).write_bytes(b"changed")
            assert client.post(f"/runs/{run}/document").status_code == 409
        else:
            filename = (
                "verification_report.docx"
                if target == "report"
                else "work_request.docx"
            )
            (tmp_path / run / filename).write_bytes(b"changed")
            route = "verification-report" if target == "report" else "document"
            assert client.get(f"/runs/{run}/{route}").status_code == 409
            assert not client.get(f"/runs/{run}").json()["audit"][-1]["decision"][
                "allowed"
            ]


def test_audit_cannot_be_rewritten(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        run = client.post("/runs", json=payload()).json()["run_id"]
    store = ResultStore(tmp_path)
    result = store.get(run)
    with pytest.raises(StorageIntegrityError):
        store.save(result.model_copy(update={"audit": ()}))
    assert store.get(run) == result


def test_interrupted_run_recovers_as_failed(tmp_path):
    store = ResultStore(tmp_path)
    run = "a" * 32
    store.begin(run, [])
    recovered = ResultStore(tmp_path).get(run)
    assert recovered.processing_status == "failed"
    assert recovered.audit[-1].error_code == "processing_interrupted"
