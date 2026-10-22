"""Offline backup tests use only synthetic documents and mock provider responses."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import payload
from test_artifact_recovery import inject_fault
from test_formats_v2 import FIXTURES, batch
from test_llm_usage import adapter_for, tariffs

from document_automation.api import create_app
from document_automation.backup import (
    DATABASE,
    MANIFEST,
    MANIFEST_HASH,
    backup,
    main,
    restore,
    verify_backup,
)
from document_automation.backup_paths import BackupError, checksum, relative_path
from document_automation.data_lease import DataBusyError, DataLease
from document_automation.schemas import ProcessingResult, result_fingerprint
from document_automation.service import DocumentService
from document_automation.storage import StorageIntegrityError

ROOT = Path(__file__).resolve().parents[1]


def tree_bytes(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def make_data(root, version="2.0", documents=True):
    with TestClient(create_app(root, contract_version=version)) as client:
        raw = client.post(
            "/runs", json=payload(), headers={"Idempotency-Key": "one-action"}
        ).json()
        run_id = raw["run_id"]
        if documents:
            assert client.post(f"/runs/{run_id}/document").status_code == 200
            assert client.post(f"/runs/{run_id}/verification-report").status_code == 200
    return run_id


@pytest.fixture(scope="module")
def sample_backup(tmp_path_factory):
    root = tmp_path_factory.mktemp("valid-backup")
    data = root / "data"
    run_id = make_data(data)
    backup(data, root / "backup")
    return root / "backup", run_id


def clone(sample_backup, tmp_path):
    original, run_id = sample_backup
    target = tmp_path / "backup"
    shutil.copytree(original, target)
    return target, run_id


def rewrite_manifest(root, mutate):
    raw = json.loads((root / MANIFEST).read_bytes())
    mutate(raw)
    data = json.dumps(raw, ensure_ascii=False).encode()
    (root / MANIFEST).write_bytes(data)
    (root / MANIFEST_HASH).write_bytes((sha256(data).hexdigest() + "\n").encode())


def reseal_database(root):
    size, digest = checksum(root / DATABASE)
    rewrite_manifest(
        root,
        lambda raw: next(e for e in raw["files"] if e["path"] == DATABASE).update(
            size=size, sha256=digest
        ),
    )


@pytest.mark.parametrize("version", ("1.0", "2.0"))
def test_roundtrip_preserves_source_results_artifacts_and_idempotency(
    tmp_path, version
):
    data, copy, restored = tmp_path / "data", tmp_path / "backup", tmp_path / "restored"
    run_id = make_data(data, version)
    before = tree_bytes(data)
    report = backup(data, copy)
    assert report.runs == 1 and report.documents == report.reports == 1
    assert tree_bytes(data) == before
    assert verify_backup(copy) == report and restore(copy, restored) == report
    assert {k: v[0] for k, v in tree_bytes(restored).items() if k != DATABASE} == {
        k: v[0] for k, v in before.items() if k != DATABASE
    }
    with sqlite3.connect(data / DATABASE) as db:
        original = db.execute("SELECT snapshot FROM runs").fetchone()[0]
    no_network, calls = adapter_for()
    with TestClient(create_app(restored, no_network)) as client:
        result = client.get(f"/runs/{run_id}").json()
        assert result == json.loads(original)
        assert result_fingerprint(
            ProcessingResult.model_validate_json(json.dumps(result))
        ) == result_fingerprint(ProcessingResult.model_validate_json(original))
        replay = client.post(
            "/runs", json=payload(), headers={"Idempotency-Key": "one-action"}
        )
        assert replay.status_code == 200 and replay.json()["run_id"] == run_id
        for field, route in (
            ("generated_document", "document"),
            ("generated_report", "verification-report"),
        ):
            downloaded = client.get(f"/runs/{run_id}/{route}")
            assert downloaded.status_code == 200
            assert sha256(downloaded.content).hexdigest() == result[field]["sha256"]
        assert client.get(f"/runs/{run_id}/usage").json()["attempts"] == 0
    assert not calls and tree_bytes(data) == before


def test_mock_usage_retry_lineage_and_unknown_expense_survive_restore(tmp_path):
    data = tmp_path / "data"
    adapter, calls = adapter_for(("schema", "ok", "timeout"))
    uploads = batch([FIXTURES / "all.docx"])
    with TestClient(create_app(data, adapter, tariffs=tariffs())) as client:
        parent = client.post(
            "/runs", json=uploads, headers={"Idempotency-Key": "parent"}
        ).json()["run_id"]
        linked = {**uploads, "retry_of": parent}
        child = client.post(
            "/runs", json=linked, headers={"Idempotency-Key": "child"}
        ).json()["run_id"]
        unknown = client.post(
            "/runs", json=uploads, headers={"Idempotency-Key": "unknown"}
        ).json()["run_id"]
        for route in ("document", "verification-report"):
            assert client.post(f"/runs/{child}/{route}").status_code == 200
        usage = {
            run_id: client.get(f"/runs/{run_id}/usage").json()
            for run_id in (parent, child, unknown)
        }
    assert usage[unknown]["estimated_llm_api_cost"] is None
    backup(data, tmp_path / "backup")
    restore(tmp_path / "backup", tmp_path / "restored")
    restarted, new_calls = adapter_for()
    with TestClient(create_app(tmp_path / "restored", restarted)) as client:
        for run_id, expected in usage.items():
            assert client.get(f"/runs/{run_id}/usage").json() == expected
        assert client.get(f"/runs/{child}/operation").json()["retry_of"] == parent
        assert (
            client.post(
                "/runs", json=linked, headers={"Idempotency-Key": "child"}
            ).json()["run_id"]
            == child
        )
        assert (
            client.post(
                "/runs", json=uploads, headers={"Idempotency-Key": "unknown"}
            ).json()["run_id"]
            == unknown
        )
        assert not client.get(f"/runs/{unknown}/operation").json()["can_retry"]
    assert len(calls) == 3 and not new_calls


def test_running_idle_backend_and_maintenance_are_mutually_exclusive(tmp_path):
    data = tmp_path / "data"
    app = create_app(data)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        with pytest.raises(DataBusyError):
            backup(data, tmp_path / "copy")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "document_automation.backup",
                "backup",
                "--data",
                str(data),
                "--output",
                str(tmp_path / "copy"),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert (
            result.returncode == 1 and json.loads(result.stdout)["code"] == "data_busy"
        )
    with DataLease(data, "maintenance"):
        with pytest.raises(DataBusyError):
            create_app(data)
    assert not (tmp_path / "copy").exists()
    backup(data, tmp_path / "copy")


def test_maintenance_prevents_backend_creating_restore_target(tmp_path):
    target = tmp_path / "new-data"
    with DataLease(target, "maintenance"):
        with pytest.raises(DataBusyError):
            create_app(target)
    assert not target.exists()


def test_direct_processing_holds_lease_while_awaiting_provider(tmp_path):
    from test_run_requests import blocking_adapter

    async def scenario():
        started, release, calls = asyncio.Event(), asyncio.Event(), []
        service = DocumentService(
            tmp_path / "data", blocking_adapter(started, release, calls)
        )
        task = asyncio.create_task(
            service.process(batch([FIXTURES / "all.docx"])["files"])
        )
        await asyncio.wait_for(started.wait(), 5)
        try:
            with pytest.raises(DataBusyError):
                backup(service.root, tmp_path / "copy")
        finally:
            release.set()
            await task
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ("document", "report"))
def test_pending_artifact_backup_refused_without_recovery_or_source_changes(
    tmp_path, monkeypatch, kind
):
    data = tmp_path / "data"
    app = create_app(data)
    with TestClient(app) as client:
        run_id = client.post("/runs", json=payload()).json()["run_id"]
        with monkeypatch.context() as patch:
            inject_fault(patch, app, "after_publish")
            route = "document" if kind == "document" else "verification-report"
            assert client.post(f"/runs/{run_id}/{route}").status_code == 503
    before = tree_bytes(data)
    with pytest.raises(BackupError, match="7B"):
        backup(data, tmp_path / "copy")
    assert tree_bytes(data) == before and not (tmp_path / "copy").exists()


def test_pending_run_is_not_automatically_recovered_by_backup(tmp_path):
    service = DocumentService(tmp_path / "data")
    service.store.begin("c" * 32, [])
    before = tree_bytes(service.root)
    with pytest.raises(BackupError) as exc:
        backup(service.root, tmp_path / "copy")
    assert exc.value.code == "recovery_required" and tree_bytes(service.root) == before


@pytest.mark.parametrize(
    "mutation",
    ("corrupt", "missing", "extra", "empty_directory", "manifest", "manifest_checksum"),
)
def test_invalid_backup_rejected_and_restore_target_unchanged(
    sample_backup, tmp_path, mutation
):
    root, run_id = clone(sample_backup, tmp_path)
    artifact = root / run_id / "work_request.docx"
    if mutation == "corrupt":
        artifact.write_bytes(b"CORRUPTED-PRIVATE-DOCUMENT")
    elif mutation == "missing":
        artifact.unlink()
    elif mutation == "extra":
        (root / ".env").write_text("PRIVATE-SECRET", encoding="utf-8")
    elif mutation == "empty_directory":
        (root / "extra").mkdir()
    elif mutation == "manifest":
        (root / MANIFEST).write_bytes((root / MANIFEST).read_bytes() + b" ")
    else:
        (root / MANIFEST_HASH).write_text("0" * 64 + "\n", encoding="ascii")
    with pytest.raises((BackupError, OSError)):
        verify_backup(root)
    target = tmp_path / "restored"
    with pytest.raises((BackupError, OSError)):
        restore(root, target)
    assert not target.exists()


@pytest.mark.parametrize(
    "name",
    (
        "../escape.docx",
        "/absolute.docx",
        "C:/escape.docx",
        "C:escape.docx",
        "\\\\server\\share\\x",
        "folder\\x",
        "folder//x",
        "./x",
        "folder/../x",
        "NUL.docx",
        "foo/CON",
        "file.docx:stream",
        "trailing. ",
        "x\x00y",
        ".",
        "COM¹.docx",
        "CONIN$",
    ),
)
def test_unsafe_relative_paths_are_rejected_even_after_manifest_resealed(
    sample_backup, tmp_path, name
):
    with pytest.raises(BackupError):
        relative_path(name)
    root, _ = clone(sample_backup, tmp_path)
    rewrite_manifest(root, lambda raw: raw["files"][0].update(path=name))
    with pytest.raises(BackupError):
        verify_backup(root)


@pytest.mark.parametrize(
    "change",
    (
        "drop_entry",
        "role",
        "result_hash",
        "version",
        "missing_version",
        "boolean_version",
        "duplicate_case",
    ),
)
def test_resealed_manifest_must_match_sqlite(sample_backup, tmp_path, change):
    root, _ = clone(sample_backup, tmp_path)

    def mutate(raw):
        if change == "drop_entry":
            raw["files"].pop()
        elif change == "role":
            raw["files"][0]["role"] = "report"
        elif change == "result_hash":
            raw["results"][0]["result_sha256"] = "0" * 64
        elif change == "version":
            raw["format_version"] = 2
        elif change == "missing_version":
            raw.pop("format_version")
        elif change == "boolean_version":
            raw["format_version"] = True
        else:
            raw["files"].append(
                {**raw["files"][0], "path": raw["files"][0]["path"].upper()}
            )

    rewrite_manifest(root, mutate)
    with pytest.raises((BackupError, ValueError)):
        verify_backup(root)


@pytest.mark.parametrize(
    "damage", ("integrity", "foreign_key", "audit", "input_path", "extra_table")
)
def test_resealed_database_still_requires_domain_and_sqlite_integrity(
    sample_backup, tmp_path, damage
):
    root, run_id = clone(sample_backup, tmp_path)
    if damage == "integrity":
        (root / DATABASE).write_bytes(b"not a database")
    else:
        with sqlite3.connect(root / DATABASE) as db:
            if damage == "foreign_key":
                db.execute(
                    "INSERT INTO inputs VALUES (?,?,?,?)",
                    ("f" * 32, "0", "missing", "0" * 64),
                )
            elif damage == "audit":
                db.execute("DELETE FROM events WHERE sequence=0")
            elif damage == "input_path":
                db.execute(
                    "UPDATE inputs SET path='../outside.docx' WHERE source_id='0'"
                )
            else:
                db.execute("CREATE TABLE secrets (api_key TEXT)")
    reseal_database(root)
    with pytest.raises((BackupError, sqlite3.Error, StorageIntegrityError)):
        verify_backup(root)


@pytest.mark.parametrize("target_exists", ("empty", "occupied"))
def test_existing_target_is_always_preserved(sample_backup, tmp_path, target_exists):
    root, _ = sample_backup
    target = tmp_path / "restore-target"
    target.mkdir()
    if target_exists == "occupied":
        (target / "important.txt").write_bytes(b"PRESERVE")
    before = tree_bytes(target)
    with pytest.raises(BackupError) as exc:
        restore(root, target)
    assert exc.value.code == "target_exists" and tree_bytes(target) == before


def test_hard_link_to_outside_backup_is_rejected(sample_backup, tmp_path):
    root, run_id = clone(sample_backup, tmp_path)
    source = root / run_id / "work_request.docx"
    outside = tmp_path / "outside.docx"
    os.link(source, outside)
    before = outside.read_bytes()
    with pytest.raises(BackupError):
        verify_backup(root)
    with pytest.raises(BackupError):
        restore(root, tmp_path / "restored")
    assert outside.read_bytes() == before


def test_source_secrets_and_recovery_material_are_not_copied(tmp_path):
    data = tmp_path / "data"
    run_id = make_data(data)
    (data / ".env").write_text("PRIVATE-API-KEY", encoding="utf-8")
    (data / "notes.tmp").write_text("PRIVATE-EXTRANEOUS", encoding="utf-8")
    quarantine = data / run_id / ".artifact-recovery"
    quarantine.mkdir()
    (quarantine / "old.tmp").write_bytes(b"PRIVATE-UNCONFIRMED")
    backup(data, tmp_path / "backup")
    names = set(tree_bytes(tmp_path / "backup"))
    assert not any(".env" in n or ".tmp" in n or "recovery" in n for n in names)
    assert (data / ".env").read_text(encoding="utf-8") == "PRIVATE-API-KEY"


@pytest.mark.parametrize("suffix", ("-wal", "-shm", "-journal"))
def test_sqlite_sidecars_refused_without_modifying_source(tmp_path, suffix):
    data = tmp_path / "data"
    make_data(data, documents=False)
    (data / (DATABASE + suffix)).write_bytes(b"unresolved sidecar")
    before = tree_bytes(data)
    with pytest.raises(BackupError) as exc:
        backup(data, tmp_path / "backup")
    assert exc.value.code == "sqlite_sidecars_present" and tree_bytes(data) == before


def test_cli_safe_errors_do_not_print_document_or_secret(
    sample_backup, tmp_path, capsys
):
    root, run_id = clone(sample_backup, tmp_path)
    (root / run_id / "work_request.docx").write_bytes(b"PRIVATE-DOC-AND-SECRET")
    assert main(["verify-backup", "--backup", str(root)]) == 1
    output = capsys.readouterr().out
    assert "PRIVATE" not in output and json.loads(output)["status"] == "error"
