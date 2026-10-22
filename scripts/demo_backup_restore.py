"""Actual local CLI backup/verify/restore and HTTP checks with synthetic data only."""

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from document_automation.adapters.openai_extractor import OpenAIExtractor
from document_automation.backup import BackupManifest
from document_automation.backup_paths import checksum
from document_automation.extraction import DemoExtractor
from document_automation.llm_usage import TariffCatalog
from document_automation.schemas import ProcessingResult, result_fingerprint
from document_automation.service import DocumentService

ROOT = Path(__file__).resolve().parents[1]
CHILD_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def environment():
    return {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUTF8": "1",
        "DAA_MODE": "demo",
        "DAA_ALLOW_PAID": "false",
        "DAA_LLM_TARIFFS": "{}",
        "DAA_EXTRACTION_LIMITS": "{}",
    }


def uploads(paths):
    return [
        dict(filename=p.name, content_base64=base64.b64encode(p.read_bytes()).decode())
        for p in paths
    ]


async def seed(data):
    infos, calls = [], []
    v1_files = uploads(
        sorted(
            p
            for p in (ROOT / "demo/A").iterdir()
            if p.suffix in (".pdf", ".docx", ".xlsx")
        )
    )
    service = DocumentService(data, DemoExtractor(), contract_version="1.0")
    v1 = await service.process(v1_files, "backup-demo-v1")
    await service.generate(v1.run_id)
    await service.generate_report(v1.run_id)
    infos.append(
        dict(run_id=v1.run_id, key="backup-demo-v1", files=v1_files, retry_of=None)
    )

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
    tariffs = TariffCatalog.model_validate_json(
        (ROOT / "config/llm_tariffs.example.json").read_text(encoding="utf-8")
    )
    service = DocumentService(data, adapter, tariffs=tariffs)
    files = uploads([ROOT / "tests/fixtures/formats/all.docx"])
    parent = await service.process(files, "backup-demo-failed")
    child = await service.process(files, "backup-demo-child", retry_of=parent.run_id)
    unknown = await service.process(files, "backup-demo-timeout")
    assert parent.processing_status == unknown.processing_status == "failed"
    assert child.processing_status == "completed" and len(calls) == 3
    await service.generate(child.run_id)
    await service.generate_report(child.run_id)
    for result, key, retry_of in (
        (parent, "backup-demo-failed", None),
        (child, "backup-demo-child", parent.run_id),
        (unknown, "backup-demo-timeout", None),
    ):
        infos.append(
            dict(run_id=result.run_id, key=key, files=files, retry_of=retry_of)
        )
    return infos, len(calls)


@contextmanager
def server(data):
    with socket.socket() as socket_for_port:
        socket_for_port.bind(("127.0.0.1", 0))
        port = socket_for_port.getsockname()[1]
    code = """import os,sys,threading,uvicorn
server=uvicorn.Server(uvicorn.Config('document_automation.api:create_app',factory=True,host='127.0.0.1',port=int(sys.argv[1]),log_level='warning',timeout_graceful_shutdown=5))
server.config.load()
def stop():
    os.read(sys.stdin.fileno(),1)
    server.should_exit=True
threading.Thread(target=stop,daemon=True).start()
server.run()
"""
    env = {**environment(), "DAA_DATA_DIR": str(data.resolve())}
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(port)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CHILD_FLAGS,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
        ) as client:
            deadline = time.monotonic() + 15
            last_status = "not ready"
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Synthetic backend did not start")
                try:
                    response = client.get("/health")
                    last_status = "HTTP " + str(response.status_code)
                    if response.status_code == 200:
                        break
                except httpx.TransportError as exc:
                    last_status = type(exc).__name__
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "Synthetic backend startup timed out: " + last_status
                    )
                time.sleep(0.05)
            yield client
    finally:
        if process.poll() is None:
            process.stdin.write(b"q")
            process.stdin.flush()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
                raise RuntimeError("Backend did not stop gracefully") from None
        process.stdin.close()
        if process.returncode != 0:
            raise RuntimeError("Synthetic backend exited with an error")


def cli(*args, success=True):
    result = subprocess.run(
        [sys.executable, "-B", "-m", "document_automation.backup", *map(str, args)],
        env=environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        creationflags=CHILD_FLAGS,
    )
    report = json.loads(result.stdout)
    if (result.returncode == 0) != success:
        raise RuntimeError(
            "Unexpected synthetic CLI outcome: " + report.get("code", "unknown")
        )
    return report


def observe(client, infos):
    results, usage, operations, artifacts = {}, {}, {}, {}
    for info in infos:
        run_id = info["run_id"]
        result = client.get(f"/runs/{run_id}")
        result.raise_for_status()
        raw = result.json()
        replay = client.post(
            "/runs",
            json={"files": info["files"], "retry_of": info["retry_of"]},
            headers={"Idempotency-Key": info["key"]},
        )
        assert replay.status_code == 200 and replay.json() == raw
        for field, route in (
            ("generated_document", "document"),
            ("generated_report", "verification-report"),
        ):
            if raw[field]:
                response = client.get(f"/runs/{run_id}/{route}")
                response.raise_for_status()
                from hashlib import sha256

                artifacts[f"{run_id}/{raw[field]['filename']}"] = sha256(
                    response.content
                ).hexdigest()
                assert (
                    artifacts[f"{run_id}/{raw[field]['filename']}"]
                    == raw[field]["sha256"]
                )
        results[run_id] = client.get(f"/runs/{run_id}").json()
        usage[run_id] = client.get(f"/runs/{run_id}/usage").json()
        operations[run_id] = client.get(f"/runs/{run_id}/operation").json()
    return results, usage, operations, artifacts


def demonstrate(output):
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    data, copy, restored = output / "data", output / "backup", output / "restored"
    infos, calls = asyncio.run(seed(data))
    with server(data) as client:
        before_results, before_usage, before_operations, before_artifacts = observe(
            client, infos
        )
        busy = cli(
            "backup",
            "--data",
            data,
            "--output",
            output / "must-not-exist",
            success=False,
        )
        assert busy["code"] == "data_busy"
    original_db = checksum(data / "workflow.sqlite3")
    created = cli("backup", "--data", data, "--output", copy)
    verified = cli("verify-backup", "--backup", copy)
    recovered = cli("restore", "--backup", copy, "--target", restored)
    manifest = BackupManifest.model_validate_json((copy / "manifest.json").read_bytes())
    registered = [e for e in manifest.files if e.role != "database"]
    assert all(
        checksum(data / e.path) == checksum(restored / e.path) == (e.size, e.sha256)
        for e in registered
    )
    assert all(
        not os.path.samefile(copy / e.path, restored / e.path)
        and (restored / e.path).stat().st_nlink == 1
        for e in manifest.files
    )
    with server(restored) as client:
        # Compare snapshots before exports legitimately append new audit events.
        for run_id, raw in before_results.items():
            assert client.get(f"/runs/{run_id}").json() == raw
        after_results, after_usage, after_operations, after_artifacts = observe(
            client, infos
        )
    assert before_usage == after_usage and before_operations == after_operations
    assert (
        before_artifacts == after_artifacts
        and checksum(data / "workflow.sqlite3") == original_db
    )
    for run_id in before_results:
        assert result_fingerprint(
            ProcessingResult.model_validate_json(json.dumps(before_results[run_id]))
        ) == result_fingerprint(
            ProcessingResult.model_validate_json(json.dumps(after_results[run_id]))
        )
    broken = output / "broken-backup"
    shutil.copytree(copy, broken)
    victim = next(e for e in manifest.files if e.role == "document")
    (broken / victim.path).write_bytes(b"synthetic corrupted artifact")
    corrupt = cli("verify-backup", "--backup", broken, success=False)
    cli(
        "restore",
        "--backup",
        broken,
        "--target",
        output / "invalid-restore",
        success=False,
    )
    assert not (output / "invalid-restore").exists()
    occupied = output / "occupied"
    occupied.mkdir()
    (occupied / "preserve.txt").write_bytes(b"PRESERVE")
    conflict = cli("restore", "--backup", copy, "--target", occupied, success=False)
    assert (
        conflict["code"] == "target_exists"
        and (occupied / "preserve.txt").read_bytes() == b"PRESERVE"
    )
    summary = dict(
        data_origin="synthetic documents; actual CLI and two localhost HTTP backend sessions",
        backup=created,
        verify=verified,
        restore=recovered,
        live_backend_backup_blocked=True,
        backend_stopped_gracefully=True,
        old_run_ids_readable=True,
        snapshots_preserved_before_exports=True,
        business_fingerprints_preserved=True,
        provenance_files_preserved=True,
        idempotency_preserved=True,
        retry_of_preserved=True,
        usage_preserved=True,
        unknown_cost_preserved=before_usage[infos[-1]["run_id"]]["has_unknown_cost"],
        source_database_unchanged=True,
        independent_restored_files=True,
        registered_file_sha256_matches=len(registered),
        docx_sha256_matches=len(after_artifacts),
        corrupt_backup_rejected=corrupt["code"],
        occupied_target_preserved=True,
        mock_llm_attempts=calls,
        external_llm_calls=0,
    )
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    (output / "summary.json").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/backup-restore-demo")
    demonstrate(parser.parse_args().output)
