"""Synthetic non-root Docker smoke; fresh resources only, no paid API calls."""

import argparse
import base64
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PARENT = "/var/lib/daa"
DATA = PARENT + "/data"


def mounts(prefix, bind_parent=None):
    if bind_parent is not None:
        return ["--mount", f"type=bind,source={bind_parent},target={PARENT}"]
    return [
        "--mount",
        f"type=volume,source={prefix}-state,target={PARENT}",
        "--mount",
        f"type=volume,source={prefix}-data,target={DATA}",
    ]


def docker(*args):
    result = subprocess.run(
        ["docker", *map(str, args)],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip()


def health(client):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Docker backend /health did not become ready")


def lease_probe(busy):
    return (
        "import os\n"
        "from document_automation.data_lease import DataLease, DataBusyError\n"
        "assert os.geteuid() == os.getegid() == 1000\n"
        "try:\n"
        "    with DataLease(os.environ['DAA_DATA_DIR'], 'maintenance'): pass\n"
        "except DataBusyError:\n"
        f"    assert {busy!r}, 'Unexpected busy lease'\n"
        "else:\n"
        f"    assert {not busy!r}, 'Shared lock failed to exclude maintenance'\n"
    )


def smoke(bind_parent=None):
    if bind_parent is not None:
        bind_parent = bind_parent.absolute()
        if (
            not bind_parent.is_dir()
            or bind_parent.is_symlink()
            or bind_parent.resolve() != bind_parent
            or any(bind_parent.iterdir())
            or "," in str(bind_parent)
        ):
            raise ValueError("Bind parent must be an existing empty real directory")
    prefix = "daa-smoke-" + uuid.uuid4().hex[:12]
    image = prefix + ":local"
    mounting = mounts(prefix, bind_parent)
    print(
        json.dumps({"image": image, "container": prefix, "mounts": mounting}),
        flush=True,
    )
    docker("version")
    docker("build", "--tag", image, ".")
    if bind_parent is None:
        for suffix in ("state", "data"):
            docker("volume", "create", prefix + "-" + suffix)
    started = False
    try:
        docker(
            "run",
            "--detach",
            "--name",
            prefix,
            *mounting,
            "--publish",
            "127.0.0.1::8000",
            "--env",
            "DAA_MODE=demo",
            "--env",
            "DAA_ALLOW_PAID=false",
            image,
        )
        started = True
        port = docker("port", prefix, "8000/tcp").splitlines()[0].rsplit(":", 1)[1]
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=30,
        ) as client:
            health(client)
            docker(
                "exec",
                prefix,
                "python",
                "-c",
                "import os; from pathlib import Path; "
                "assert os.geteuid() == os.getegid() == 1000; "
                "p=Path(os.environ['DAA_DATA_DIR']); "
                "assert p.as_posix() == '/var/lib/daa/data'; "
                "assert os.access(p, os.W_OK) and os.access(p.parent, os.W_OK); "
                "assert len(list(p.parent.glob('.daa-data-*.lock'))) == 1",
            )
            files = [
                {
                    "filename": p.name,
                    "content_base64": base64.b64encode(p.read_bytes()).decode(),
                }
                for p in sorted((ROOT / "demo/A").iterdir())
                if p.suffix in (".pdf", ".docx", ".xlsx")
            ]
            payload, headers = {"files": files}, {"Idempotency-Key": "docker-smoke"}
            response = client.post("/runs", json=payload, headers=headers)
            assert response.status_code == 201, response.status_code
            result = response.json()
            assert result["processing_status"] == "completed"
            run_id = result["run_id"]
            artifacts = {}
            for route in ("document", "verification-report"):
                assert client.post(f"/runs/{run_id}/{route}").status_code == 200
                response = client.get(f"/runs/{run_id}/{route}")
                assert response.status_code == 200
                artifacts[route] = response.content
            usage = client.get(f"/runs/{run_id}/usage").json()
            assert client.get("/business-metrics").json()["llm_attempts"] == 0
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                *mounting,
                image,
                "python",
                "-c",
                lease_probe(True),
            )
        docker("restart", prefix)
        # Docker may assign a new ephemeral host port after restart.
        port = docker("port", prefix, "8000/tcp").splitlines()[0].rsplit(":", 1)[1]
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=30,
        ) as client:
            health(client)
            replay = client.post("/runs", json=payload, headers=headers)
            assert replay.status_code == 200 and replay.json()["run_id"] == run_id
            assert client.get(f"/runs/{run_id}/usage").json() == usage
            for route, content in artifacts.items():
                assert client.get(f"/runs/{run_id}/{route}").content == content
            assert client.get("/business-metrics").json()["llm_attempts"] == 0
        docker("stop", prefix)
        started = False
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            *mounting,
            image,
            "python",
            "-c",
            lease_probe(False),
        )
        print(
            json.dumps(
                {
                    "passed": True,
                    "run_id": run_id,
                    "uid": 1000,
                    "gid": 1000,
                    "docx_unchanged_after_restart": 2,
                    "llm_attempts": 0,
                    "maintenance_excluded_while_backend_running": True,
                    "resources_retained": prefix,
                }
            ),
            flush=True,
        )
    finally:
        if started:
            docker("stop", prefix)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-parent", type=Path)
    smoke(parser.parse_args().bind_parent)
