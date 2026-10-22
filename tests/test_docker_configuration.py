"""Configuration and shared-lease checks; these do not pretend to run Docker."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from document_automation.data_lease import DataBusyError, DataLease

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "smoke_docker", ROOT / "scripts/smoke_docker.py"
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_image_and_compose_mount_data_and_lock_parent_for_nonroot():
    image = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "compose.yaml").read_text()
    assert "USER 1000:1000" in image
    assert (
        "groupadd --gid 1000 app" in image and "useradd --uid 1000 --gid 1000" in image
    )
    assert f"mkdir -p {smoke.DATA}" in image
    assert f"chown -R app:app {smoke.PARENT}" in image
    assert f"ENV DAA_DATA_DIR={smoke.DATA}" in image
    assert f"workflow-data:{smoke.DATA}" in compose
    assert f"workflow-state:{smoke.PARENT}" in compose
    assert "127.0.0.1:" in compose
    assert "chmod 777" not in image and "USER root" not in image
    assert "workflow-data:/data" not in compose
    assert all(
        target in " ".join(smoke.mounts("synthetic"))
        for target in (f"target={smoke.DATA}", f"target={smoke.PARENT}")
    )


def test_bind_mount_shares_parent_and_data():
    args = smoke.mounts("unused", Path("/synthetic-bind"))
    assert args == [
        "--mount",
        "type=bind,source=/synthetic-bind,target=/var/lib/daa",
    ] or args == ["--mount", r"type=bind,source=\synthetic-bind,target=/var/lib/daa"]


def test_smoke_refuses_occupied_bind_without_docker(tmp_path, monkeypatch):
    (tmp_path / "original").write_text("synthetic")
    monkeypatch.setattr(smoke, "docker", lambda *a: pytest.fail("Docker must not run"))
    with pytest.raises(ValueError, match="empty"):
        smoke.smoke(tmp_path)
    assert (tmp_path / "original").read_text() == "synthetic"


def test_shared_parent_lock_excludes_second_process(tmp_path):
    root = tmp_path / "shared-parent/data"
    root.mkdir(parents=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    code = (
        "import sys\n"
        "from document_automation.data_lease import DataLease, DataBusyError\n"
        "try:\n"
        "    with DataLease(sys.argv[1], 'maintenance'): pass\n"
        "except DataBusyError:\n"
        "    sys.exit(23)\n"
    )

    def probe():
        return subprocess.run(
            [sys.executable, "-B", "-c", code, str(root)],
            env=env,
            capture_output=True,
            timeout=15,
        ).returncode

    with DataLease(root, "backend"):
        assert len(list(root.parent.glob(".daa-data-*.lock"))) == 1
        assert not list(root.glob("*.lock"))
        assert probe() == 23
    assert probe() == 0


def test_wrong_parent_permissions_do_not_fall_back_to_private_lock(
    tmp_path, monkeypatch
):
    root = tmp_path / "shared/data"
    root.mkdir(parents=True)
    original = Path.open
    attempts = []

    def denied(path, *args, **kwargs):
        if path.name.startswith(".daa-data-"):
            attempts.append(path)
            raise PermissionError("Synthetic volume ownership mismatch")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises((PermissionError, DataBusyError)):
        with DataLease(root):
            pytest.fail("Must fail closed")
    assert len(attempts) == 1 and attempts[0].parent == root.parent
    assert not any(root.iterdir())
