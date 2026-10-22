"""Synthetic failure injection at staged publication and SQLite boundaries."""

import json
import sqlite3
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from test_api import payload
from test_formats_v2 import FIXTURES, batch
from test_llm_usage import adapter_for, tariffs

from document_automation.api import create_app
from document_automation.artifact_files import FIELDS, ArtifactFiles
from document_automation.generation import (
    generate_document,
    generate_verification_report,
)
from document_automation.schemas import ProcessingResult, result_fingerprint
from document_automation.storage import StorageIntegrityError

KINDS = ("document", "report")
ROUTES = {"document": "document", "report": "verification-report"}


def fingerprint(raw):
    return result_fingerprint(ProcessingResult.model_validate_json(json.dumps(raw)))


def route(run_id, kind):
    return f"/runs/{run_id}/{ROUTES[kind]}"


def states(client, run_id):
    response = client.get(f"/runs/{run_id}/artifacts")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("version", ("1.0", "2.0"))
@pytest.mark.parametrize("kind", KINDS)
def test_first_repeat_download_restart_keep_exact_bytes(tmp_path, version, kind):
    app = create_app(tmp_path, contract_version=version)
    with TestClient(app) as client:
        original = client.post(
            "/runs", json=payload(), headers={"Idempotency-Key": "action"}
        ).json()
        run_id = original["run_id"]
        assert states(client, run_id)[kind]["state"] == "not_generated"
        first = client.post(route(run_id, kind))
        assert first.status_code == 200, first.text
        meta = first.json()[FIELDS[kind]]
        path = tmp_path / run_id / meta["filename"]
        data, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        for _ in range(2):
            assert client.post(route(run_id, kind)).json()[FIELDS[kind]] == meta
            assert client.get(route(run_id, kind)).content == data
        result = client.get(f"/runs/{run_id}").json()
        assert path.stat().st_mtime_ns == timestamp
        assert sha256(data).hexdigest() == meta["sha256"]
        assert fingerprint(result) == fingerprint(original)
        assert sum(e["event"] == "generated" for e in result["audit"]) == 1
        assert len(app.state.service.store.artifact_writes(run_id, kind)) == 1
        assert not list((tmp_path / run_id).glob("*.tmp"))
    with TestClient(create_app(tmp_path)) as client:
        assert states(client, run_id)[kind]["state"] == "verified"
        assert client.get(route(run_id, kind)).content == data
        assert client.post(route(run_id, kind)).json()[FIELDS[kind]] == meta
        assert client.get(f"/runs/{run_id}/usage").json()["attempts"] == 0


def inject_fault(monkeypatch, app, fault):
    store = app.state.service.store
    write_stage, publish = ArtifactFiles.write_stage, ArtifactFiles.publish
    save, finish = store.save, store._finish_artifact

    def fail_stage(self, write, data):
        if fault == "partial_stage":
            self.path(self.stage_name(write)).write_bytes(data[:17])
        raise OSError("synthetic stage interruption")

    def fail_publish(self, write):
        if fault == "after_publish":
            publish(self, write)
        raise OSError("synthetic publication interruption")

    def fail_save(result, **kwargs):
        if kwargs.get("artifact_write"):
            if fault == "after_commit":
                save(result, **kwargs)
            raise sqlite3.OperationalError("synthetic commit interruption")
        return save(result, **kwargs)

    def fail_finish(db, write, state):
        finish(db, write, state)
        if state == "committed":
            raise sqlite3.OperationalError("synthetic rollback after SQL updates")

    if fault in ("before_stage", "partial_stage"):
        monkeypatch.setattr(ArtifactFiles, "write_stage", fail_stage)
    elif fault in ("after_stage", "after_publish"):
        monkeypatch.setattr(ArtifactFiles, "publish", fail_publish)
    elif fault == "after_sql_updates":
        monkeypatch.setattr(store, "_finish_artifact", fail_finish)
    else:
        monkeypatch.setattr(store, "save", fail_save)
    return write_stage


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "fault",
    (
        "before_stage",
        "partial_stage",
        "after_stage",
        "after_publish",
        "before_commit",
        "after_sql_updates",
        "after_commit",
    ),
)
def test_fault_restart_explicit_recovery_preserves_other_docx(
    tmp_path, monkeypatch, kind, fault
):
    app = create_app(tmp_path)
    other = "report" if kind == "document" else "document"
    with TestClient(app) as client:
        original = client.post("/runs", json=payload()).json()
        run_id = original["run_id"]
        assert client.post(route(run_id, other)).status_code == 200
        other_bytes = client.get(route(run_id, other)).content
        usage = client.get(f"/runs/{run_id}/usage").json()
        with monkeypatch.context() as patch:
            inject_fault(patch, app, fault)
            failed = client.post(route(run_id, kind))
            assert failed.status_code == 503, failed.text
        saved = client.get(f"/runs/{run_id}").json()
        assert fingerprint(saved) == fingerprint(original)
        assert bool(saved[FIELDS[kind]]) == (fault == "after_commit")
        assert client.get(route(run_id, other)).content == other_bytes
    with TestClient(create_app(tmp_path)) as client:
        if fault == "after_commit":
            assert states(client, run_id)[kind]["state"] == "verified"
            assert client.post(route(run_id, kind)).status_code == 200
        else:
            status = states(client, run_id)[kind]
            assert status["state"] == "recovery_required" and not status["verified"]
            assert status["pending_operations"] == 1
            assert client.get(route(run_id, kind)).status_code == 409
            assert (
                client.post(route(run_id, kind)).json()["code"]
                == "artifact_recovery_required"
            )
        recovery = client.post(route(run_id, kind) + "/recover")
        assert recovery.status_code == 200, recovery.text
        recovered = recovery.json()
        recovered_bytes = client.get(route(run_id, kind)).content
        assert states(client, run_id)[kind]["state"] == "verified"
        assert (
            client.post(route(run_id, kind) + "/recover").json()[FIELDS[kind]]
            == recovered[FIELDS[kind]]
        )
        assert client.get(route(run_id, kind)).content == recovered_bytes
        assert client.get(route(run_id, other)).content == other_bytes
        assert client.get(f"/runs/{run_id}/usage").json() == usage
        assert fingerprint(recovered) == fingerprint(original)
        assert not list((tmp_path / run_id).glob("*.tmp"))
        writes = client.app.state.service.store.artifact_writes(run_id, kind)
        assert [w.state for w in writes] == (
            ["committed"] if fault == "after_commit" else ["abandoned", "committed"]
        )
        if fault not in ("before_stage", "after_commit"):
            assert list((tmp_path / run_id / ".artifact-recovery").iterdir())
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("damage", ("missing", "corrupt"))
def test_damage_requires_explicit_repair_preserves_previous_metadata(
    tmp_path, kind, damage
):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        for artifact in KINDS:
            assert client.post(route(run_id, artifact)).status_code == 200
        before = client.get(f"/runs/{run_id}").json()
        old_meta = before[FIELDS[kind]]
        path = tmp_path / run_id / old_meta["filename"]
        other = "report" if kind == "document" else "document"
        other_data = client.get(route(run_id, other)).content
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"synthetic damaged file")
        assert states(client, run_id)[kind]["state"] == "damaged"
        assert client.post(route(run_id, kind)).json()["code"] == "artifact_damaged"
        assert client.get(route(run_id, kind)).status_code == 409
        repaired = client.post(route(run_id, kind) + "/recover")
        assert repaired.status_code == 200, repaired.text
        assert fingerprint(repaired.json()) == fingerprint(before)
        assert client.get(route(run_id, other)).content == other_data
        assert client.get(route(run_id, kind)).status_code == 200
        write = app.state.service.store.artifact_writes(run_id, kind)[-1]
        assert write.previous_metadata.model_dump(mode="json") == old_meta
        if damage == "corrupt":
            isolated = list((tmp_path / run_id / ".artifact-recovery").iterdir())
            assert (
                len(isolated) == 1
                and isolated[0].read_bytes() == b"synthetic damaged file"
            )


@pytest.mark.parametrize("kind", KINDS)
def test_valid_orphan_and_legacy_temporary_are_not_auto_confirmed(tmp_path, kind):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        result = app.state.service.get(run_id)
        candidate, data = (
            generate_document(result)
            if kind == "document"
            else generate_verification_report(result)
        )
        path = tmp_path / run_id / getattr(candidate, FIELDS[kind]).filename
        path.write_bytes(data)
        path.with_suffix(".tmp").write_bytes(b"partial legacy write")
    with TestClient(create_app(tmp_path)) as client:
        assert states(client, run_id)[kind]["state"] == "recovery_required"
        assert client.get(f"/runs/{run_id}").json()[FIELDS[kind]] is None
        assert client.get(route(run_id, kind)).status_code == 409
        assert client.post(route(run_id, kind)).status_code == 409
        repaired = client.post(route(run_id, kind) + "/recover")
        assert repaired.status_code == 200, repaired.text
        isolated = [
            p.read_bytes() for p in (tmp_path / run_id / ".artifact-recovery").iterdir()
        ]
        assert data in isolated and b"partial legacy write" in isolated


@pytest.mark.parametrize("kind", KINDS)
def test_bad_staging_or_bad_published_bytes_never_get_metadata(
    tmp_path, monkeypatch, kind
):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        original = ArtifactFiles.write_stage

        def tamper(self, write, data):
            original(self, write, b"malformed DOCX")

        with monkeypatch.context() as patch:
            patch.setattr(ArtifactFiles, "write_stage", tamper)
            failed = client.post(route(run_id, kind))
        assert (
            failed.status_code == 409
            and failed.json()["code"] == "artifact_verification_failed"
        )
        assert client.get(f"/runs/{run_id}").json()[FIELDS[kind]] is None
        assert client.get(route(run_id, kind)).status_code == 409
        assert client.post(route(run_id, kind) + "/recover").status_code == 200


def test_publication_refuses_to_overwrite_file_created_after_preflight(
    tmp_path, monkeypatch
):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        original = ArtifactFiles.publish

        def collision(self, write):
            self.path(write.filename).write_bytes(b"existing file must survive")
            original(self, write)

        with monkeypatch.context() as patch:
            patch.setattr(ArtifactFiles, "publish", collision)
            assert client.post(route(run_id, "document")).status_code == 503
        path = tmp_path / run_id / "work_request.docx"
        assert path.read_bytes() == b"existing file must survive"
        assert client.get(f"/runs/{run_id}").json()["generated_document"] is None
        assert client.post(route(run_id, "document") + "/recover").status_code == 200
        assert any(
            p.read_bytes() == b"existing file must survive"
            for p in (tmp_path / run_id / ".artifact-recovery").iterdir()
        )


def test_metadata_cannot_be_saved_without_verified_publication(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
    store = app.state.service.store
    before = store.get(run_id)
    candidate, _ = generate_document(before)
    with pytest.raises(StorageIntegrityError):
        store.save(candidate)
    assert store.get(run_id) == before


def test_generation_and_recovery_preserve_idempotency_lineage_and_costs(
    tmp_path, monkeypatch
):
    adapter, calls = adapter_for(("schema", "ok"))
    app = create_app(tmp_path, adapter, tariffs=tariffs())
    uploads = batch([FIXTURES / "all.docx"])
    with TestClient(app) as client:
        parent = client.post(
            "/runs", json=uploads, headers={"Idempotency-Key": "parent"}
        ).json()["run_id"]
        linked = {**uploads, "retry_of": parent}
        result = client.post(
            "/runs", json=linked, headers={"Idempotency-Key": "child"}
        ).json()
        run_id = result["run_id"]
        usage = client.get(f"/runs/{run_id}/usage").json()
        with monkeypatch.context() as patch:
            inject_fault(patch, app, "after_publish")
            assert client.post(route(run_id, "document")).status_code == 503
    restarted, restart_calls = adapter_for()
    with TestClient(create_app(tmp_path, restarted)) as client:
        assert (
            client.post(
                "/runs", json=linked, headers={"Idempotency-Key": "child"}
            ).json()["run_id"]
            == run_id
        )
        assert client.get(f"/runs/{run_id}/operation").json()["retry_of"] == parent
        for kind in KINDS:
            assert client.post(route(run_id, kind) + "/recover").status_code == 200
            assert client.post(route(run_id, kind)).status_code == 200
            assert client.get(route(run_id, kind)).status_code == 200
        assert client.get(f"/runs/{run_id}/usage").json() == usage
    assert len(calls) == 2 and not restart_calls


@pytest.mark.parametrize("version", ("1.0", "2.0"))
def test_legacy_confirmed_files_without_write_journal_remain_available(
    tmp_path, version
):
    with TestClient(create_app(tmp_path, contract_version=version)) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        artifacts = {}
        for kind in KINDS:
            assert client.post(route(run_id, kind)).status_code == 200
            artifacts[kind] = client.get(route(run_id, kind)).content
        before = client.get(f"/runs/{run_id}").content
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
        db.execute("DROP TABLE artifact_writes")
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/runs/{run_id}").content == before
        for kind, data in artifacts.items():
            assert states(client, run_id)[kind]["verified"]
            assert client.post(route(run_id, kind)).status_code == 200
            assert client.get(route(run_id, kind)).content == data
        assert client.app.state.service.store.artifact_writes(run_id, "document") == ()
