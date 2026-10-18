"""Exercise a running local API with real synthetic files, without paid calls."""

import base64
import os
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
output = root / "tmp" / "demo-output"
output.mkdir(parents=True, exist_ok=True)
with httpx.Client(
    base_url=os.environ.get("DAA_API_URL", "http://127.0.0.1:18000"), timeout=120
) as client:
    assert client.get("/health").json()["mode"] == "demo-rules-v1"
    for scenario in ("A", "B", "C", "V", "T"):
        files = [
            {
                "filename": p.name,
                "content_base64": base64.b64encode(p.read_bytes()).decode(),
            }
            for p in sorted(
                (root / "demo" / (scenario if scenario != "T" else "A")).glob("*")
            )
        ]
        if scenario == "T":
            files = [{"filename": "broken.pdf", "content_base64": "YQ=="}]
        response = client.post("/runs", json={"files": files})
        response.raise_for_status()
        result = response.json()
        run = result["run_id"]
        assert result["processing_status"] == (
            "failed" if scenario == "T" else "completed"
        )
        assert result["needs_review"] == (scenario in ("B", "C", "V"))
        generated = client.post(f"/runs/{run}/document")
        downloaded = client.get(f"/runs/{run}/document")
        assert (
            generated.status_code
            == downloaded.status_code
            == (409 if scenario == "T" else 200)
        )
        if scenario != "T":
            report_generated = client.post(f"/runs/{run}/verification-report")
            report_downloaded = client.get(f"/runs/{run}/verification-report")
            assert report_generated.status_code == report_downloaded.status_code == 200
            saved = client.get(f"/runs/{run}").json()
            assert (
                saved["generated_document"]["run_id"]
                == saved["generated_report"]["run_id"]
                == run
            )
            (output / f"{scenario}-work-request.docx").write_bytes(downloaded.content)
            (output / f"{scenario}-verification-report.docx").write_bytes(
                report_downloaded.content
            )
        else:
            assert client.post(f"/runs/{run}/verification-report").status_code == 409
            assert client.get(f"/runs/{run}/verification-report").status_code == 409
        print(
            scenario,
            result["processing_status"],
            result["needs_review"],
            downloaded.status_code,
            (report_downloaded.status_code if scenario != "T" else 409),
        )
assert (
    httpx.get(
        os.environ.get("DAA_UI_URL", "http://127.0.0.1:18501") + "/_stcore/health"
    ).status_code
    == 200
)
print("API and Streamlit health passed")
