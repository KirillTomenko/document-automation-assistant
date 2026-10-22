"""Recovery interruption, late corruption, permissions and the Streamlit action."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import payload
from test_artifact_recovery import KINDS, inject_fault, route, states

from document_automation.api import create_app
from document_automation.artifact_files import FIELDS, ArtifactFiles


@pytest.mark.parametrize("phase", ("published", "before_commit", "missing"))
@pytest.mark.parametrize("kind", KINDS)
def test_late_corruption_never_gets_confirmed_metadata(
    tmp_path, monkeypatch, kind, phase
):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        publish, save = ArtifactFiles.publish, app.state.service.store.save

        def tamper_published(self, write):
            publish(self, write)
            if phase == "missing":
                self.path(write.filename).unlink()
            else:
                self.path(write.filename).write_bytes(b"malformed published DOCX")

        def tamper_at_commit(result, **kwargs):
            write = kwargs.get("artifact_write")
            if write:
                (tmp_path / run_id / write.filename).write_bytes(
                    b"changed before commit"
                )
            return save(result, **kwargs)

        with monkeypatch.context() as patch:
            if phase == "before_commit":
                patch.setattr(app.state.service.store, "save", tamper_at_commit)
            else:
                patch.setattr(ArtifactFiles, "publish", tamper_published)
            failed = client.post(route(run_id, kind))
        assert (
            failed.status_code == 409
            and failed.json()["code"] == "artifact_verification_failed"
        )
        assert client.get(f"/runs/{run_id}").json()[FIELDS[kind]] is None
        assert client.get(route(run_id, kind)).status_code == 409
        assert client.post(route(run_id, kind) + "/recover").status_code == 200


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fault", ("isolation_unlink", "abandon_commit"))
def test_recovery_itself_can_be_repeated_after_interruption(
    tmp_path, monkeypatch, kind, fault
):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        with monkeypatch.context() as patch:
            inject_fault(patch, app, "after_publish")
            assert client.post(route(run_id, kind)).status_code == 503
        write = app.state.service.store.artifact_writes(run_id, kind)[0]
        path = tmp_path / run_id / write.filename
        unconfirmed = path.read_bytes()
        unlink, save = Path.unlink, app.state.service.store.save

        def fail_unlink(self, *args, **kwargs):
            if self == path:
                raise OSError("synthetic interruption after quarantine hard link")
            return unlink(self, *args, **kwargs)

        def fail_save(result, **kwargs):
            if kwargs.get("abandon_write"):
                raise sqlite3.OperationalError(
                    "synthetic interruption of recovery commit"
                )
            return save(result, **kwargs)

        with monkeypatch.context() as patch:
            if fault == "isolation_unlink":
                patch.setattr(Path, "unlink", fail_unlink)
            else:
                patch.setattr(app.state.service.store, "save", fail_save)
            assert client.post(route(run_id, kind) + "/recover").status_code == 503
        assert client.get(route(run_id, kind)).status_code == 409
    with TestClient(create_app(tmp_path)) as client:
        assert states(client, run_id)[kind]["state"] == "recovery_required"
        response = client.post(route(run_id, kind) + "/recover")
        assert response.status_code == 200, response.text
        assert client.get(route(run_id, kind)).status_code == 200
        assert any(
            p.read_bytes() == unconfirmed
            for p in (tmp_path / run_id / ".artifact-recovery").iterdir()
        )


@pytest.mark.parametrize("kind", KINDS)
def test_failure_before_intent_can_be_repeated_normally(tmp_path, monkeypatch, kind):
    import document_automation.service as service_module

    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]

        def fail_render(result):
            raise OSError("synthetic rendering interruption before intent")

        with monkeypatch.context() as patch:
            patch.setattr(
                service_module,
                "generate_document"
                if kind == "document"
                else "generate_verification_report",
                fail_render,
            )
            assert client.post(route(run_id, kind)).status_code == 503
        assert app.state.service.store.artifact_writes(run_id, kind) == ()
        assert states(client, run_id)[kind]["state"] == "not_generated"
        assert client.post(route(run_id, kind)).status_code == 200


def test_cleanup_of_stray_temp_never_moves_confirmed_file(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        meta = client.post(route(run_id, "document")).json()["generated_document"]
        path = tmp_path / run_id / meta["filename"]
        data, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        (tmp_path / run_id / "work_request.tmp").write_bytes(
            b"legacy incomplete staging"
        )
        status = states(client, run_id)["document"]
        assert status["verified"] and status["state"] == "recovery_required"
        assert client.get(route(run_id, "document")).content == data
        assert client.post(route(run_id, "document") + "/recover").status_code == 200
        assert states(client, run_id)["document"]["state"] == "verified"
        assert path.read_bytes() == data and path.stat().st_mtime_ns == timestamp
        assert (
            app.state.service.get(run_id).generated_document.model_dump(mode="json")
            == meta
        )


def test_corrupt_write_journal_blocks_only_affected_artifact(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        for kind in KINDS:
            assert client.post(route(run_id, kind)).status_code == 200
        report = client.get(route(run_id, "report")).content
        with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
            db.execute(
                "UPDATE artifact_writes SET digest='corrupt' WHERE kind='document'"
            )
        assert (
            client.post(route(run_id, "document")).json()["code"] == "storage_integrity"
        )
        assert client.post(route(run_id, "document") + "/recover").status_code == 409
        assert client.get(route(run_id, "report")).content == report
        assert client.get(f"/runs/{run_id}").status_code == 200


@pytest.mark.parametrize("kind", KINDS)
def test_sql_failure_after_metadata_insert_rolls_back_entire_commit(tmp_path, kind):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        before = client.get(f"/runs/{run_id}").content
        # The final runs UPDATE happens after journal, audit and artifact metadata
        # statements. A real SQLite abort must roll ALL of those changes back.
        with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
            db.execute("""CREATE TRIGGER synthetic_abort_snapshot BEFORE UPDATE ON runs
                BEGIN SELECT RAISE(ABORT, 'synthetic snapshot update failure'); END""")
        assert client.post(route(run_id, kind)).status_code == 503
        assert client.get(f"/runs/{run_id}").content == before
        with sqlite3.connect(tmp_path / "workflow.sqlite3") as db:
            table = "artifacts" if kind == "document" else "report_artifacts"
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
            assert db.execute("SELECT state FROM artifact_writes").fetchone() == (
                "prepared",
            )
            db.execute("DROP TRIGGER synthetic_abort_snapshot")
        assert states(client, run_id)[kind]["state"] == "recovery_required"
        assert client.post(route(run_id, kind) + "/recover").status_code == 200


@pytest.mark.parametrize("kind", KINDS)
def test_recovery_cannot_bypass_failed_run_permissions(tmp_path, kind):
    with TestClient(create_app(tmp_path)) as client:
        broken = {"files": [{"filename": "broken.pdf", "content_base64": "YQ=="}]}
        run_id = client.post("/runs", json=broken).json()["run_id"]
        assert client.post(route(run_id, kind) + "/recover").status_code == 409
        assert client.app.state.service.store.artifact_writes(run_id, kind) == ()


def test_streamlit_offers_explicit_recovery_and_preserves_other_docx(
    tmp_path, monkeypatch
):
    import httpx
    from streamlit.testing.v1 import AppTest

    app = create_app(tmp_path)
    with TestClient(app) as client:

        def request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return client.request(
                method, url.replace("http://127.0.0.1:8000", ""), **kwargs
            )

        monkeypatch.setattr(httpx, "request", request)
        ui = AppTest.from_file(
            str(Path(__file__).parents[1] / "src/document_automation/ui.py"),
            default_timeout=30,
        ).run()
        ui.selectbox[0].select("A").run()
        ui.button[0].click().run()
        assert not ui.exception
        run_id = ui.session_state["result"]["run_id"]
        assert client.post(route(run_id, "report")).status_code == 200
        report = client.get(route(run_id, "report")).content
        with monkeypatch.context() as patch:
            inject_fault(patch, app, "after_publish")
            assert client.post(route(run_id, "document")).status_code == 503
        ui.run()
        recovery = next(
            b for b in ui.button if b.label == "Восстановить рабочую заявку"
        )
        recovery.click().run()
        assert not ui.exception
        assert ui.session_state["result"]["generated_document"] is not None
        assert states(client, run_id)["document"]["verified"]
        assert client.get(route(run_id, "report")).content == report
